"""Durable evidence windows and one global seven-day dreaming cooldown."""
import json
import uuid
from datetime import timedelta

from . import memory
from .mcp import ToolError

COOLDOWN = timedelta(days=7)
SCHEMA = """
CREATE TABLE IF NOT EXISTS dream_runs (
 id TEXT PRIMARY KEY, started REAL NOT NULL, eligible REAL NOT NULL,
 status TEXT NOT NULL, doc TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dream_cursors (repo TEXT PRIMARY KEY, seq INTEGER NOT NULL);
"""


class Dreaming:
    def __init__(self, store):
        self.store = store
        store._db.executescript(SCHEMA)

    def latest(self):
        with self.store._lock:
            row = self.store._db.execute("SELECT doc FROM dream_runs ORDER BY started DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def collect(self, config):
        evidence, cursors, size = [], {}, 0
        for repo in sorted(config.repos):
            with self.store._lock:
                row = self.store._db.execute("SELECT seq FROM dream_cursors WHERE repo=?", (repo,)).fetchone()
            after = row[0] if row else 0
            page = memory.events(self.store, config, {"repo": repo, "after": after, "limit": 20})
            for event in page["events"]:
                event = {"repo": repo, **event}
                count = len(json.dumps(event).encode())
                if size + count > 12000:
                    break
                evidence.append(event)
                cursors[repo] = event["seq"]
                size += count
        return evidence, cursors

    def status(self, config, at):
        last = self.latest()
        eligible_at = last["nextEligibleAt"] if last else 0
        if at.timestamp() < eligible_at:
            return {"eligible": False, "reason": "cooldown", "nextEligibleAt": eligible_at}
        if last and last["status"] in ("planned", "publishing"):
            return {"eligible": False, "reason": "publication_pending", "runId": last["id"]}
        evidence, _ = self.collect(config)
        return {"eligible": bool(evidence), "reason": "ready" if evidence else "no_evidence",
                "nextEligibleAt": eligible_at}

    def claim(self, config, at):
        if not self.status(config, at)["eligible"]:
            return None
        evidence, cursors = self.collect(config)
        if not evidence:
            return None
        with self.store._lock, self.store._transaction():
            last = self.store._db.execute("SELECT eligible, status FROM dream_runs ORDER BY started DESC LIMIT 1").fetchone()
            if last and (at.timestamp() < last[0] or last[1] in ("planned", "publishing")):
                return None
            # A crashed claim cannot cause nightly model retries. Once its cooldown ends,
            # the unprocessed evidence is eligible again; old state remains available.
            self.store._db.execute("UPDATE dream_runs SET status='abandoned', "
                "doc=json_set(doc, '$.status', 'abandoned') WHERE status='claimed'")
            run = {"id": uuid.uuid4().hex, "startedAt": at.timestamp(),
                   "nextEligibleAt": (at + COOLDOWN).timestamp(), "status": "claimed",
                   "evidence": evidence, "cursors": cursors}
            self.store._db.execute("INSERT INTO dream_runs VALUES (?,?,?,?,?)", (
                run["id"], run["startedAt"], run["nextEligibleAt"], run["status"], json.dumps(run)))
        return run

    def settle(self, run_id, state, result):
        if state not in ("failed", "planned", "complete"):
            raise ToolError("invalid dream outcome")
        with self.store._lock, self.store._transaction():
            row = self.store._db.execute("SELECT doc FROM dream_runs WHERE id=?", (run_id,)).fetchone()
            if not row:
                raise ToolError("unknown dream run")
            run = json.loads(row[0])
            if run["status"] != "claimed":
                raise ToolError("dream already settled")
            run.update(status=state, result=result)
            self.store._db.execute("UPDATE dream_runs SET status=?, doc=? WHERE id=?",
                                  (state, json.dumps(run), run_id))
            if state != "failed":
                for repo, cursor in run["cursors"].items():
                    self.store._db.execute("INSERT INTO dream_cursors VALUES (?,?) ON CONFLICT(repo) "
                        "DO UPDATE SET seq=MAX(seq,excluded.seq)", (repo, cursor))
        return run
