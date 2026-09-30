"""Phase E: verdict comments drive CHANGES -> fix -> MERGE, with per-problem round counting."""
import unittest

from factory_mcp.config import Config
from factory_mcp.foreman import Foreman
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

    def test_gate_human_verdict(self):
        self.gate("HUMAN", 1, "c1")
        wi = self.tick()
        self.assertEqual(wi["state"], "human_waiting")
        self.assertIn("pr-gatekeeper decided HUMAN", wi["blockedOn"])


if __name__ == "__main__":
    unittest.main()
