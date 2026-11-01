"""One durable ledger row per finding: identity, occurrences, decisions and scope."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from factory_mcp.findings import reconcile
from factory_mcp.store import Store
from tests.fakes import FakeGitHub


class FindingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "factory.sqlite3")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, repo="acme/demo", fingerprint="slow-verify", summary="Verify runs twice",
               evidence="audit:1", at="2026-11-01T00:00:00+00:00", issue=7):
        return self.store.findings.record(repo, fingerprint, summary, evidence, at, issue=issue)

    def gh(self):
        gh = FakeGitHub()
        gh.add_issue("acme/demo", 7, state="closed")
        gh.timelines[("acme/demo", 7)] = [
            {"event": "cross-referenced", "source": {"issue": {"number": 9, "pull_request": {}}}}]
        return gh

    def config(self):
        return SimpleNamespace(owner="acme")

    def versions(self, doc):
        return [self.store.findings.version(doc["id"], v)
                for v in range(1, doc["version"] + 1)]

    def test_the_same_cause_is_one_row_with_two_occurrences(self):
        first = self.record(evidence="audit:1")
        second = self.record(evidence="audit:2", at="2026-11-08T00:00:00+00:00")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["occurrences"], 2)
        self.assertEqual(second["evidence"], ["audit:1", "audit:2"])
        self.assertEqual(second["lastSeen"], "2026-11-08T00:00:00+00:00")
        self.assertEqual(second["version"], 2)
        self.assertEqual(self.store.findings.get(second["id"]), second)

    def test_identity_is_per_repo_and_per_cause(self):
        other_repo = self.record(repo="acme/other")
        other_cause = self.record(fingerprint="flaky-test")
        self.assertNotEqual(other_repo["id"], self.record()["id"])
        self.assertNotEqual(other_cause["id"], other_repo["id"])
        self.store.findings.decide(other_cause["id"], "resolved", "fixed by the issue", "2026-11-02T00:00:00+00:00")
        self.assertEqual([doc["fingerprint"] for doc in self.store.findings.open("acme/demo")],
                         ["slow-verify"])
        self.assertEqual([doc["id"] for doc in self.store.findings.open("acme/other")],
                         [other_repo["id"]])

    def test_a_rejection_keeps_its_reason_and_history(self):
        finding = self.record()
        decided = self.store.findings.decide(finding["id"], "rejected", "No; too costly for now",
                                             "2026-11-03T00:00:00+00:00")
        self.assertEqual(decided["verdict"], "rejected")
        self.assertEqual(decided["reason"], "No; too costly for now")
        stored = self.store.findings.get(finding["id"])
        self.assertEqual(stored, decided)
        self.assertEqual(self.store.findings.version(finding["id"], 1), finding)
        self.assertEqual(self.store.findings.version(finding["id"], 2), decided)
        self.assertEqual(self.store.findings.open("acme/demo"), [])

    def test_open_lists_unfinished_findings_only(self):
        kept = self.record(fingerprint="a")
        self.record(fingerprint="b")
        rejected = self.record(fingerprint="c")
        self.record(fingerprint="d", at="2026-11-15T00:00:00+00:00")
        self.store.findings.decide(rejected["id"], "rejected", "no", "2026-11-05T00:00:00+00:00")
        opened = self.store.findings.open("acme/demo")
        self.assertEqual([doc["fingerprint"] for doc in opened], ["d", "a", "b"])
        for limit in range(1, 53):
            self.store.findings.record("acme/demo", f"bulk-{limit}", "x", f"audit:{limit}",
                                       at="2026-11-20T00:00:00+00:00")
        self.assertEqual(len(self.store.findings.open("acme/demo")), 50)
        first = self.store.findings.open("acme/demo")[0]
        self.assertEqual(first["lastSeen"], "2026-11-20T00:00:00+00:00")
        self.assertIn(kept["verdict"], ("open",))


    def test_a_merge_is_not_a_resolution(self):
        """A merged pull request lands the fix; only a quiet window or the owner resolves it."""
        finding = self.record(at="2026-11-01T00:00:00+00:00")
        gh = self.gh()
        gh.add_pr("acme/demo", 9, "fix", "sha9", state="closed", merged=True,
                  merged_at="2026-11-01T12:00:00Z")
        reconcile(self.store, gh, self.config(), "2026-11-02T00:00:00+00:00")
        stored = self.store.findings.get(finding["id"])
        self.assertEqual(stored["verdict"], "fix_landed")
        self.assertEqual(stored["fixPr"], 9)
        self.assertEqual(stored["landedAt"], "2026-11-01T12:00:00Z")
        self.assertEqual(stored["repo"], "acme/demo")
        self.assertEqual([v["verdict"] for v in self.versions(stored)], ["open", "fix_landed"])

    def test_a_quiet_window_resolves_a_landed_fix(self):
        """A fix_landed finding with no occurrence since the merge resolves after quiet_days."""
        finding = self.record(at="2026-11-01T00:00:00+00:00")
        gh = self.gh()
        gh.add_pr("acme/demo", 9, "fix", "sha9", state="closed", merged=True,
                  merged_at="2026-11-01T12:00:00Z")
        reconcile(self.store, gh, self.config(), "2026-11-02T00:00:00+00:00")
        reconcile(self.store, gh, self.config(), "2026-11-30T00:00:00+00:00")
        stored = self.store.findings.get(finding["id"])
        self.assertEqual(stored["verdict"], "resolved")
        self.assertEqual(stored["fixPr"], 9)
        self.assertEqual([v["verdict"] for v in self.versions(stored)],
                         ["open", "fix_landed", "resolved"])

    def test_a_recurrence_after_a_merge_names_the_failed_fix(self):
        """An occurrence after the merge returns the finding to open and names the fix."""
        finding = self.record(at="2026-11-01T00:00:00+00:00")
        gh = self.gh()
        gh.add_pr("acme/demo", 9, "fix", "sha9", state="closed", merged=True,
                  merged_at="2026-11-01T12:00:00Z")
        reconcile(self.store, gh, self.config(), "2026-11-02T00:00:00+00:00")
        self.record(at="2026-11-10T00:00:00+00:00")
        reconcile(self.store, gh, self.config(), "2026-11-12T00:00:00+00:00")
        stored = self.store.findings.get(finding["id"])
        self.assertEqual(stored["verdict"], "open")
        self.assertEqual(stored["failedFix"], 9)
        history = self.versions(stored)
        # a repeat pass on the same pull names the same failed fix once, not again
        reconcile(self.store, gh, self.config(), "2026-11-13T00:00:00+00:00")
        self.assertEqual(self.store.findings.get(finding["id"]), stored)
        self.assertEqual([self.store.findings.version(stored["id"], v)
                          for v in range(1, stored["version"] + 1)], history)
        # past the quiet window since the merge, the recurrence still keeps it open
        reconcile(self.store, gh, self.config(), "2026-12-05T00:00:00+00:00")
        recurred = self.store.findings.get(finding["id"])
        self.assertEqual(recurred["verdict"], "open")
        self.assertEqual(recurred["failedFix"], 9)

    def test_a_closed_issue_without_a_merge_is_a_rejection(self):
        """Only the owner's last comment is the reason, sanitized and bounded."""
        finding = self.record(at="2026-11-01T00:00:00+00:00")
        gh = self.gh()
        gh.timelines[("acme/demo", 7)] = []
        gh.issue_comments[("acme/demo", 7)] = [
            {"body": "first take", "created_at": "t1", "user": {"login": "acme"}},
            {"body": "why  'now'?\t" + "x" * 600, "created_at": "t2", "user": {"login": "helper"}},
            {"body": "Not now;  the window is closed", "created_at": "t3", "user": {"login": "acme"}}]
        reconcile(self.store, gh, self.config(), "2026-11-02T00:00:00+00:00")
        stored = self.store.findings.get(finding["id"])
        self.assertEqual(stored["verdict"], "rejected")
        self.assertEqual(stored["reason"], "Not now; the window is closed")
        self.assertLessEqual(len(stored["reason"]), 500)

    def test_unavailable_github_facts_change_nothing(self):
        """A missing issue or an unset owner leaves the row exactly as it was."""
        finding = self.record(at="2026-11-01T00:00:00+00:00")
        gh = self.gh()
        gh.issues.clear()
        reconcile(self.store, gh, self.config(), "2026-11-02T00:00:00+00:00")
        self.assertEqual(self.store.findings.get(finding["id"]), finding)
        gh = self.gh()
        gh.timelines[("acme/demo", 7)] = []
        reconcile(self.store, gh, self.config(), "2026-11-02T00:00:00+00:00")
        self.assertEqual(self.store.findings.get(finding["id"]), finding)
        gh = self.gh()
        gh.issues[("acme/demo", 7)]["state"] = "open"
        reconcile(self.store, gh, self.config(), "2026-11-02T00:00:00+00:00")
        self.assertEqual(self.store.findings.get(finding["id"]), finding)

if __name__ == "__main__":
    unittest.main()
