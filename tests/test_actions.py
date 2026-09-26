"""The Foreman's actions and guards, driven tick by tick against fakes. Every WorkItem and WorkOrder
it writes must validate against its schema."""
import dataclasses
import json
import unittest
from pathlib import Path

from jsonschema import Draft202012Validator

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.mcp import ToolError
from factory_mcp.store import Store
from tests.fakes import FakeGit, FakeGitHub, FakeRelay, result_line

REPO, ID = "o/r", "o/r#7"
ROOT = Path(__file__).resolve().parent.parent


def schema_errors(name, doc):
    schema = json.loads((ROOT / "schemas" / f"{name}.schema.json").read_text())
    return [e.message for e in Draft202012Validator(schema).iter_errors(doc)]


class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
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
        self.assertEqual(wi["worker"], "codex")

        self.relay.finish(task["id"], output=result_line(order["id"]))
        wi = self.tick()
        self.assertEqual((wi["state"], wi["prNumber"], wi["headSha"]), ("pr_open", 100, "c1"))
        # factory-mcp pushed the patch (ADR-0016): built on the default branch's head, over any earlier attempt
        self.assertEqual(self.git.pushes, [{"branch": "factory/7", "base": "base0", "force": True}])
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
        self.git.files = ({"filename": "docs/agents.md", "previous_filename": "AGENTS.md", "status": "renamed",
                           "additions": 0, "deletions": 0},)
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual(wi["state"], "ready")
        self.assertIn("changes protected path AGENTS.md", wi["attempts"][0])

    def test_a_change_must_fit_one_pr(self):
        """ADR-0002: a PR stays within 600 changed lines and 20 files, the gate's limits."""
        cases = [("600 lines", (("src/app.py", 590, 10),), "pr_open"),
                 ("601 lines", (("src/app.py", 591, 10),), "ready"),
                 ("20 files", tuple((f"src/m{n}.py", 1, 0) for n in range(20)), "pr_open"),
                 ("21 files", tuple((f"src/m{n}.py", 1, 0) for n in range(21)), "ready")]
        for case, files, state in cases:
            with self.subTest(case):
                self.setUp()
                wi, task = self.dispatched()
                self.git.files = files
                self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
                wi = self.tick()
                self.assertEqual(wi["state"], state, wi.get("attempts"))
                if state == "ready":
                    self.assertIn("exceeds the WorkOrder constraints", wi["attempts"][0])

    def test_a_result_that_reports_failed_is_an_attempt(self):
        """How worker/runner.py reports a failing verify: the task completes, and its Result says failed."""
        wi, task = self.dispatched()
        self.relay.finish(task["id"], output=json.dumps({
            "schemaVersion": "factory.result/v1", "workOrderId": wi["activeWorkOrderId"], "status": "failed",
            "error": "verify exited 1: FAILED (failures=2)",
            "evidence": [{"kind": "test", "command": "make test", "exit": 1, "tail": "FAILED"}],
            "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:10:00Z"}))
        wi = self.tick()
        self.assertEqual(wi["state"], "ready")
        self.assertIn("worker reported failed: verify exited 1: FAILED (failures=2)", wi["attempts"][0])
        self.assertEqual(self.github.pulls, [])

    def test_a_result_for_a_branch_with_an_open_pr_joins_that_pr(self):
        """ADR-0003: the Foreman adopts the PR on the branch instead of opening a second one."""
        wi, task = self.dispatched()
        self.github.add_pr(REPO, 55, "factory/7", "c0")  # opened while the worker ran
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual((wi["state"], wi["prNumber"], wi["headSha"]), ("reviewing", 55, "c1"))
        self.assertEqual(len(self.github.pulls), 1)

    def test_open_pr_adopts_a_pr_opened_after_the_reconcile(self):
        wi, _ = self.dispatched()
        self.github.add_pr(REPO, 55, "factory/7", "c1")
        pr = self.foreman.open_pr(wi, self.foreman.store.workorders(ID)[0], [], [], "codex")
        self.assertEqual((pr["number"], len(self.github.pulls)), (55, 1))

    def test_a_repo_off_the_allowlist_is_left_alone(self):
        """Taking a repo off FACTORY_REPOS stops the factory acting on it; putting it back resumes,
        in any case: GitHub repo names ignore case."""
        allowlisted = dataclasses.replace(self.foreman.config, repos=frozenset({REPO.upper()}))
        off = Config(mcp_token="t", repos=frozenset())
        self.foreman.config = off
        self.foreman.tick()
        self.assertEqual((self.foreman.store.get(ID)["state"], self.relay.tasks), ("ready", {}))
        with self.assertRaisesRegex(ToolError, "o/r is not in FACTORY_REPOS"):
            self.foreman.tick(ID)

        self.foreman.config = allowlisted
        wi, task = self.dispatched()
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        self.foreman.config = off
        self.foreman.tick()
        self.assertEqual((self.foreman.store.get(ID)["state"], self.github.pulls), ("implementing", []))
        self.foreman.config = allowlisted
        self.assertEqual(self.tick()["state"], "pr_open")

    def test_bad_results_fail_attempts_then_block(self):
        wi, task = self.dispatched()
        self.git.files = ((".github/workflows/x.yml", 1, 0),)
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual(wi["state"], "ready")
        self.assertIn("changes protected path .github/workflows/x.yml", wi["attempts"][0])

        wi, task = self.dispatched()
        self.assertEqual(wi["activeWorkOrderId"], "o/r#7:r0:implement:a1")
        self.git.sha, self.git.lands = "c2", False  # GitHub still shows the first attempt's c1
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual(wi["state"], "blocked")
        self.assertIn("factory/7 is at c1, not the pushed c2", wi["attempts"][1])
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
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        self.tick()
        self.github.add_verdict(REPO, 100, "MERGE", 1, "c0-older")
        self.assertEqual(self.tick()["state"], "pr_open")


if __name__ == "__main__":
    unittest.main()
