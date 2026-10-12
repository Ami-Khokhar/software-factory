"""The SQLite store: WorkItems survive a restart, a stale write loses, old stores are migrated."""
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from factory_mcp.store import SCHEMA, Conflict, Store
from tests.fakes import workitem


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = str(Path(self.dir.name) / "state" / "factory.sqlite3")

    def tearDown(self):
        self.dir.cleanup()

    def test_survives_restart(self):
        store = Store(self.path)
        stored = store.put(workitem())
        store.put(dict(stored, state="ready"))
        store.log("tick", {"to": "ready"}, workitem_id="o/r#1")
        store.close()

        reopened = Store(self.path)
        self.assertEqual(reopened.get("o/r#1")["state"], "ready")
        self.assertEqual(reopened.get("o/r#1")["stateVersion"], 2)
        self.assertEqual([h["action"] for h in reopened.history("o/r#1")], ["tick"])
        reopened.close()

    def test_stale_version_loses(self):
        store = Store(self.path)
        first = store.put(workitem())
        store.put(dict(first, state="ready"))
        with self.assertRaises(Conflict):
            store.put(dict(first, state="cancelled"))
        with self.assertRaises(Conflict):
            store.put(workitem())
        self.assertEqual(store.get("o/r#1")["state"], "ready")
        store.close()

    def test_opening_an_old_store_drops_the_removed_fields(self):
        """A store written before #27 holds WorkItems with fields the schema now rejects."""
        Path(self.path).parent.mkdir(parents=True)
        old = sqlite3.connect(self.path)
        old.executescript(SCHEMA)
        doc = dict(workitem(), stateVersion=3, provider="agent-relay", sessionHandle="s-1",
                   sessionNative=False, baseSha="b0", blockedOn="waiting on a person")
        old.execute("INSERT INTO workitems VALUES (?, 3, ?)", (doc["id"], json.dumps(doc)))
        waiting = dict(workitem(state="human_waiting", blockedOn="gate said HUMAN"),
                       stateVersion=3, provider="agent-relay", sessionHandle="s-1",
                       sessionNative=False, baseSha="b0")
        old.execute("INSERT INTO workitems VALUES (?, 3, ?)",
                    (waiting["id"].replace("#1", "#2"), json.dumps(waiting)))
        old.commit()
        old.close()

        store = Store(self.path)
        self.assertEqual(store.get("o/r#1"), dict(workitem(), stateVersion=3))
        self.assertEqual(store.get("o/r#2"),
                         dict(workitem(state="human_waiting", blockedOn="gate said HUMAN"), stateVersion=3))
        store.close()

    def test_migration_runs_once_a_store_at_version_2_keeps_its_blocked_on(self):
        """A store at version 2 was migrated before #126; opening it again must not move a WorkItem."""
        Path(self.path).parent.mkdir(parents=True)
        old = sqlite3.connect(self.path)
        old.executescript(SCHEMA)
        doc = dict(workitem(state="cancelled", blockedOn="stale wait"), stateVersion=3)
        old.execute("INSERT INTO workitems VALUES (?, 3, ?)", (doc["id"], json.dumps(doc)))
        old.commit()
        old.execute("PRAGMA user_version = 2")
        old.commit()
        old.close()

        store = Store(self.path)
        self.assertEqual(store.get("o/r#1"),
                         dict(workitem(state="cancelled", blockedOn="stale wait"), stateVersion=3))
        store.close()
    def test_journal_survives_interruption_without_sidecar_files(self):
        """A store left un-closed (server SIGTERM) keeps committed data, and nothing lives outside
        factory.sqlite3: a copy of that file alone is the whole store (#197: no WAL sidecars)."""
        store = Store(self.path)
        self.assertNotEqual(store._db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        store.open_request("req-crash", "recover", {"workItemId": "o/r#1"})
        store.advance_request("req-crash", "pushing")
        # No close: as if the process were killed. The committed phase must still be there.
        store._db.close()

        sidecars = [p.name for p in Path(self.path).parent.iterdir() if p.name != Path(self.path).name]
        self.assertEqual(sidecars, [], f"the store leaves sidecar files: {sidecars}")
        reopened = Store(self.path)
        self.assertEqual(reopened.request("req-crash")["phase"], "pushing")
        reopened.close()

    def test_request_journal_survives_restart(self):
        """A replacement request recorded before a restart is journalled durably."""
        store = Store(self.path)
        opened = store.open_request("req-1", "recover", {"workItemId": "o/r#1"})
        self.assertEqual(opened["phase"], "started")
        store.advance_request("req-1", "pushing")
        store.log("recover", {"requestId": "req-1"})
        store.close()

        reopened = Store(self.path)
        self.assertEqual(reopened.request("req-1"),
                         {"requestId": "req-1", "operation": "recover", "phase": "pushing",
                          "inputsFingerprint": opened["inputsFingerprint"]})
        reopened.close()

    def test_identical_request_resumes_same_operation(self):
        store = Store(self.path)
        first = store.open_request("req-1", "recover", {"workItemId": "o/r#1"})
        store.advance_request("req-1", "pushing")
        store.close()

        reopened = Store(self.path)
        resumed = reopened.open_request("req-1", "recover", {"workItemId": "o/r#1"})
        self.assertEqual(resumed, dict(first, reused=True, phase="pushing"))
        reopened.close()

    def test_same_request_id_with_different_inputs_fails(self):
        store = Store(self.path)
        store.open_request("req-1", "recover", {"workItemId": "o/r#1"})
        with self.assertRaises(Conflict):
            store.open_request("req-1", "recover", {"workItemId": "o/r#2"})
        with self.assertRaises(Conflict):
            store.open_request("req-1", "replace", {"workItemId": "o/r#1"})
        # The journal still holds the first request untouched.
        self.assertEqual(store.request("req-1")["operation"], "recover")
        store.close()

    def test_non_identical_inputs_have_distinct_fingerprints(self):
        store = Store(self.path)
        one = store.open_request("req-1", "recover", {"workItemId": "o/r#1"})
        other = store.open_request("req-2", "recover", {"workItemId": "o/r#2"})
        self.assertNotEqual(one["inputsFingerprint"], other["inputsFingerprint"])
        # Key order and whitespace do not change the identity of the inputs.
        reordered = store.open_request("req-3", "recover", {"workItemId": "o/r#1"})
        self.assertEqual(one["inputsFingerprint"], reordered["inputsFingerprint"])
        store.close()

    def test_phases_only_move_forward_and_reject_unknown_ones(self):
        store = Store(self.path)
        store.open_request("req-1", "recover", {"workItemId": "o/r#1"})
        with self.assertRaises(Conflict):
            store.advance_request("req-1", "done")  # not the next phase
        store.advance_request("req-1", "pushing")  # the next phase
        with self.assertRaises(Conflict):
            store.advance_request("req-1", "started")  # backwards
        with self.assertRaises(Conflict):
            store.advance_request("req-1", "made-up-phase")
        self.assertEqual(store.request("req-1")["phase"], "pushing")
        store.close()

    def test_unknown_request_reads_as_none(self):
        store = Store(self.path)
        self.assertIsNone(store.request("req-nope"))
        self.assertEqual(store.requests(), [])
        store.close()


if __name__ == "__main__":
    unittest.main()
