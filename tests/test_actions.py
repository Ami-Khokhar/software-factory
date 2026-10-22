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
from tests.fakes import ISSUE_BODY, FakeGit, FakeGitHub, FakeRelay, result_line

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

    def test_the_worker_reads_the_issue_description(self):
        _, task = self.dispatched()
        order = json.loads(task["input"].split("WorkOrder:\n", 1)[1])
        self.assertEqual(order["description"], "Make the relay fail loud.")

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
        # factory-mcp pushed the patch (ADR-0017): built on the default branch's head, over any earlier attempt
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

    def test_a_patch_is_applied_on_the_commit_it_names(self):
        """worker/runner.py names the commit the harness built on; the WorkOrder's base is the fallback."""
        wi, task = self.dispatched()
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"], base_sha="b7"))
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.git.pushes[0]["base"], "b7")

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
        """ADR-0070 over ADR-0002: the line count is a small-PR target, not a wall — a whole-PR
        exceedance warns and still opens the pull request; the file count binds, as do the
        protected paths."""
        cases = [("600 lines", (("src/app.py", 590, 10),), "pr_open"),
                 ("601 lines", (("src/app.py", 591, 10),), "pr_open"),
                 ("610 lines, the previous fix's size", (("src/app.py", 601, 9),), "pr_open"),
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
                    self.assertIn("exceeds the WorkOrder constraint of 20 files", wi["attempts"][0])

    def test_an_advisory_lines_exceedance_is_recorded_not_failed(self):
        """ADR-0079: a source-line exceedance over the opened pull request is noted in the audit
        log, never as an attempt failure: no rejection for source count alone under the ceiling."""
        wi, task = self.dispatched()
        self.git.files = (("src/app.py", 691, 10),)  # 701 source lines, over the 700 target
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual(wi["state"], "pr_open", wi.get("attempts"))
        noted = [row["detail"] for row in self.foreman.store.history(ID)
                 if row["action"] == "pr_size_advisory"]
        self.assertEqual(noted, [{"source": 701, "tests": 0, "docs": 0, "total": 701,
                                  "target": 700, "files": 1}])

    def test_a_target_only_workorder_uses_the_reviewable_ceiling(self):
        """A WorkOrder with maxSourceChangedLines but no ceiling keeps the documented default of
        1000: 701 source lines is advisory and pushes, while 1001 is a size-only recovery. Without
        the default a target+1 became a ceiling error instead of the advisory (finding a391ed41)."""
        wi, _ = self.dispatched()
        order = self.foreman.store.workorders(ID)[0]
        order["constraints"].pop("maxSourceChangedLinesCeiling")
        self.github.push(REPO, wi["branch"], "c701", files=(("src/app.py", 691, 10),))
        errors, _ = self.foreman.check_pushed(wi, order, "c701")
        self.assertEqual(errors, [])
        noted = [row["detail"] for row in self.foreman.store.history(ID)
                 if row["action"] == "pr_size_advisory"]
        self.assertEqual(noted[-1]["source"], 701)
        self.github.push(REPO, wi["branch"], "c1001", files=(("src/big.py", 1001, 0),))
        errors, _ = self.foreman.check_pushed(wi, order, "c1001")
        self.assertTrue(any("source-size-ceiling" in e for e in errors), errors)

    def test_a_source_ceiling_excess_routes_to_a_decomposition_not_an_attempt(self):
        """ADR-0079: a size-only source excess makes no attempt failure and opens no PR: the
        Foreman records an acceptance-preserving decomposition and asks the owner to activate it."""
        wi, task = self.dispatched()
        self.git.files = (("src/big.py", 1001, 0),)  # 1001 source lines, over the 1000 ceiling
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting", wi.get("attempts"))
        self.assertIn("acceptance-preserving", wi["blockedOn"])
        self.assertEqual(wi["attempts"], [])
        self.assertEqual(self.github.pulls, [])
        rows = [row for row in self.foreman.store.history(ID) if row["action"] == "size_recovery"]
        self.assertEqual(len(rows), 1)
        self.assertEqual([s["claim"] for s in rows[0]["detail"]["slices"]],
                         ["default_config_path() raises when the user registry is missing",
                          "README names the registry path"])

    def test_an_allowance_never_buys_a_planned_decomposition_without_the_owner(self):
        """ADR-0079 with ADR-0069: an approved replacement allowance is recorded, not spent; new
        issue bodies are a material scope change, so the owner still activates the plan."""
        wi, task = self.dispatched()
        self.foreman.approve_plan_allowance("plan-1", ["replace"], 3, "owner", REPO, items=[ID])
        self.git.files = (("src/big.py", 1001, 0),)
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertEqual(self.foreman.store.allowance("plan-1")["remaining"], 3)
        self.assertEqual(len(self.github.issues), 1)  # no replacement issue was opened
        rows = [row for row in self.foreman.store.history(ID) if row["action"] == "size_recovery"]
        self.assertTrue(rows[0]["detail"]["authorized"])

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
        self.assertIn("Fix the cause named in attempts", wi["blockedOn"])  # one ran out of time, not each

    def test_a_timeout_that_the_diagnostics_do_not_support_stays_unknown(self):
        """#190: the runner's harness timeout, then the relay's task timeout. A pass that ran out of
        time is not by itself an issue too big for one pass; the cause is unknown, so the blocked
        guidance asks what it asks of any two failures."""
        self.foreman.set_timeout(ID, 3600000, "owner")  # already at the longest run: no ADR-0108 run
        wi, task = self.dispatched()
        self.relay.finish(task["id"], output=json.dumps({
            "schemaVersion": "factory.result/v1", "workOrderId": wi["activeWorkOrderId"], "status": "failed",
            "error": "the harness timed out", "evidence": [],
            "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:20:00Z"}))
        self.assertEqual(self.tick()["state"], "ready")
        wi, task = self.dispatched()
        self.relay.finish(task["id"], status="timed_out", error="timed out")
        wi = self.tick()
        self.assertEqual(wi["state"], "blocked")
        self.assertNotIn("too big", wi["blockedOn"])
        self.assertNotIn("split", wi["blockedOn"])
        self.assertIn("Fix the cause named in attempts", wi["blockedOn"])

    def test_a_lost_relay_task_is_requeued_without_a_strike(self):
        """ADR-0081: an accepted task the relay no longer knows is an infrastructure loss. The same
        WorkOrder is re-queued under the same idempotency key, no attempt is recorded, and a
        successful Result resets the count."""
        wi, task = self.dispatched()
        order_id = wi["activeWorkOrderId"]
        self.relay.restart()  # the relay's tasks and request ids live in memory only
        wi = self.tick()
        self.assertEqual(wi["state"], "dispatching")
        self.assertEqual(wi["attempts"], [])
        self.assertEqual(wi["infraRetries"], 1)
        self.assertTrue(wi["recovery"]["action"] == "relay_lost")
        wi = self.tick()  # the same WorkOrder goes out once more
        self.assertEqual((wi["state"], wi["activeWorkOrderId"]), ("implementing", order_id))
        self.assertEqual(list(self.relay.by_request), [order_id])
        self.assertEqual([o["id"] for o in self.foreman.store.workorders(ID)], [order_id])
        self.relay.finish(wi["providerTaskId"], output=result_line(order_id))
        wi = self.tick()
        self.assertEqual((wi["state"], wi["attempts"], wi["infraRetries"]), ("pr_open", [], 0))
        results = [h for h in self.foreman.store.history(ID) if h["action"] == "recovery_result"]
        self.assertEqual([(r["detail"]["action"], r["detail"]["succeeded"]) for r in results],
                         [("relay_lost", True)])

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


class PathsFenceTest(unittest.TestCase):
    """ADR-0087: an issue's `Paths:` line fences the source files its pass may change, and a pushed
    source change outside them fails the attempt. Tests and docs can change anywhere."""

    body = ISSUE_BODY.replace("## Acceptance", "Paths: factory_mcp/*\n\n## Acceptance")

    def setUp(self):
        self.fence(self.body)

    def fence(self, body):
        """A foreman whose issue carries this body."""
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
        self.github.add_issue(REPO, 7, body=body)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.foreman.intake(REPO, 7)

    def tick(self):
        wi = self.foreman.tick(ID)["workItem"]
        self.assertEqual(schema_errors("workitem", wi), [])
        return wi

    def dispatched(self):
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing")
        return wi, self.relay.tasks[wi["providerTaskId"]]

    def test_a_pushed_source_change_outside_the_fence_fails_the_attempt_and_names_the_file(self):
        wi, task = self.dispatched()
        self.assertEqual(self.foreman.store.workorders(ID)[0]["constraints"]["allowedPaths"],
                         ["factory_mcp/*"])
        self.git.files = (("src/outside.py", 10, 0),)
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual(wi["state"], "ready", wi.get("attempts"))
        self.assertIn("changes src/outside.py, outside this node's Paths", wi["attempts"][0])

    def test_a_test_or_doc_file_outside_the_fence_is_accepted(self):
        wi, task = self.dispatched()
        self.git.files = (("factory_mcp/new.py", 10, 0), ("tests/extra_test.py", 4, 0),
                          ("docs/extra.md", 3, 0))
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        self.assertEqual(self.tick()["state"], "pr_open")

    def test_an_issue_without_paths_runs_unfenced(self):
        self.fence(ISSUE_BODY)
        wi, task = self.dispatched()
        self.assertNotIn("allowedPaths", self.foreman.store.workorders(ID)[0]["constraints"])
        self.git.files = (("src/anywhere.py", 10, 0),)
        self.relay.finish(task["id"], output=result_line(wi["activeWorkOrderId"]))
        self.assertEqual(self.tick()["state"], "pr_open")

    def test_the_workers_rules_list_the_fence(self):
        _, task = self.dispatched()
        self.assertIn("change source only inside them: factory_mcp/*", task["input"])


if __name__ == "__main__":
    unittest.main()
