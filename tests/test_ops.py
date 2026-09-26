"""A person's acts: approving an issue, answering an escalation. Proposing issues, worker passes
and the autonomy rate."""
import json
import unittest
from pathlib import Path

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.metrics import autonomy, escalation
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
            self.relay.finish(wi["providerTaskId"], output="no result here")
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
        status = tools.build(self.foreman.store, self.foreman)["factory_status"].fn({"id": ID})
        self.assertEqual(status["passes"], 2)

        wi = self.foreman.resolve_human(ID, "retry", "Registry path fixed on the runner", "amteshwar")
        self.assertEqual((wi["state"], wi["attempts"], wi["blockedOn"]), ("ready", [], None))
        self.assertEqual(self.tick()["state"], "implementing")
        with self.assertRaisesRegex(ToolError, "not waiting on a person"):
            self.foreman.resolve_human(ID, "close", "n", "amteshwar")

    def gate_says_human(self):
        """PR 100 at c1, with a HUMAN verdict from the gate."""
        wi = self.tick()
        self.github.push(REPO, "factory/7", "c1")
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"], "c1"))
        self.tick()
        self.github.add_run(REPO, "c1", conclusion="neutral")
        self.github.add_verdict(REPO, 100, "HUMAN", 1, "c1")
        self.assertEqual(self.tick()["state"], "human_waiting")

    def test_retry_on_a_pr_sends_the_note_as_a_finding(self):
        self.gate_says_human()
        with self.assertRaisesRegex(ToolError, "resolution must be"):
            self.foreman.resolve_human(ID, "merge", "n", "amteshwar")

        self.foreman.resolve_human(ID, "retry", "Keep the old path as a fallback", "amteshwar")
        wi = self.tick()
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((wi["state"], order["kind"]), ("implementing", "fix"))
        self.assertEqual(order["findingsToAddress"],
                         [{"fingerprint": "human", "problem": "Keep the old path as a fallback"}])

    def test_regate_sends_a_pr_back_to_the_gate(self):
        self.gate_says_human()
        wi = self.foreman.resolve_human(ID, "regate", "Pushed the fix myself", "amteshwar")
        self.assertEqual((wi["state"], wi["blockedOn"]), ("reviewing", None))
        self.assertEqual(self.tick()["state"], "reviewing")  # it waits for the gate: no worker pass
        self.assertEqual(len(self.foreman.store.workorders(ID)), 1)

    def test_a_decision_waits_while_a_tick_holds_the_lease(self):
        self.block()
        self.foreman.store.acquire(ID, "another-tick")
        with self.assertRaisesRegex(ToolError, "is being ticked; try again"):
            self.foreman.resolve_human(ID, "close", "Not worth it", "amteshwar")
        self.assertEqual(self.foreman.store.get(ID)["state"], "blocked")

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


class ApproveIssueTest(unittest.TestCase):
    def setUp(self):
        self.github = FakeGitHub()
        self.foreman = Foreman(Store(":memory:"), self.github, Config(mcp_token="t", repos=frozenset({REPO})),
                               FakeRelay())

    def test_approving_labels_the_issue_and_takes_it_in_once(self):
        self.github.add_issue(REPO, 7, labels=())
        for _ in range(2):
            wi = self.foreman.approve_issue(REPO, 7, "amteshwar")
        self.assertEqual(wi["state"], "ready")
        self.assertEqual(self.github.labelled, [(REPO, 7, ["factory:ready"])])
        self.assertEqual([h["action"] for h in self.foreman.store.history(ID)], ["approved", "intake"])

    def test_any_case_of_the_repo_name_is_the_one_workitem(self):
        self.github.add_issue(REPO, 7, labels=())
        self.assertEqual(self.foreman.approve_issue(REPO.upper(), 7, "amteshwar")["id"], ID)
        self.assertEqual(self.foreman.intake("o/R", 7)["id"], ID)
        self.assertEqual(len(self.foreman.store.all()), 1)

    def test_a_finished_workitem_is_not_approved_again(self):
        self.github.add_issue(REPO, 7, labels=())
        wi = self.foreman.approve_issue(REPO, 7, "amteshwar")
        self.foreman.store.put(dict(wi, state="exhausted"))
        with self.assertRaisesRegex(ToolError, "o/r#7 is exhausted; open a new issue"):
            self.foreman.approve_issue(REPO, 7, "amteshwar")
        self.assertEqual(len(self.github.labelled), 1)

    def test_what_intake_refuses_is_refused_before_the_label(self):
        """A factory:ready label alone makes the next rebuild adopt the issue."""
        self.github.add_issue(REPO, 7, labels=(), author="stranger")
        self.github.add_issue(REPO, 8, labels=(), state="closed")
        self.github.add_issue(REPO, 9, labels=(), body="No acceptance here.")
        cases = [("an untrusted author", REPO, 7, "stranger, who is not a trusted author"),
                 ("a closed issue", REPO, 8, "o/r#8 is closed"),
                 ("no acceptance checks", REPO, 9, "needs an '## Acceptance' list"),
                 ("an issue that does not exist", REPO, 404, "o/r#404 does not exist"),
                 ("a repo off the allowlist", "o/other", 7, "o/other is not in FACTORY_REPOS or repos.json")]
        for case, repo, number, reason in cases:
            with self.subTest(case), self.assertRaisesRegex(ToolError, reason):
                self.foreman.approve_issue(repo, number, "amteshwar")
        self.assertEqual((self.github.labelled, self.foreman.store.all()), ([], []))


class AutonomyTest(unittest.TestCase):
    def test_a_merge_without_the_gate_is_not_autonomous(self):
        store = Store(":memory:")
        for workitem_id in ("o/r#1", "o/r#2"):
            store.log("result", {"commitSha": "c1"}, workitem_id)
            store.log("reconcile", {"headSha": "c1", "from": "pr_open", "to": "merged"}, workitem_id)
        workitems = [{"id": "o/r#1", "state": "merged", "terminalReason": None},
                     {"id": "o/r#2", "state": "merged",
                      "terminalReason": "merged without a pr-gatekeeper MERGE verdict on c1"}]
        self.assertEqual(autonomy(workitems, store.history),
                         {"ended": 2, "autonomous": 1, "unknown": 0, "rate": 0.5})

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
