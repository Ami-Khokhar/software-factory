"""The WorkItem store (SQLite) and the audit log.

A WorkItem is stored as its whole JSON document. `stateVersion` is optimistic concurrency: a write
must name the version it read, and it stores version + 1.
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS workitems (
  id TEXT PRIMARY KEY,
  state_version INTEGER NOT NULL,
  doc TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  workitem_id TEXT,
  action TEXT NOT NULL,
  detail TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_workitem ON audit (workitem_id, seq);
"""


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Conflict(Exception):
    """A write named a stateVersion that is no longer current."""


class Store:
    def __init__(self, path):
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._lock = threading.Lock()
        self._db.executescript(SCHEMA)

    def close(self):
        self._db.close()

    def get(self, workitem_id):
        with self._lock:
            row = self._db.execute("SELECT doc FROM workitems WHERE id = ?", (workitem_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def all(self):
        with self._lock:
            rows = self._db.execute("SELECT doc FROM workitems ORDER BY id").fetchall()
        return [json.loads(row[0]) for row in rows]

    def put(self, doc):
        """Write `doc` if its stateVersion is current (0 for a new WorkItem). Returns the stored doc."""
        read_version = doc["stateVersion"]
        stored = dict(doc, stateVersion=read_version + 1, updatedAt=now())
        body = json.dumps(stored, sort_keys=True)
        with self._lock:
            if read_version == 0:
                try:
                    self._db.execute("INSERT INTO workitems VALUES (?, 1, ?)", (doc["id"], body))
                except sqlite3.IntegrityError:
                    raise Conflict(f"{doc['id']} already exists") from None
            else:
                cursor = self._db.execute(
                    "UPDATE workitems SET state_version = ?, doc = ? WHERE id = ? AND state_version = ?",
                    (read_version + 1, body, doc["id"], read_version))
                if cursor.rowcount != 1:
                    raise Conflict(f"{doc['id']} changed since version {read_version}")
        return stored

    def log(self, action, detail, workitem_id=None):
        with self._lock:
            self._db.execute("INSERT INTO audit (at, workitem_id, action, detail) VALUES (?, ?, ?, ?)",
                             (now(), workitem_id, action, json.dumps(detail, sort_keys=True)))

    def history(self, workitem_id):
        with self._lock:
            rows = self._db.execute(
                "SELECT at, action, detail FROM audit WHERE workitem_id = ? ORDER BY seq",
                (workitem_id,)).fetchall()
        return [{"at": at, "action": action, "detail": json.loads(detail)} for at, action, detail in rows]
