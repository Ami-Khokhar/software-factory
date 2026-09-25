"""The WorkItem store (SQLite) and the audit log.

A WorkItem is stored as its whole JSON document. `stateVersion` is optimistic concurrency: a write
must name the version it read, and it stores version + 1.
"""
import json
import sqlite3
import threading
import time
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
CREATE TABLE IF NOT EXISTS leases (
  workitem_id TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  expires REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workorders (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  id TEXT UNIQUE NOT NULL,
  workitem_id TEXT NOT NULL,
  doc TEXT NOT NULL
);
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
        """The audit rows of one WorkItem; `None` gives the rows about no WorkItem (repo ops)."""
        with self._lock:
            rows = self._db.execute(
                "SELECT at, action, detail FROM audit WHERE workitem_id IS ? ORDER BY seq",
                (workitem_id,)).fetchall()
        return [{"at": at, "action": action, "detail": json.loads(detail)} for at, action, detail in rows]

    def acquire(self, workitem_id, holder, ttl=300):
        """Take the per-WorkItem lease. False if another holder has an unexpired one."""
        with self._lock:
            self._db.execute("DELETE FROM leases WHERE workitem_id = ? AND expires < ?",
                             (workitem_id, time.time()))
            try:
                self._db.execute("INSERT INTO leases VALUES (?, ?, ?)",
                                 (workitem_id, holder, time.time() + ttl))
            except sqlite3.IntegrityError:
                return False
        return True

    def release(self, workitem_id, holder):
        with self._lock:
            self._db.execute("DELETE FROM leases WHERE workitem_id = ? AND holder = ?", (workitem_id, holder))

    def save_workorder(self, doc):
        """Idempotent: the id is the idempotency key, so a replay keeps the first document."""
        with self._lock:
            self._db.execute("INSERT OR IGNORE INTO workorders (id, workitem_id, doc) VALUES (?, ?, ?)",
                             (doc["id"], doc["workItemId"], json.dumps(doc, sort_keys=True)))

    def workorders(self, workitem_id):
        with self._lock:
            rows = self._db.execute("SELECT doc FROM workorders WHERE workitem_id = ? ORDER BY seq",
                                    (workitem_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]
