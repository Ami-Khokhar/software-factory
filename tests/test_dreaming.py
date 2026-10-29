"""Persistent eligibility, failure cooldown, bounded windows and concurrent claims."""
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from factory_mcp.config import Config
from factory_mcp.memory import events
from factory_mcp.store import Store
from tests.fakes import workitem

AT = datetime(2026, 10, 28, tzinfo=timezone.utc)


class DreamingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "factory.sqlite3")
        self.store = Store(self.path)
        self.config = Config(mcp_token="owner", repos=frozenset({"o/r"}))
        self.store.put(workitem())

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def evidence(self, reason="test failed"):
        self.store.log("attempt_failed", {"reason": reason}, "o/r#1")

    def test_no_evidence_never_claims_or_starts_cooldown(self):
        self.assertIsNone(self.store.dreaming.claim(self.config, AT))
        self.assertEqual(self.store.dreaming.status(self.config, AT)["reason"], "no_evidence")
        self.assertIsNone(self.store.dreaming.latest())

    def test_cooldown_survives_restart_failure_and_exact_boundary(self):
        self.evidence()
        run = self.store.dreaming.claim(self.config, AT)
        self.store.dreaming.settle(run["id"], "failed", {"error": "model unavailable"})
        self.store.close()
        self.store = Store(self.path)
        self.assertIsNone(self.store.dreaming.claim(self.config, AT + timedelta(days=6, hours=23)))
        next_run = self.store.dreaming.claim(self.config, AT + timedelta(days=7))
        self.assertEqual(next_run["evidence"], run["evidence"])
        self.assertNotEqual(next_run["id"], run["id"])

    def test_concurrent_connections_claim_only_once(self):
        self.evidence()
        other = Store(self.path)
        barrier, results = threading.Barrier(2), []
        def claim(store):
            barrier.wait()
            results.append(store.dreaming.claim(self.config, AT))
        threads = [threading.Thread(target=claim, args=(store,)) for store in (self.store, other)]
        try:
            for thread in threads: thread.start()
            for thread in threads: thread.join(timeout=5)
            self.assertEqual(len(results), 2)
            self.assertEqual(sum(result is not None for result in results), 1)
        finally:
            other.close()

    def test_crashed_claim_waits_seven_days_and_preserves_evidence(self):
        self.evidence()
        first = self.store.dreaming.claim(self.config, AT)
        self.assertIsNone(self.store.dreaming.claim(self.config, AT + timedelta(days=1)))
        second = self.store.dreaming.claim(self.config, AT + timedelta(days=7))
        self.assertEqual(first["evidence"], second["evidence"])

    def test_completed_windows_leave_new_and_overflow_events_pending(self):
        for n in range(50): self.evidence(str(n) + " x" * 200)
        run = self.store.dreaming.claim(self.config, AT)
        self.assertLessEqual(len(run["evidence"]), 20)
        self.store.dreaming.settle(run["id"], "complete", {"summary": "no actionable defect"})
        self.evidence("new event")
        next_run = self.store.dreaming.claim(self.config, AT + timedelta(days=7))
        self.assertTrue(next_run["evidence"])
        self.assertGreater(next_run["evidence"][0]["seq"], run["evidence"][-1]["seq"])

    def test_pending_publication_cannot_start_another_reflection(self):
        self.evidence()
        run = self.store.dreaming.claim(self.config, AT)
        self.store.dreaming.settle(run["id"], "planned", {"candidates": []})
        self.evidence("later")
        self.assertIsNone(self.store.dreaming.claim(self.config, AT + timedelta(days=8)))
        self.assertEqual(self.store.dreaming.status(self.config, AT + timedelta(days=8))["reason"],
                         "publication_pending")

    def test_busy_repos_share_the_window_and_deferred_repos_lead_after_restart(self):
        import json
        repos = {f"o/r{n:02}" for n in range(30)}
        config = Config(mcp_token="owner", repos=frozenset(repos))
        sequences = {}
        for repo in sorted(repos):
            wid = repo + "#1"
            self.store.put(dict(workitem(), id=wid, repo=repo))
            for _ in range(2):
                self.store.log("attempt_failed", {"reason": "x" * 300, "diagnosis": "y" * 300}, wid)
            sequences[repo] = [row["seq"] for row in events(self.store, config, {"repo": repo})["events"]]
        first = self.store.dreaming.claim(config, AT)
        included = {event["repo"] for event in first["evidence"]}
        self.assertGreater(len(included), 1)
        self.assertLess(len(included), len(repos))
        self.assertLessEqual(sum(len(json.dumps(event).encode()) for event in first["evidence"]), 12000)
        for repo, cursor in first["cursors"].items():
            self.assertEqual([event["seq"] for event in first["evidence"] if event["repo"] == repo],
                             [seq for seq in sequences[repo] if seq <= cursor])
        self.store.dreaming.settle(first["id"], "complete", {})
        self.store.close()
        self.store = Store(self.path)
        second = self.store.dreaming.claim(config, AT + timedelta(days=7))
        deferred = repos - included
        self.assertIn(second["evidence"][0]["repo"], deferred)
        self.assertTrue(deferred <= {event["repo"] for event in second["evidence"]})
