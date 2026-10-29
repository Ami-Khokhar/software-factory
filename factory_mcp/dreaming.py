"""Durable evidence windows and one global seven-day dreaming cooldown."""
import json
import uuid
from datetime import timedelta

from . import memory
from .mcp import ToolError

COOLDOWN = timedelta(days=7)


class Dreaming:
    def __init__(self, store):
        self.store = store

    def latest(self):
        with self.store._lock:
            row = self.store._db.execute("SELECT doc FROM dream_runs ORDER BY started DESC LIMIT 1").fetchone()
        return json.loads(row[0]) if row else None

    def collect(self, config):
        evidence, cursors, size = [], {}, 0
        repos, pages, blocked = sorted(config.repos), {}, set()
        last = self.latest()
        previous = last["evidence"][-1]["repo"] if last and last.get("evidence") else None
        if previous in repos:
            start = repos.index(previous) + 1
            repos = repos[start:] + repos[:start]
        for repo in repos:
            with self.store._lock:
                row = self.store._db.execute("SELECT seq FROM dream_cursors WHERE repo=?", (repo,)).fetchone()
            after = row[0] if row else 0
            pages[repo] = memory.events(self.store, config, {"repo": repo, "after": after, "limit": 20})["events"]
        # Share the byte budget; rotate the first repo after each durable run.
        for index in range(20):
            for repo in repos:
                if repo in blocked or index >= len(pages[repo]):
                    continue
                event = {"repo": repo, **pages[repo][index]}
                count = len(json.dumps(event).encode())
                if size + count > 12000:
                    blocked.add(repo)  # Never acknowledge a later event past an omitted one.
                    continue
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
