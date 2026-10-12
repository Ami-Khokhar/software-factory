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

    def paused(self, store):
        """A journalled replace operation with a pause barrier over original #1, replacement #2
        and dependent #3."""
        store.open_request("req-1", "replace", {"workItemId": "o/r#1"})
        store.pause("req-1", "replace", {"original": "o/r#1", "replacement": "o/r#2",
                                        "dependent": "o/r#3"})

    def test_pause_survives_restart_until_released(self):
        """#233: the pause barrier is durable, like the journal it is tied to, and holds until
        an explicit release."""
        store = Store(self.path)
        self.paused(store)
        store.close()

        reopened = Store(self.path)
        self.assertEqual(reopened.active_pauses(), [
            {"requestId": "req-1", "operation": "replace", "role": "original", "workItemId": "o/r#1"},
            {"requestId": "req-1", "operation": "replace", "role": "replacement", "workItemId": "o/r#2"},
            {"requestId": "req-1", "operation": "replace", "role": "dependent", "workItemId": "o/r#3"}])
        reopened.close()

    def test_pause_only_names_the_operation_that_is_journalled(self):
        store = Store(self.path)
        with self.assertRaisesRegex(Conflict, "is not journalled"):
            store.pause("req-1", "replace", {"original": "o/r#1", "replacement": "o/r#2",
                                            "dependent": "o/r#3"})
        store.open_request("req-1", "replace", {"workItemId": "o/r#1"})
        with self.assertRaisesRegex(Conflict, "journalled as .replace."):
            store.pause("req-1", "recover", {"original": "o/r#1", "replacement": "o/r#2",
                                            "dependent": "o/r#3"})
        self.assertEqual(store.active_pauses(), [])
        store.close()

    def test_pausing_twice_with_the_same_membership_replays(self):
        store = Store(self.path)
        self.paused(store)
        self.paused(store)
        self.assertEqual(len(store.active_pauses()), 3)
        store.close()

    def test_unrelated_work_is_not_included(self):
        store = Store(self.path)
        self.paused(store)
        self.assertEqual(store.active_pauses("o/r#4"), [])
        self.assertEqual([p["workItemId"] for p in store.active_pauses("o/r#2")], ["o/r#2"])
        store.close()

    def test_release_is_explicit_and_only_after_the_operation_completed(self):
        """A failed or incomplete operation cannot silently clear the pause: the release reads
        the journal, and only an operation at the done phase may clear it. An operation that
        failed can only release with a new journalled attempt, never by advancing the old one."""
        store = Store(self.path)
        self.paused(store)
        with self.subTest("started"), self.assertRaisesRegex(Conflict, "has not completed"):
            store.release_pause("req-1")
        self.assertEqual(len(store.active_pauses()), 3, "the started pause was cleared")
        store.advance_request("req-1", "pushing")
        with self.subTest("pushing"), self.assertRaisesRegex(Conflict, "has not completed"):
            store.release_pause("req-1")
        self.assertEqual(len(store.active_pauses()), 3, "the pushing pause was cleared")
        store.advance_request("req-1", "done")
        store.release_pause("req-1")
        self.assertEqual(store.active_pauses(), [])
        store.close()

    def test_release_is_recorded_in_the_audit_log(self):
        """A restart can see the barrier was released, not that it never was."""
        store = Store(self.path)
        self.paused(store)
        store.advance_request("req-1", "pushing")
        store.advance_request("req-1", "done")
        store.release_pause("req-1")
        store.close()

        reopened = Store(self.path)
        self.assertEqual(reopened.active_pauses(), [])
        released = [row for row in reopened._db.execute(
            "SELECT request_id, released_at FROM pauses WHERE request_id = 'req-1'").fetchall()]
        self.assertEqual([row[0] for row in released], ["req-1", "req-1", "req-1"])
        self.assertTrue(all(row[1] for row in released), "a released barrier has no released_at")
        reopened.close()

    def test_release_of_an_unknown_pause_is_refused(self):
        store = Store(self.path)
        self.paused(store)
        with self.assertRaisesRegex(Conflict, "has no active pause"):
            store.release_pause("req-2")
        self.assertEqual(len(store.active_pauses()), 3)
        store.close()

    def test_unknown_request_reads_as_none(self):
        store = Store(self.path)
        self.assertIsNone(store.request("req-nope"))
        self.assertEqual(store.requests(), [])
        store.close()


if __name__ == "__main__":
    unittest.main()
