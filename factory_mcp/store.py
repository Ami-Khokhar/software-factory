"""The WorkItem store (SQLite) and the audit log.

A WorkItem is stored as its whole JSON document. `stateVersion` is optimistic concurrency: a write
must name the version it read, and it stores version + 1. `PRAGMA user_version` counts the migrations
a store has had.
"""
import hashlib
import json
import sqlite3
import threading
import time
from contextlib import contextmanager
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
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY,
  operation TEXT NOT NULL,
  inputs_fingerprint TEXT NOT NULL,
  phase TEXT NOT NULL,
  started_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pauses (
  request_id TEXT NOT NULL,
  operation TEXT NOT NULL,
  role TEXT NOT NULL,
  workitem_id TEXT NOT NULL,
  paused_at TEXT NOT NULL,
  released_at TEXT
);
CREATE INDEX IF NOT EXISTS pauses_workitem ON pauses (workitem_id, released_at);
CREATE TABLE IF NOT EXISTS allowances (
  plan_id TEXT PRIMARY KEY,
  actions TEXT NOT NULL,
  remaining INTEGER NOT NULL,
  approved_by TEXT NOT NULL,
  approved_at TEXT NOT NULL
);
"""
# Migration 1: WorkItem fields the contract no longer has (#27).
REMOVED_FIELDS = ("parentGoalId", "baseSha", "provider", "sessionHandle", "sessionNative", "sessionUrl")


def _migrate_1(wi):
    return {key: value for key, value in wi.items() if key not in REMOVED_FIELDS}


# Migration 2: a WorkItem that left a wait before #110 kept its stale blockedOn; only
# human_waiting and blocked may carry it (line "`blocked` carries evidence" in contracts.md).
def _migrate_2(wi):
    if wi["state"] not in ("human_waiting", "blocked"):
        wi = {key: value for key, value in wi.items() if key != "blockedOn"}
    return wi


# Migration 3: the replacement request journal (#197). It adds only a new table, so no
# stored WorkItem changes; the version bump marks stores that have the `requests` table.
def _migrate_3(wi):
    return wi


# Migration 4: the pause barrier (#233). It adds only a new table, so no
# stored WorkItem changes; the version bump marks stores that have the `pauses` table.
def _migrate_4(wi):
    return wi


# Migration 5: the plan recovery allowance (#239). It adds only a new table, so no stored
# WorkItem changes; the version bump marks stores that have the `allowances` table.
def _migrate_5(wi):
    return wi


MIGRATIONS = {1: _migrate_1, 2: _migrate_2, 3: _migrate_3, 4: _migrate_4, 5: _migrate_5}


def canonical_inputs(inputs):
    """The identity of an approved request's inputs: key order and spacing do not matter."""
    return json.dumps(inputs, sort_keys=True, separators=(",", ":"))


def _fingerprint(inputs):
    return hashlib.sha256(canonical_inputs(inputs).encode()).hexdigest()


# The replacement-recovery phases, in the only order they may be walked (#197).
REQUEST_PHASES = ("started", "pushing", "done")

# The pause-barrier roles: what a recovery operation holds on the graph (#233).
PAUSE_ROLES = ("original", "replacement", "dependent")


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
        # Rollback-journal mode keeps the whole store in the one factory.sqlite3 file: each
        # committed write leaves the hot journal inside it, so a copy of that file alone is
        # complete and an interrupted store is recovered on the next open (#197 finding: WAL
        # spreads commits over -wal/-shm sidecars that survive an unclean stop).
        self._db.execute("PRAGMA synchronous = FULL")
        self._db.executescript(SCHEMA)
        self._migrate()

    def _migrate(self):
        """Bring WorkItems written by an older factory-mcp up to the contract. Safe to re-run."""
        version = self._db.execute("PRAGMA user_version").fetchone()[0]
        while version < max(MIGRATIONS):
            version += 1
            for workitem_id, doc in self._db.execute("SELECT id, doc FROM workitems").fetchall():
                wi = MIGRATIONS[version](json.loads(doc))
                self._db.execute("UPDATE workitems SET doc = ? WHERE id = ?",
                                 (json.dumps(wi, sort_keys=True), workitem_id))
            self._db.execute(f"PRAGMA user_version = {version}")

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

    def all_workorders(self):
        """Every stored WorkOrder, oldest first: what metrics.lesson_effect measures."""
        with self._lock:
            rows = self._db.execute("SELECT doc FROM workorders ORDER BY seq").fetchall()
        return [json.loads(row[0]) for row in rows]

    def open_request(self, request_id, operation, inputs):
        """Journal (or resume) a replacement request keyed by its stable request ID.

        The same ID with the same inputs resumes where the journal left off; the same ID
        with different inputs is a conflicting reuse and fails.
        """
        fingerprint = _fingerprint(inputs)
        with self._lock:
            row = self._db.execute(
                "SELECT operation, inputs_fingerprint, phase FROM requests WHERE request_id = ?",
                (request_id,)).fetchone()
            if row:
                if row[0] != operation or row[1] != fingerprint:
                    raise Conflict(
                        f"request {request_id} is already journalled as {row[0]} with inputs "
                        f"{row[1][:12]}…; this call names {operation} with inputs {fingerprint[:12]}…")
                return {"requestId": request_id, "operation": operation,
                        "inputsFingerprint": fingerprint, "phase": row[2], "reused": True}
            at = now()
            self._db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?)",
                             (request_id, operation, fingerprint, REQUEST_PHASES[0], at, at))
        return {"requestId": request_id, "operation": operation,
                "inputsFingerprint": fingerprint, "phase": REQUEST_PHASES[0], "reused": False}

    def advance_request(self, request_id, phase):
        """Move the request to `phase`; only the next phase in REQUEST_PHASES is accepted."""
        with self._lock:
            current = self._db.execute("SELECT phase FROM requests WHERE request_id = ?",
                                       (request_id,)).fetchone()
            if not current:
                raise Conflict(f"request {request_id} is not journalled")
            try:
                expected = REQUEST_PHASES[REQUEST_PHASES.index(current[0]) + 1]
            except (ValueError, IndexError):
                raise Conflict(f"request {request_id} is at phase {current[0]!r} and cannot "
                               f"advance to {phase!r}") from None
            if phase != expected:
                raise Conflict(f"request {request_id} is at phase {current[0]!r}; the next "
                               f"phase is {expected!r}, not {phase!r}")
            self._db.execute("UPDATE requests SET phase = ?, updated_at = ? WHERE request_id = ?",
                             (phase, now(), request_id))

    def request(self, request_id):
        with self._lock:
            row = self._db.execute(
                "SELECT request_id, operation, inputs_fingerprint, phase FROM requests "
                "WHERE request_id = ?", (request_id,)).fetchone()
        return (dict(zip(("requestId", "operation", "inputsFingerprint", "phase"), row))
                if row else None)

    def requests(self):
        with self._lock:
            rows = self._db.execute(
                "SELECT request_id, operation, inputs_fingerprint, phase FROM requests "
                "ORDER BY started_at, request_id").fetchall()
        return [dict(zip(("requestId", "operation", "inputsFingerprint", "phase"), row))
                for row in rows]

    @contextmanager
    def _transaction(self):
        """One committed write (#233 finding): the rows land together or not at all.

        The connection runs in autocommit mode (isolation_level=None), so each statement
        commits alone; a mid-write crash or a rejected row would leave part of the pause
        barrier behind that the retry can never finish.
        """
        self._db.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._db.execute("ROLLBACK")
            raise
        else:
            self._db.execute("COMMIT")

    def pause(self, request_id, operation, members):
        """Record a durable pause barrier for a journalled recovery operation (#233).

        `members` maps PAUSE_ROLES to the WorkItems the operation holds on the graph: a role's
        value is one WorkItem id string, or a list of them (empty when the operation names no
        WorkItem for the role; a replacement plan may hold several, #235). Each member must
        name a WorkItem. The barrier is tied to the request journal: it can only name an
        operation that is journalled with the same operation, and pausing again with the same
        membership replays the one barrier while different membership is a conflicting reuse.
        """
        if set(members) != set(PAUSE_ROLES):
            raise Conflict(f"a pause barrier names exactly the roles {PAUSE_ROLES}, not {sorted(members)}")
        for role, held in members.items():
            ids = held if isinstance(held, list) else [held]
            if any(not isinstance(workitem_id, str) or not workitem_id for workitem_id in ids):
                raise Conflict(f"the {role} member is {held!r}; a barrier member names a "
                               f"WorkItem id string")
        membership = [(role, workitem_id)
                      for role in PAUSE_ROLES
                      for workitem_id in (members[role] if isinstance(members[role], list)
                                          else [members[role]])]
        with self._lock, self._transaction():
            row = self._db.execute("SELECT operation FROM requests WHERE request_id = ?",
                                   (request_id,)).fetchone()
            if not row:
                raise Conflict(f"request {request_id} is not journalled; a pause barrier names "
                               f"a journalled operation")
            if row[0] != operation:
                raise Conflict(f"request {request_id} is journalled as {row[0]!r}, not {operation!r}")
            stored = self._active_members(request_id)
            if stored:
                if sorted(stored) != sorted(membership):
                    raise Conflict(f"request {request_id} already pauses "
                                   f"{sorted(stored)}; this call names {sorted(membership)}")
                return
            at = now()
            self._db.executemany("INSERT INTO pauses (request_id, operation, role, workitem_id, "
                                 "paused_at, released_at) VALUES (?, ?, ?, ?, ?, NULL)",
                                 [(request_id, operation, role, workitem_id, at)
                                  for role, workitem_id in membership])

    def _active_members(self, request_id):
        """The active barrier's (role, workitem_id) pairs, or [] when none is active."""
        rows = self._db.execute(
            "SELECT role, workitem_id FROM pauses WHERE request_id = ? AND released_at IS NULL",
            (request_id,)).fetchall()
        return [tuple(row) for row in rows]

    def active_pauses(self, workitem_id=None):
        """Every unreleased pause barrier, or those holding one WorkItem."""
        with self._lock:
            query = ("SELECT request_id, operation, role, workitem_id FROM pauses "
                     "WHERE released_at IS NULL")
            args = ()
            if workitem_id is not None:
                query += " AND workitem_id = ?"
                args = (workitem_id,)
            rows = self._db.execute(query + " ORDER BY paused_at, workitem_id, role", args).fetchall()
        return [dict(zip(("requestId", "operation", "role", "workItemId"), row)) for row in rows]

    def release_pause(self, request_id):
        """Clear the operation's pause barrier. Explicit, and only after the operation completed.

        The journal decides what completed means: while the operation has not reached the done
        phase, the pause stays, so a failed or interrupted attempt cannot silently release it.
        """
        with self._lock, self._transaction():
            row = self._db.execute("SELECT phase FROM requests WHERE request_id = ?",
                                   (request_id,)).fetchone()
            if not row:
                raise Conflict(f"request {request_id} has no active pause; it is not journalled")
            if row[0] != REQUEST_PHASES[-1]:
                raise Conflict(f"request {request_id} has not completed; it is at phase {row[0]!r}, "
                               f"so its pause holds until the operation reaches "
                               f"{REQUEST_PHASES[-1]!r}")
            if not self._active_members(request_id):
                raise Conflict(f"request {request_id} has no active pause")
            self._db.execute("UPDATE pauses SET released_at = ? WHERE request_id = ? "
                             "AND released_at IS NULL", (now(), request_id))
            self._db.execute("INSERT INTO audit (at, workitem_id, action, detail) VALUES (?, NULL, ?, ?)",
                             (now(), "pause_released", json.dumps({"requestId": request_id}, sort_keys=True)))

    def approve_allowance(self, plan_id, actions, remaining, approved_by):
        """Record one owner approval of a plan recovery allowance (#239). One approval per plan id:
        another call for the same plan refuses, so an allowance is never quietly topped up."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO allowances VALUES (?, ?, ?, ?, ?)",
                                 (plan_id, json.dumps(actions, sort_keys=True), remaining,
                                  approved_by, now()))
            except sqlite3.IntegrityError:
                raise Conflict(f"a plan allowance for {plan_id!r} is already approved") from None

    def allowance(self, plan_id):
        """The approved plan allowance, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT plan_id, actions, remaining, approved_by, approved_at FROM allowances "
                "WHERE plan_id = ?", (plan_id,)).fetchone()
        return self._allowance_row(row) if row else None

    def allowances(self):
        """Every approved plan allowance, oldest approval first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT plan_id, actions, remaining, approved_by, approved_at FROM allowances "
                "ORDER BY approved_at, plan_id").fetchall()
        return [self._allowance_row(row) for row in rows]

    @staticmethod
    def _allowance_row(row):
        return dict(zip(("planId", "actions", "remaining", "approvedBy", "approvedAt"),
                        (row[0], json.loads(row[1]), row[2], row[3], row[4])))

    def spend_allowance(self, plan_id, action, workitem_id=None, evidence=""):
        """Count one recovery use against an approved plan allowance (#239), and audit it.

        Read, check and write sit in one committed transaction, so two callers racing on the
        same use cannot both spend it, and the audit line lands with the count. Raises Conflict
        when no allowance is approved for the plan, when `action` is outside its scope, or when
        the remaining allowance is gone or changed under this spend.
        """
        with self._lock, self._transaction():
            row = self._db.execute("SELECT actions, remaining FROM allowances WHERE plan_id = ?",
                                   (plan_id,)).fetchone()
            if not row:
                raise Conflict(f"no plan allowance for {plan_id!r} is approved; the owner approves "
                               "one with factory_approve_plan_allowance")
            actions, remaining = json.loads(row[0]), row[1]
            if action not in actions:
                raise Conflict(f"the plan allowance {plan_id!r} permits {actions}, not {action!r}")
            if remaining < 1:
                raise Conflict(f"the plan allowance {plan_id!r} is exhausted; the owner decides any "
                               "further recovery, for example with factory_resolve_human")
            decremented = self._db.execute(
                "UPDATE allowances SET remaining = remaining - 1 "
                "WHERE plan_id = ? AND remaining = ?", (plan_id, remaining))
            if decremented.rowcount != 1:
                raise Conflict(f"the plan allowance {plan_id!r} changed while this spend was written")
            spent = {"planId": plan_id, "action": action, "remaining": remaining - 1}
            self._db.execute("INSERT INTO audit (at, workitem_id, action, detail) VALUES (?, ?, ?, ?)",
                             (now(), workitem_id, "allowance_spent",
                              json.dumps(dict(spent, evidence=evidence), sort_keys=True)))
        return spent
