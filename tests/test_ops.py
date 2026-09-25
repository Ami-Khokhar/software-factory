"""Phase F: resolving escalations, proposing issues, the cost ledger and the autonomy rate."""
import json
import unittest
from pathlib import Path

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.metrics import autonomy, cost, escalation
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, ISSUE_BODY, result_line

REPO, ID = "o/r", "o/r#7"


class ResolveHumanTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 7)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO}), owner="amteshwar"),
                               self.relay)
        self.foreman.intake(REPO, 7)

    def tick(self):
        return self.foreman.tick(ID)["workItem"]

    def block(self):
        for _ in range(2):
            wi = self.tick()
            output = json.dumps({"usage": {"tokens": 1000, "usd": 0.25}})
            self.relay.finish(wi["providerTaskId"], output=output)
            wi = self.tick()
        self.assertEqual(wi["state"], "blocked")
        return wi

    def test_blocked_escalation_then_retry(self):
        wi = self.block()
        self.assertEqual(wi["owner"], "amteshwar")
        history = self.foreman.store.history(ID)
        asked = escalation(wi, history)
        self.assertEqual(len(asked["tried"]), 2)
        self.assertIn("factory_resolve_human", asked["needFromYou"])
        self.assertEqual(cost(history, 2), {"passes": 2, "tokens": 2000, "usd": 0.5})
        needs = tools.build(self.foreman.store, self.foreman)["factory_status"].fn({})["needsYou"]
        self.assertEqual([(n["id"], n["reason"]) for n in needs], [(ID, wi["blockedOn"])])

        wi = self.foreman.resolve_human(ID, "retry", "Registry path fixed on the runner", "amteshwar")
        self.assertEqual((wi["state"], wi["attempts"], wi["blockedOn"]), ("ready", [], None))
        self.assertEqual(self.tick()["state"], "implementing")
        with self.assertRaisesRegex(ToolError, "not waiting on a person"):
            self.foreman.resolve_human(ID, "close", "n", "amteshwar")

    def test_retry_on_a_pr_sends_the_note_as_a_finding(self):
        wi = self.tick()
        self.github.push(REPO, "factory/7", "c1")
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"], "c1"))
        self.tick()
        self.github.add_run(REPO, "c1", conclusion="neutral")
        self.github.add_verdict(REPO, 100, "HUMAN", 1, "c1")
        self.assertEqual(self.tick()["state"], "human_waiting")
        with self.assertRaisesRegex(ToolError, "resolution must be"):
            self.foreman.resolve_human(ID, "merge", "n", "amteshwar")

        self.foreman.resolve_human(ID, "retry", "Keep the old path as a fallback", "amteshwar")
        wi = self.tick()
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((wi["state"], order["kind"]), ("implementing", "fix"))
        self.assertEqual(order["findingsToAddress"],
                         [{"fingerprint": "human", "problem": "Keep the old path as a fallback"}])

    def test_regate_and_close(self):
        self.block()
        with self.assertRaisesRegex(ToolError, "regate \\(needs a PR\\)"):
            self.foreman.resolve_human(ID, "regate", "pushed", "amteshwar")
        wi = self.foreman.resolve_human(ID, "close", "Not worth it", "amteshwar")
        self.assertEqual((wi["state"], wi["terminalReason"]), ("cancelled", "closed by amteshwar: Not worth it"))

    def test_propose_issue(self):
        made = self.foreman.propose_issue(REPO, "Fail loud", ISSUE_BODY)
        issue = self.github.issues[(REPO, made["issue"])]
        self.assertEqual([label["name"] for label in issue["labels"]], ["factory:proposed"])
        with self.assertRaises(ToolError):
            self.foreman.propose_issue(REPO, "Vague", "## Acceptance\n- it works")
        with self.assertRaises(ToolError):
            self.foreman.propose_issue("other/repo", "Fail loud", ISSUE_BODY)


class AutonomyTest(unittest.TestCase):
    def test_rate_from_fixture_history(self):
        fixture = json.loads((Path(__file__).parent / "fixtures" / "autonomy-history.json").read_text())
        store = Store(":memory:")
        for workitem_id, item in fixture["workItems"].items():
            for action, detail in item["history"]:
                store.log(action, detail, workitem_id)
        workitems = [{"id": i, "state": item["state"]} for i, item in fixture["workItems"].items()]
        self.assertEqual(autonomy(workitems, store.history), fixture["expected"])


if __name__ == "__main__":
    unittest.main()
