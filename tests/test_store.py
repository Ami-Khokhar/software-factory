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


if __name__ == "__main__":
    unittest.main()
