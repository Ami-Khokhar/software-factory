"""Phase E: verdict comments drive CHANGES -> fix -> MERGE, with per-problem round counting."""
import json
import unittest

from factory_mcp import tools
from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
from factory_mcp.github import GitHubError
from factory_mcp.metrics import autonomy
from factory_mcp.store import Store
from factory_mcp.verdict import blockers, fingerprint
from tests.fakes import FakeGit, FakeGitHub, FakeRelay, result_line

REPO, ID = "o/r", "o/r#7"
MISSING_TEST = {"severity": "blocking", "path": "src/app.py", "line": 3, "problem": "No test for the error"}
BAD_NAME = {"severity": "blocking", "path": "src/app.py", "line": 9, "problem": "Rename x to registry"}
NIT = {"severity": "minor", "path": "README.md", "line": 1, "problem": "Typo"}


class BlockersTest(unittest.TestCase):
    def test_first_seen_round_and_resolution(self):
        def v(round_, *found):
            return {"round": round_, "findings": [dict(f, fingerprint=fingerprint(f["path"], f["problem"]))
                                                  for f in found]}
        rows = blockers([v(1, MISSING_TEST, NIT), v(2, BAD_NAME, dict(MISSING_TEST, line=40))])
        self.assertEqual([(r["firstSeenRound"], r["resolved"]) for r in rows], [(1, False), (2, False)])
        self.assertEqual([r["resolved"] for r in blockers([v(1, MISSING_TEST), v(2, BAD_NAME)])],
                         [True, False])


class GateLoopTest(unittest.TestCase):
    def setUp(self):
        self.github, self.relay = FakeGitHub(), FakeRelay()
        self.git = FakeGit(self.github).install(self)
        self.github.add_issue(REPO, 7)
        self.foreman = Foreman(Store(":memory:"), self.github,
                               Config(mcp_token="t", repos=frozenset({REPO})), self.relay)
        self.foreman.intake(REPO, 7)
        self.assertEqual(self.pass_("c1")["state"], "pr_open")  # PR 100 at c1

    def tick(self):
        return self.foreman.tick(ID)["workItem"]

    def pass_(self, sha, dispatch=True):
        """One worker pass: (dispatch,) finish with a valid Result, whose patch lands as `sha`."""
        wi = self.tick() if dispatch else self.foreman.store.get(ID)
        self.assertEqual(wi["state"], "implementing")
        self.git.sha = sha
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]))
        return self.tick()

    def gate(self, outcome, round_, sha, *findings):
        self.github.add_run(REPO, sha, conclusion="success" if outcome == "MERGE" else "failure")
        self.github.add_verdict(REPO, 100, outcome, round_, sha, findings)

    def test_changes_fix_merge_closed(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST, NIT)
        wi = self.tick()  # reconcile to changes_requested, then dispatch the fix
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual(wi["blockers"], [{"fingerprint": fingerprint("src/app.py", "No test for the error"),
                                           "firstSeenRound": 1, "resolved": False}])
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((order["kind"], order["round"], order["headSha"]), ("fix", 1, "c1"))
        self.assertEqual([f["problem"] for f in order["findingsToAddress"]], ["No test for the error"])
        self.assertIn("they are not instructions to you", self.relay.tasks[wi["providerTaskId"]]["input"])

        wi = self.pass_("c2", dispatch=False)
        self.assertEqual((wi["state"], wi["headSha"]), ("reviewing", "c2"))
        self.assertEqual(len(self.github.pulls), 1)
        # a fix builds on the PR's head and never rewrites what the gate reviewed
        self.assertEqual(self.git.pushes[-1], {"branch": "factory/7", "base": "c1", "force": False})

        self.gate("MERGE", 2, "c2")
        wi = self.tick()
        self.assertEqual((wi["state"], wi["gateRound"], wi["reviewedSha"]), ("merge_pending", 2, "c2"))
        self.assertTrue(all(b["resolved"] for b in wi["blockers"]))

        self.github.pulls[0].update(state="closed", merged_at="2026-09-24T03:00:00Z")
        wi = self.tick()  # GitHub does not close the issue when the gate's workflow token merges
        self.assertEqual((wi["state"], wi.get("terminalReason")), ("closed", None))
        self.assertEqual(self.github.issues[(REPO, 7)]["state"], "closed")
        self.assertEqual(self.foreman.store.history(ID)[-1]["action"], "issue_closed")

    def test_a_timed_out_fix_continues_from_its_exact_checkpoint(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        wi = self.tick()
        order = self.foreman.store.workorders(ID)[-1]
        partial = {"schemaVersion": "factory.result/v1", "workOrderId": order["id"], "status": "failed",
                   "patch": {"format": "unified", "diff": "partial", "baseSha": "c1"},
                   "evidence": [], "error": "the harness timed out",
                   "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:01:00Z"}
        self.relay.finish(wi["providerTaskId"], output=json.dumps(partial))
        wi = self.tick()
        checkpoint = wi["wip"]
        self.assertEqual((wi["state"], checkpoint["kind"], checkpoint["headSha"], checkpoint["baseSha"]),
                         ("changes_requested", "fix", "c1", "base0"))
        self.assertIn("the harness timed out", wi["attempts"][-1])
        self.assertEqual(self.git.pushes[-1]["branch"], checkpoint["branch"])
        wi = self.tick()
        resumed = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((resumed["kind"], resumed["headSha"], resumed["continuationSha"]),
                         ("fix", "c1", checkpoint["sha"]))

    def test_a_moved_pr_head_or_default_base_retires_a_fix_checkpoint(self):
        for moved in ("head", "base"):
            with self.subTest(moved=moved):
                self.setUp()
                self.gate("CHANGES", 1, "c1", MISSING_TEST)
                wi = self.tick()
                order = self.foreman.store.workorders(ID)[-1]
                partial = {"schemaVersion": "factory.result/v1", "workOrderId": order["id"], "status": "failed",
                           "patch": {"format": "unified", "diff": "partial", "baseSha": "c1"},
                           "evidence": [], "error": "the harness timed out",
                           "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:01:00Z"}
                self.relay.finish(wi["providerTaskId"], output=json.dumps(partial))
                wi = self.tick()
                branch = wi["wip"]["branch"]
                if moved == "head":
                    self.github.push(REPO, "factory/7", "person-head")
                else:
                    self.github.branches[(REPO, "main")] = "base1"
                wi = self.tick()
                if wi["state"] != "implementing":
                    wi = self.tick()
                resumed = self.foreman.store.workorders(ID)[-1]
                self.assertIsNone(resumed.get("continuationSha"))
                self.assertIn((REPO, branch), self.github.deleted)

    def timed_out_fix_checkpoint(self):
        """One CHANGES round whose fix pass times out, leaving a fix checkpoint. Returns the
        checkpoint record."""
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        wi = self.tick()
        order = self.foreman.store.workorders(ID)[-1]
        partial = {"schemaVersion": "factory.result/v1", "workOrderId": order["id"], "status": "failed",
                   "patch": {"format": "unified", "diff": "partial", "baseSha": "c1"},
                   "evidence": [], "error": "the harness timed out",
                   "startedAt": "2026-09-24T00:00:00Z", "finishedAt": "2026-09-24T00:01:00Z"}
        self.relay.finish(wi["providerTaskId"], output=json.dumps(partial))
        wi = self.tick()
        return wi["wip"]

    def test_a_running_continuation_keeps_its_checkpoint_when_the_default_base_moves(self):
        checkpoint = self.timed_out_fix_checkpoint()
        wi = self.tick()  # dispatch the continuation: the worker holds the checkpoint as its base
        self.assertEqual((wi["state"], self.foreman.store.workorders(ID)[-1]["continuationSha"]),
                         ("implementing", checkpoint["sha"]))
        self.github.branches[(REPO, "main")] = "base1"  # the default base moves while the pass runs
        wi = self.tick()
        self.assertEqual((wi["state"], wi["wip"]["sha"]), ("implementing", checkpoint["sha"]))
        self.assertNotIn((REPO, checkpoint["branch"]), self.github.deleted)  # the base still exists
        self.git.sha = "c3"
        self.relay.finish(wi["providerTaskId"], output=result_line(wi["activeWorkOrderId"]))
        wi = self.tick()
        self.assertEqual((wi["state"], wi.get("wip")), ("reviewing", None))
        self.assertEqual(self.git.pushes[-1]["base"], checkpoint["sha"])  # applied on its exact base

    def merge(self, head):
        """The gate decides MERGE on `head` and PR 100 merges. Returns whether the WorkItem counts as
        merged with no person."""
        self.gate("MERGE", 1, head)
        self.tick()
        self.github.pulls[0].update(state="closed", merged_at="2026-09-24T03:00:00Z")
        wi = self.tick()
        self.assertEqual(wi["state"], "closed")
        return autonomy([wi], self.foreman.store.history)["autonomous"] == 1

    def test_the_gates_merge_of_the_base_branch_is_not_a_person(self):
        """ADR-0039: the gate merges main into a PR that fell behind it, then reviews that head."""
        self.github.gate_merges_base(REPO, "factory/7", "m1")
        self.assertTrue(self.merge("m1"))

    def test_a_commit_a_person_pushed_is_a_person(self):
        self.github.push(REPO, "factory/7", "p1")
        self.assertFalse(self.merge("p1"))

    def test_a_head_github_will_not_show_counts_as_a_persons(self):
        """A 403 or 404 counts the head as a person's; any other error fails the tick, which tries again."""
        self.github.gate_merges_base(REPO, "factory/7", "m1")
        self.refuse("commit", status=502)
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.foreman.store.history(ID)[-1]["action"], "tick_error")
        self.refuse("commit", status=404)
        self.assertFalse(self.merge("m1"))

    def test_a_fix_that_adds_a_file_lists_it_in_the_pr_body(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        self.tick()
        self.git.files = (("src/app.py", 10, 2), ("tests/test_error.py", 12, 0))
        self.assertEqual(self.pass_("c2", dispatch=False)["state"], "reviewing")
        body = self.github.pulls[0]["body"]
        self.assertIn("- `tests/test_error.py`: modified (+12 -0)", body)
        self.assertIn(f"WorkOrder `{self.foreman.store.workorders(ID)[-1]['id']}`", body)

    def test_a_body_that_cannot_be_edited_leaves_the_push_standing(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        self.tick()
        def refused(repo, number, body):
            raise GitHubError(403, "Resource not accessible")
        self.github.edit_pull = refused
        wi = self.pass_("c2", dispatch=False)
        self.assertEqual((wi["state"], wi["headSha"]), ("reviewing", "c2"))
        self.assertEqual(self.foreman.store.history(ID)[-1]["action"], "pr_body_failed")

    def test_the_reconcile_audit_keeps_the_accepted_verdicts_blocking_findings(self):
        """lessons.candidates reads these entries, so they hold the blocking findings only."""
        self.gate("CHANGES", 1, "c1", MISSING_TEST, NIT)
        self.tick()
        recorded = [h["detail"]["blocking"] for h in self.foreman.store.history(ID)
                    if h["action"] == "reconcile" and "blocking" in h["detail"]]
        self.assertEqual(recorded, [[{"fingerprint": fingerprint("src/app.py", "No test for the error"),
                                      "problem": "No test for the error"}]])

    def test_a_verdict_with_only_minor_findings_records_no_blocking_finding(self):
        self.gate("CHANGES", 1, "c1", NIT)
        self.tick()
        recorded = [h["detail"]["blocking"] for h in self.foreman.store.history(ID)
                    if h["action"] == "reconcile" and "blocking" in h["detail"]]
        self.assertEqual(recorded, [[]])

    def test_an_issue_github_closed_on_merge_is_not_closed_again(self):
        self.gate("MERGE", 1, "c1")
        self.tick()
        self.github.pulls[0].update(state="closed", merged_at="2026-09-24T03:00:00Z")
        self.github.issues[(REPO, 7)]["state"] = "closed"
        self.github.close_issue = None  # any call fails the test
        self.assertEqual(self.tick()["state"], "closed")

    def test_merge_without_a_merge_verdict_is_flagged(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        self.github.pulls[0].update(state="closed", merged_at="2026-09-24T03:00:00Z")  # by a person
        wi = self.tick()
        self.assertEqual(wi["state"], "closed")
        self.assertEqual(wi["terminalReason"], "merged without a pr-gatekeeper MERGE verdict on c1")

    def test_same_problem_twice_goes_to_a_human(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        self.tick()
        self.pass_("c2", dispatch=False)
        self.gate("CHANGES", 2, "c2", dict(MISSING_TEST, line=12))
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("No test for the error", wi["blockedOn"])

    def test_new_problems_each_round_keep_going(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        self.tick()
        self.pass_("c2", dispatch=False)
        self.gate("CHANGES", 2, "c2", BAD_NAME)
        self.assertEqual(self.tick()["state"], "implementing")
        self.assertEqual(self.foreman.store.workorders(ID)[-1]["idempotencyKey"], "o/r#7:r2:fix:a0")

    def test_push_after_a_failed_fix_waits_for_the_gate(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        wi = self.tick()
        self.relay.finish(wi["providerTaskId"], status="failed", error="crashed")
        self.assertEqual(self.tick()["state"], "changes_requested")
        self.github.push(REPO, "factory/7", "c9")  # a person fixed it by hand
        self.assertEqual(self.tick()["state"], "reviewing")

    def test_fix_attempts_that_ran_out_of_time_are_not_told_to_split_the_issue(self):
        """A fix answers the gate on an open PR, which closing the issue for a split would drop."""
        self.foreman.set_timeout(ID, 3600000, "owner")  # already at the longest run: no ADR-0108 run
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        wi = self.tick()
        self.relay.finish(wi["providerTaskId"], status="timed_out", error="timed out")
        self.assertEqual(self.tick()["state"], "changes_requested")
        wi = self.tick()
        self.relay.finish(wi["providerTaskId"], status="timed_out", error="timed out")
        wi = self.tick()
        self.assertEqual(wi["state"], "blocked")
        self.assertIn("Fix the cause named in attempts", wi["blockedOn"])

    def refuse(self, *names, status=403):
        """GitHub answers each of these reads with `status`, as it does a fine-grained token on a
        private repo."""
        def refused(*args, **kwargs):
            raise GitHubError(status, "Resource not accessible by personal access token")
        for name in names:
            setattr(self.github, name, refused)

    def test_an_unreadable_check_run_falls_back_to_the_gates_workflow_run(self):
        self.refuse("check_runs")
        self.github.workflow_run_list[(REPO, "c1")] = [  # newest first: an edit cancelled the older run
            {"name": "pr-gatekeeper", "status": "completed", "conclusion": "cancelled"},
            {"name": "pr-gatekeeper", "status": "in_progress", "conclusion": None}]
        self.assertEqual(self.tick()["state"], "reviewing")

    def test_with_no_gate_run_readable_the_verdict_still_moves_the_workitem(self):
        self.refuse("check_runs", "workflow_runs")
        self.github.add_verdict(REPO, 100, "CHANGES", 1, "c1", [MISSING_TEST])
        wi = self.tick()
        self.assertEqual((wi["state"], self.foreman.store.workorders(ID)[-1]["kind"]), ("implementing", "fix"))

    def test_any_other_error_reading_the_gate_run_fails_the_tick(self):
        self.refuse("check_runs", status=500)
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.foreman.store.history(ID)[-1]["action"], "tick_error")

    def test_any_other_error_reading_the_workflow_run_fails_the_tick(self):
        self.refuse("check_runs")
        self.refuse("workflow_runs", status=500)
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.foreman.store.history(ID)[-1]["action"], "tick_error")

    def test_a_cancelled_check_run_is_not_the_gates_answer(self):
        self.github.runs[(REPO, "c1")] = [{"name": "pr-gatekeeper", "status": "completed", "conclusion": "cancelled"}]
        self.assertEqual(self.tick()["state"], "pr_open")  # not human_waiting: the next run answers

    def conflict(self):
        self.github.branches[(REPO, "main")] = "m2"  # main moved on, and the PR no longer merges
        self.github.pulls[0]["mergeable"] = False

    def test_a_pr_that_conflicts_with_main_is_made_again_on_the_current_main(self):
        self.conflict()
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing")
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((order["kind"], order["baseSha"]), ("implement", "m2"))
        self.assertEqual([f["problem"] for f in order["findingsToAddress"]],
                         ["PR #100 conflicts with main, which changed after this branch was made. "
                          "Make the change again on the current main."])
        wi = self.pass_("c2", dispatch=False)
        self.assertEqual((wi["state"], wi["prNumber"], len(self.github.pulls)), ("reviewing", 100, 1))
        self.assertEqual(self.git.pushes[-1], {"branch": "factory/7", "base": "m2", "force": True})
        self.assertEqual((self.tick()["state"], len(self.foreman.store.workorders(ID))), ("reviewing", 2))

    def test_a_conflicting_pr_with_gate_findings_is_made_again_with_them(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        self.conflict()
        self.tick()
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((order["kind"], order["round"]), ("implement", 1))
        self.assertEqual([f["fingerprint"] for f in order["findingsToAddress"]],
                         ["merge-conflict", fingerprint("src/app.py", "No test for the error")])

    def test_a_failed_redo_of_a_conflicting_pr_keeps_the_gate_findings_on_the_retry(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        self.conflict()
        wi = self.tick()
        self.relay.finish(wi["providerTaskId"], status="failed", error="crashed")
        self.tick()  # the attempt fails; the next tick sees the same PR head, still conflicting
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing")
        self.assertEqual([f["fingerprint"] for f in self.foreman.store.workorders(ID)[-1]["findingsToAddress"]],
                         ["merge-conflict", fingerprint("src/app.py", "No test for the error"), "previous-attempt"])

    def test_a_verdict_on_an_older_head_does_not_ride_on_a_redo(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        wi = self.tick()
        self.relay.finish(wi["providerTaskId"], status="failed", error="crashed")
        self.tick()
        self.github.push(REPO, "factory/7", "c9")  # a person fixed it by hand; the gate has not seen c9
        self.assertEqual(self.tick()["state"], "reviewing")
        self.conflict()
        self.tick()
        found = [f["fingerprint"] for f in self.foreman.store.workorders(ID)[-1]["findingsToAddress"]]
        self.assertEqual(found[0], "merge-conflict")
        self.assertNotIn(fingerprint("src/app.py", "No test for the error"), found)

    def test_a_live_head_changes_verdict_survives_a_later_comment_for_another_head(self):
        self.gate("CHANGES", 1, "c1", BAD_NAME)
        self.tick()
        self.pass_("c2")
        self.gate("CHANGES", 2, "c2", MISSING_TEST)
        self.gate("MERGE", 1, "c1")  # a delayed comment from the earlier head

        wi = self.tick()

        self.assertEqual(wi["state"], "implementing")
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((order["kind"], order["round"], order["headSha"]), ("fix", 2, "c2"))
        self.assertEqual([f["problem"] for f in order["findingsToAddress"]], [MISSING_TEST["problem"]])
        blocker = next(b for b in wi["blockers"]
                       if b["fingerprint"] == fingerprint("src/app.py", "No test for the error"))
        self.assertFalse(blocker["resolved"])

    def test_a_pr_that_cannot_be_read_on_its_own_is_not_taken_as_conflicting(self):
        def gone(repo, number):
            raise GitHubError(404, "Not Found")
        self.github.pull = gone
        self.github.add_verdict(REPO, 100, "CHANGES", 1, "c1", [MISSING_TEST])
        self.assertEqual(self.foreman.store.workorders(ID)[-1]["kind"], "implement")
        wi = self.tick()  # the verdict is still taken in
        self.assertEqual((wi["state"], self.foreman.store.workorders(ID)[-1]["kind"]), ("implementing", "fix"))

    def test_any_other_error_reading_the_pr_fails_the_tick(self):
        def down(repo, number):
            raise GitHubError(502, "Bad Gateway")
        self.github.pull = down
        self.assertEqual(self.tick()["state"], "pr_open")
        self.assertEqual(self.foreman.store.history(ID)[-1]["action"], "tick_error")

    def test_changes_with_no_finding_go_to_a_person_not_a_worker(self):
        self.gate("CHANGES", 1, "c1")  # only the gate's PR lint failed: its JSON names no finding
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("no finding a worker could fix", wi["blockedOn"])
        self.assertEqual((wi["blockers"], wi["gateRound"], wi["reviewedSha"]), ([], 1, "c1"))
        self.assertEqual(len(self.foreman.store.workorders(ID)), 1)
        self.foreman.resolve_human(ID, "retry", "List tests/test_error.py under What changed", "amteshwar")
        self.tick()
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((order["kind"], [f["fingerprint"] for f in order["findingsToAddress"]]), ("fix", ["human"]))

    def gate_problems(self, round_, sha, problems):
        """A gate verdict whose marker names pipeline problems and no findings, as pr-gatekeeper
        writes when verify, pr-lint or the proof of work failed."""
        payload = json.dumps({"round": round_, "outcome": "CHANGES", "head_sha": sha,
                              "findings": [], "problems": problems}).replace(">", "\\u003e")
        self.github.issue_comments.setdefault((REPO, 100), []).append(
            {"body": f"<!-- pr-gatekeeper {payload} -->", "created_at": "2026-09-24T02:00:00Z",
             "user": {"login": "github-actions[bot]"}})
        self.github.add_run(REPO, sha, conclusion="failure")

    def test_a_changes_finding_with_fix_and_test_reaches_the_fix_workorder(self):
        self.gate("CHANGES", 1, "c1", dict(MISSING_TEST, fix="Assert the error path in the test",
                                           test="tests/test_error.py"))
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing")
        addressed = self.foreman.store.workorders(ID)[-1]["findingsToAddress"][0]
        self.assertEqual((addressed["fix"], addressed["test"]),
                         ("Assert the error path in the test", "tests/test_error.py"))

    def test_a_changes_marker_with_problems_and_no_findings_dispatches_a_fix_that_carries_them(self):
        self.gate_problems(1, "c1", [{"kind": "lint", "path": "src/app.py", "problem": "Line 9 is too long"}])
        wi = self.tick()
        self.assertEqual(wi["state"], "implementing")
        order = self.foreman.store.workorders(ID)[-1]
        self.assertEqual((order["kind"], [f["problem"] for f in order["findingsToAddress"]]),
                         ("fix", ["Line 9 is too long"]))
        self.assertEqual([f["fingerprint"] for f in order["findingsToAddress"]],
                         [fingerprint("src/app.py", "Line 9 is too long")])

    def test_a_changes_marker_without_problems_carries_no_fix_or_test_it_did_not_name(self):
        self.gate("CHANGES", 1, "c1", MISSING_TEST)
        self.tick()
        addressed = self.foreman.store.workorders(ID)[-1]["findingsToAddress"][0]
        self.assertEqual((addressed["fix"], addressed["test"]), (None, None))

    def test_the_same_problem_reported_as_a_problem_in_two_rounds_escalates(self):
        problems = [{"kind": "lint", "path": "src/app.py", "problem": "Line 9 is too long"}]
        self.gate_problems(1, "c1", problems)
        self.assertEqual(self.tick()["state"], "implementing")
        self.pass_("c2", dispatch=False)
        self.gate_problems(2, "c2", problems)
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("Line 9 is too long", wi["blockedOn"])

    def test_gate_human_verdict(self):
        self.gate("HUMAN", 1, "c1")
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("pr-gatekeeper decided HUMAN", wi["blockedOn"])


    def test_merge_after_a_human_verdict_drops_blocked_on(self):
        self.gate("HUMAN", 1, "c1")
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.github.pulls[0].update(state="closed", merged_at="2026-09-24T03:00:00Z")
        wi = self.tick()
        self.assertEqual(wi["state"], "closed")
        self.assertIsNone(wi["blockedOn"])
        rows = tools.build(self.foreman.store, self.foreman)["factory_status"].fn({})["workItems"]
        self.assertIsNone([r for r in rows if r["id"] == ID][0]["blockedOn"])

if __name__ == "__main__":
    unittest.main()
