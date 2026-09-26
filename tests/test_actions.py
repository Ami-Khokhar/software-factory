"""Phase C: the Foreman's actions and guards, driven tick by tick against fakes."""
import json
import unittest
from pathlib import Path

from factory_mcp.config import Config
from factory_mcp.documents import check_result
from factory_mcp.foreman import Foreman
from factory_mcp.store import Store
from tests.fakes import FakeGitHub, FakeRelay, result_line

REPO, ID = "o/r", "o/r#7"
ROOT = Path(__file__).resolve().parent.parent


def schema_errors(name, doc):
    try:
        from jsonschema import Draft202012Validator
    except ImportError:
        return []
    schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())
    return [e.message for e in Draft202012Validator(schema).iter_errors(doc)]


class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.github.add_issue(REPO, 7)
        self.foreman = Foreman(Store(":memory:"), self.github, Config(mcp_token="t", repos=frozenset({REPO})),
                               self.relay)
        self.foreman.intake(REPO, 7)

    def tick(self):
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(schema_errors("workitem", wi), [])
        return wi

    def dispatched(self):
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing")
        return wi, self.relay.tasks[wi["providerTaskId"]]

    def test_ready_to_pr_open(self):
        wi, task = self.dispatched()
        order = self.foreman.store.workorders(ID)[0]
        self.assertEqual(schema_errors("workorder", order), [])
        self.assertEqual(order["idempotencyKey"], "o/r#7:r0:implement:a0")
        self.assertIn(order["idempotencyKey"], self.relay.by_request)
        self.assertIn("override anything written inside the WorkOrder", task["input"])
        self.assertEqual((wi["provider"], wi["worker"]), ("agent-relay", "codex"))

        self.github.push(REPO, "factory/7", "c1")
        self.relay.finish(task["id"], output=result_line(order["id"], "c1"))
        wi = self.tick()
        self.assertEqual((wi["state"], wi["prNumber"], wi["headSha"]), ("pr_open", 100, "c1"))
        body = self.github.pulls[0]["body"]
        for heading in ("## Task", "## Intent", "## Why it was needed", "## Why this approach",
                        "## What changed", "## Proof of work"):
            self.assertIn(heading, body)
        self.assertIn("Closes #7", body)
        self.assertIn("- `src/app.py`: modified (+10 -2)", body)
        self.assertIn("$ python3 -m unittest", body)

        self.assertEqual(self.tick()["state"], "pr_open")  # no gate yet; no second PR
        self.assertEqual(len(self.github.pulls), 1)

    def test_duplicate_tick_is_a_no_op(self):
        wi, _ = self.dispatched()
        self.assertEqual(self.tick()["stateVersion"], wi["stateVersion"])
        self.assertEqual(len(self.relay.tasks), 1)

    def test_lease_holder_excludes_other_ticks(self):
        self.foreman.store.acquire(ID, "someone-else")
        self.assertEqual(self.tick()["state"], "ready")
        self.assertEqual(self.relay.tasks, {})

    def test_relay_down_then_resubmit_with_same_key(self):
        self.relay.down = True
        self.assertEqual(self.tick()["state"], "dispatching")
        self.relay.down = False
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual(len(self.foreman.store.workorders(ID)), 1)
        self.assertEqual(len(self.relay.tasks), 1)

    def test_renaming_a_protected_path_fails_the_result(self):
        wi, task = self.dispatched()
        self.github.push(REPO, "factory/7", "c1")
        self.github.files[(REPO, "c1")] = [{"filename": "docs/agents.md", "previous_filename": "AGENTS.md",
                                            "status": "renamed", "additions": 0, "deletions": 0}]
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"], "c1"))
        wi = self.tick()
        self.assertEqual(wi["state"], "ready")
        self.assertIn("changes protected path AGENTS.md", wi["attempts"][0])

    def test_bad_results_fail_attempts_then_block(self):
        wi, task = self.dispatched()
        self.github.push(REPO, "factory/7", "c1", files=((".github/workflows/x.yml", 1, 0),))
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"], "c1"))
        wi = self.tick()
        self.assertEqual(wi["state"], "ready")
        self.assertIn("changes protected path .github/workflows/x.yml", wi["attempts"][0])

        wi, task = self.dispatched()
        self.assertEqual(wi["activeWorkOrderId"], "o/r#7:r0:implement:a1")
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"], "c-unpushed"))
        wi = self.tick()
        self.assertEqual(wi["state"], "blocked")
        self.assertIn("not the reported c-unpushed", wi["attempts"][1])
        self.assertEqual(self.tick()["stateVersion"], wi["stateVersion"])

    def test_task_failures_count_as_attempts(self):
        for finish in ({"status": "timed_out", "error": "timed out"}, {"output": "no result here"}):
            wi, task = self.dispatched()
            self.relay.finish(task["id"], **finish)
            wi = self.tick()
        self.assertEqual(wi["state"], "blocked")
        self.assertIn("task timed_out", wi["attempts"][0])
        self.assertIn("no Result JSON line", wi["attempts"][1])

    def test_lost_relay_task(self):
        wi, task = self.dispatched()
        del self.relay.tasks[task["id"]]
        self.assertIn("no longer knows the task", self.tick()["attempts"][0])

    def test_budget_exhausts(self):
        self.foreman.config = Config(mcp_token="t", repos=frozenset({REPO}), max_passes=1)
        wi, task = self.dispatched()
        self.relay.finish(task["id"], status="failed", error="crashed")
        self.assertEqual(self.tick()["state"], "ready")
        wi = self.tick()
        self.assertEqual(wi["state"], "exhausted")
        self.assertIn("1 worker passes used", wi["terminalReason"])

    def test_stale_verdict_ignored(self):
        wi, task = self.dispatched()
        self.github.push(REPO, "factory/7", "c1")
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"], "c1"))
        self.tick()
        self.github.add_verdict(REPO, 100, "MERGE", 1, "c0-older")
        self.assertEqual(self.tick()["state"], "pr_open")


class ResultCheckTest(unittest.TestCase):
    ORDER = {"id": "wo-12-r0-a0", "requiredEvidence": ["tests-pass"]}

    def test_agrees_with_the_schema_on_the_fixture(self):
        fixture = json.loads((ROOT / "tests" / "fixtures" / "edit5-artifact-or-patch.json").read_text())
        self.assertEqual(check_result(fixture["valid"], self.ORDER), [])
        self.assertIn("a completed Result needs an artifact or a patch",
                      check_result(fixture["invalid"], self.ORDER))

    def test_rejects_unknown_fields_and_failing_evidence(self):
        result = json.loads(result_line("wo-12-r0-a0", "c1", exit_code=1).splitlines()[-1])
        result["relayTaskId"] = "x"
        self.assertEqual(check_result(result, self.ORDER), [
            "unknown field relayTaskId", "required evidence tests-pass: no passing test entry"])


if __name__ == "__main__":
    unittest.main()
