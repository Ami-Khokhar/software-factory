import tempfile
import unittest
from pathlib import Path

from factory_mcp.store import Conflict, Store
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


if __name__ == "__main__":
    unittest.main()
