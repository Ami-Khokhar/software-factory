"""One durable ledger row per finding: identity, occurrences, decisions and scope."""
import tempfile
import unittest
from pathlib import Path

from factory_mcp.store import Store


class FindingsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "factory.sqlite3")
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def record(self, repo="acme/demo", fingerprint="slow-verify", summary="Verify runs twice",
               evidence="audit:1", at="2026-11-01T00:00:00+00:00"):
        return self.store.findings.record(repo, fingerprint, summary, evidence, at)

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


if __name__ == "__main__":
    unittest.main()
