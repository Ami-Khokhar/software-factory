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
  approved_at TEXT NOT NULL,
  scope TEXT
);
CREATE TABLE IF NOT EXISTS setup_merges (
  repo TEXT PRIMARY KEY,
  fingerprint TEXT NOT NULL,
  verify TEXT NOT NULL,
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


# Migration 6: the setup-merge authorization (#263). It adds only a new table, so no stored
# WorkItem changes; the version bump marks stores that have the `setup_merges` table.
def _migrate_6(wi):
    return wi


# Migration 7: the allowance scope (#274). It widens `allowances` with the approved membership;
# no stored WorkItem changes and no allowance row changes meaning (an old row's scope reads as
# NULL, which refuses spends until the owner approves a scoped plan id).
TABLE_MIGRATIONS = {7: "ALTER TABLE allowances ADD COLUMN scope TEXT"}


def _migrate_7(wi):
    return wi


# Migration 8: optional per-item timeout. Old items retain the configured default.
def _migrate_8(wi):
    return dict(wi, wallMsOverride=wi.get("wallMsOverride"))


# Migration 9: mark older salvage checkpoints for safe retirement on the next tick.
def _migrate_9(wi):
    """Mark pre-provenance checkpoints so the Foreman can retire their scratch branches."""
    wip = wi.get("wip")
    if isinstance(wip, dict) and "provenanceVersion" not in wip:
        return dict(wi, wip=dict(wip, provenanceVersion=0))
    return wi


# Migration 10: initialize the durable Day Desk retry intent for older WorkItems.
def _migrate_10(wi):
    return dict(wi, pendingRetry=wi.get("pendingRetry"))


MIGRATIONS = {1: _migrate_1, 2: _migrate_2, 3: _migrate_3, 4: _migrate_4, 5: _migrate_5,
              6: _migrate_6, 7: _migrate_7, 8: _migrate_8, 9: _migrate_9, 10: _migrate_10}


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


class LeaseBusy(Conflict):
    """A retry write no longer holds the WorkItem lease it acquired."""


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
            if TABLE_MIGRATIONS.get(version):
                columns = [row[1] for row in self._db.execute("PRAGMA table_info(allowances)")]
                if "scope" not in columns:  # a fresh store already has it from SCHEMA
                    self._db.execute(TABLE_MIGRATIONS[version])
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

    def _put_locked(self, doc, stored):
        body = json.dumps(stored, sort_keys=True)
        cursor = self._db.execute(
            "UPDATE workitems SET state_version = ?, doc = ? WHERE id = ? AND state_version = ?",
            (stored["stateVersion"], body, doc["id"], doc["stateVersion"]))
        if cursor.rowcount != 1:
            raise Conflict(f"{doc['id']} changed since version {doc['stateVersion']}")

    def _require_lease_locked(self, workitem_id, holder):
        row = self._db.execute("SELECT holder, expires FROM leases WHERE workitem_id = ?",
                               (workitem_id,)).fetchone()
        if not row or row[0] != holder or row[1] <= time.time():
            raise LeaseBusy(f"{workitem_id} lease changed before the retry was recorded")

    def queue_retry(self, doc, request_id, inputs, holder):
        """Record one retry intent and its request identity without spending the allowance."""
        fingerprint = _fingerprint(inputs)
        with self._lock, self._transaction():
            self._require_lease_locked(doc["id"], holder)
            row = self._db.execute("SELECT operation, inputs_fingerprint, phase FROM requests "
                                   "WHERE request_id = ?", (request_id,)).fetchone()
            if row:
                if row[0] != "retry" or row[1] != fingerprint:
                    raise Conflict(f"request {request_id} is already journalled with different inputs")
                saved = self._db.execute("SELECT doc FROM workitems WHERE id = ?", (doc["id"],)).fetchone()
                return {"workItem": json.loads(saved[0]) if saved else None, "phase": row[2], "reused": True}
            stored = dict(doc, stateVersion=doc["stateVersion"] + 1, updatedAt=now())
            self._put_locked(doc, stored)
            at = now()
            self._db.execute("INSERT INTO requests VALUES (?, 'retry', ?, 'queued', ?, ?)",
                             (request_id, fingerprint, at, at))
            self._db.execute("INSERT INTO audit (at, workitem_id, action, detail) VALUES (?, ?, ?, ?)",
                             (at, doc["id"], "retry_queued", json.dumps(inputs, sort_keys=True)))
        return {"workItem": stored, "phase": "queued", "reused": False}

    def cancel_retry(self, doc, request_id, action, detail, holder):
        """Atomically clear a queued intent when a person takes over or its facts become stale."""
        with self._lock, self._transaction():
            self._require_lease_locked(doc["id"], holder)
            row = self._db.execute("SELECT operation, phase FROM requests WHERE request_id = ?",
                                   (request_id,)).fetchone()
            if not row or row != ("retry", "queued"):
                raise Conflict(f"retry request {request_id} is no longer queued")
            current = self._db.execute("SELECT doc FROM workitems WHERE id = ?", (doc["id"],)).fetchone()
            pending = json.loads(current[0]).get("pendingRetry") if current else None
            if not pending or pending.get("requestId") != request_id:
                raise Conflict(f"{doc['id']} no longer carries retry request {request_id}")
            stored = dict(doc, stateVersion=doc["stateVersion"] + 1, updatedAt=now())
            self._put_locked(doc, stored)
            if action != "retry_cancelled":
                self._db.execute("INSERT INTO audit (at, workitem_id, action, detail) VALUES (?, ?, ?, ?)",
                                 (now(), doc["id"], "retry_cancelled",
                                  json.dumps({"requestId": request_id, "supersededBy": action}, sort_keys=True)))
            self._db.execute("INSERT INTO audit (at, workitem_id, action, detail) VALUES (?, ?, ?, ?)",
                             (now(), doc["id"], action, json.dumps(detail, sort_keys=True)))
            self._db.execute("UPDATE requests SET phase = 'cancelled', updated_at = ? WHERE request_id = ?",
                             (now(), request_id))
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

    def global_history(self):
        """The audit rows with no WorkItem: the owner-tool trail, like a setup-merge grant."""
        with self._lock:
            rows = self._db.execute(
                "SELECT at, action, detail FROM audit WHERE workitem_id IS NULL ORDER BY seq").fetchall()
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

    def approve_setup_merge(self, repo, fingerprint, verify, approved_by):
        """Record one owner authorization to merge this repo's setup PR unattended (#263). One row
        per repo: a re-approval replaces the old one, and both the grant and the replacement land
        in the audit, so every authorization is auditable. Revocation is explicit (revoke_setup_merge)."""
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO setup_merges VALUES (?, ?, ?, ?, ?)",
                             (repo.lower(), fingerprint, verify or "", approved_by, now()))
        self.log("setup_merge_authorized", {"repo": repo, "fingerprint": fingerprint, "by": approved_by})

    def revoke_setup_merge(self, repo, revoked_by):
        """Take an owner authorization back. Conflict when there is none. Revocation writesthe
        audit before anything else may merge a setup PR again."""
        with self._lock:
            if not self._db.execute("SELECT 1 FROM setup_merges WHERE repo = ?",
                                    (repo.lower(),)).fetchone():
                raise Conflict(f"no setup-merge authorization for {repo!r} is approved")
            self._db.execute("DELETE FROM setup_merges WHERE repo = ?", (repo.lower(),))
        self.log("setup_merge_revoked", {"repo": repo, "by": revoked_by})

    def setup_merge(self, repo):
        """The approved setup-merge authorization for `repo`, or None. Lowercase: one row per repo."""
        with self._lock:
            row = self._db.execute(
                "SELECT repo, fingerprint, verify, approved_by, approved_at FROM setup_merges "
                "WHERE repo = ?", (repo.lower(),)).fetchone()
        return (dict(zip(("repo", "fingerprint", "verify", "approvedBy", "approvedAt"), row))
                if row else None)

    def approve_allowance(self, plan_id, actions, remaining, approved_by, scope=None):
        """Record one owner approval of a plan recovery allowance (#239). One approval per plan id:
        another call for the same plan refuses, so an allowance is never quietly topped up.
        `scope` is the approved membership the owner named: None only for a record the older
        factory wrote, which refuses spends until the owner approves a scoped plan id (#274)."""
        with self._lock:
            try:
                self._db.execute("INSERT INTO allowances VALUES (?, ?, ?, ?, ?, ?)",
                                 (plan_id, json.dumps(actions, sort_keys=True), remaining,
                                  approved_by, now(),
                                  json.dumps(scope, sort_keys=True) if scope is not None else None))
            except sqlite3.IntegrityError:
                raise Conflict(f"a plan allowance for {plan_id!r} is already approved") from None

    def allowance(self, plan_id):
        """The approved plan allowance, or None."""
        with self._lock:
            row = self._db.execute(
                "SELECT plan_id, actions, remaining, approved_by, approved_at, scope FROM allowances "
                "WHERE plan_id = ?", (plan_id,)).fetchone()
        return self._allowance_row(row) if row else None

    def allowances(self):
        """Every approved plan allowance, oldest approval first."""
        with self._lock:
            rows = self._db.execute(
                "SELECT plan_id, actions, remaining, approved_by, approved_at, scope FROM allowances "
                "ORDER BY approved_at, plan_id").fetchall()
        return [self._allowance_row(row) for row in rows]

    @staticmethod
    def _allowance_row(row):
        return dict(zip(("planId", "actions", "remaining", "approvedBy", "approvedAt", "scope"),
                        (row[0], json.loads(row[1]), row[2], row[3], row[4],
                         json.loads(row[5]) if row[5] else None)))

    def spend_allowance(self, plan_id, action, workitem_id=None, evidence=""):
        """Count one recovery use against an approved plan allowance (#239), and audit it.

        Read, check and write sit in one committed transaction, so two callers racing on the
        same use cannot both spend it, and the audit line lands with the count. Raises Conflict
        when no allowance is approved for the plan, when `action` is outside its scope, or when
        the remaining allowance is gone or changed under this spend. The membership the approval
        named is checked here too (#274): a scoped allowance spends only on a WorkItem of its repo
        and, when it names exact members, of those members; a record without a scope is legacy and
        refuses by default until the owner approves a new scoped plan id.
        """
        with self._lock, self._transaction():
            row = self._db.execute("SELECT actions, remaining, scope FROM allowances WHERE plan_id = ?",
                                   (plan_id,)).fetchone()
            if not row:
                raise Conflict(f"no plan allowance for {plan_id!r} is approved; the owner approves "
                               "one with factory_approve_plan_allowance")
            actions, remaining, scope_row = json.loads(row[0]), row[1], row[2]
            if action not in actions:
                raise Conflict(f"the plan allowance {plan_id!r} permits {actions}, not {action!r}")
            scope = json.loads(scope_row) if scope_row else None
            if scope is None:
                raise Conflict(f"the plan allowance {plan_id!r} predates scoped membership, so it "
                               "refuses beyond the owner's own hands; the owner approves a new "
                               "scoped plan id with factory_approve_plan_allowance, naming the repo "
                               "the plan covers")
            doc = self._db.execute("SELECT doc FROM workitems WHERE id = ?",
                                   (workitem_id,)).fetchone()
            wi = json.loads(doc[0]) if doc else {}
            if wi.get("repo") != scope["repo"] or (scope["items"] and workitem_id not in scope["items"]):
                raise Conflict(f"the plan allowance {plan_id!r} covers {'members ' + ', '.join(scope['items']) if scope['items'] else 'repo ' + scope['repo']}, "
                               f"not {workitem_id!r}; the owner decides that with "
                               "factory_resolve_human or a new scoped plan id")
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
