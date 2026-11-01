"""One durable ledger row per finding: the root cause of a rejected or resolved proposal.

Findings are keyed by root cause fingerprint, so a repair that fails is visible as the
same finding coming back, and an owner's decision survives after the dream issue closes.
The identity matches the memory node a dream writes for that cause (#442).
"""
import json
from datetime import datetime, timedelta

from .knowledge import record_id, text

TERMINAL = ("rejected", "resolved")


def validate_evidence(evidence_seq):
    if not isinstance(evidence_seq, str) or not evidence_seq.strip():
        raise ValueError("evidence sequence must be nonempty text")
    return evidence_seq.strip()


class Findings:
    def __init__(self, store):
        self.store = store

    def identity(self, repo, fingerprint):
        return record_id(repo, "", "cause:" + fingerprint)

    def record(self, repo, fingerprint, summary, evidence_seq, at, issue=None):
        summary = text(summary, "summary", 200)
        evidence_seq = validate_evidence(evidence_seq)
        identity = self.identity(repo, fingerprint)
        with self.store._lock, self.store._transaction():
            db = self.store._db
            current = db.execute("SELECT version, doc FROM findings WHERE id = ?",
                                 (identity,)).fetchone()
            if current:
                doc = json.loads(current[1])
                doc["occurrences"] += 1
                doc["summary"] = summary
                evidence = doc.get("evidence", [])
                if evidence_seq not in evidence:
                    evidence.append(evidence_seq)
                doc["evidence"] = evidence[-10:]
                doc["lastSeen"] = at
                doc["version"] = current[0] + 1
            else:
                doc = {"id": identity, "repo": repo, "fingerprint": fingerprint,
                       "summary": summary, "verdict": "open", "occurrences": 1,
                       "evidence": [evidence_seq], "lastSeen": at, "at": at, "version": 1}
            if issue is not None:
                doc["issue"] = int(issue)
            encoded = json.dumps(doc, sort_keys=True)
            db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?)",
                       (identity, repo, doc["version"], encoded))
            db.execute("INSERT INTO finding_versions VALUES (?,?,?)",
                       (identity, doc["version"], encoded))
        return doc

    def decide(self, identity, verdict, reason, at):
        if verdict not in ("rejected", "resolved", "open"):
            raise ValueError("verdict must be rejected, resolved or open")
        reason = text(reason, "reason", 500)
        with self.store._lock, self.store._transaction():
            db = self.store._db
            row = db.execute("SELECT doc FROM findings WHERE id = ?", (identity,)).fetchone()
            if row is None:
                raise ValueError(f"no finding {identity}")
            doc = json.loads(row[0])
            doc["verdict"] = verdict
            doc["reason"] = reason
            doc["at"] = at
            doc["version"] += 1
            encoded = json.dumps(doc, sort_keys=True)
            db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?)",
                       (identity, doc["repo"], doc["version"], encoded))
            db.execute("INSERT INTO finding_versions VALUES (?,?,?)",
                       (identity, doc["version"], encoded))
        return doc

    def get(self, identity):
        with self.store._lock:
            row = self.store._db.execute("SELECT doc FROM findings WHERE id = ?",
                                         (identity,)).fetchone()
        return json.loads(row[0]) if row else None

    def version(self, identity, version):
        with self.store._lock:
            row = self.store._db.execute(
                "SELECT doc FROM finding_versions WHERE id = ? AND version = ?",
                (identity, version)).fetchone()
        return json.loads(row[0]) if row else None

    def _write(self, identity, at, **fields):
        """Record one decision `reconcile` read from GitHub, keeping the occurrence history."""
        with self.store._lock, self.store._transaction():
            db = self.store._db
            row = db.execute("SELECT doc FROM findings WHERE id = ?", (identity,)).fetchone()
            if row is None:
                raise ValueError(f"no finding {identity}")
            doc = json.loads(row[0])
            doc.update(fields)
            doc["at"] = at
            doc["version"] += 1
            encoded = json.dumps(doc, sort_keys=True)
            db.execute("INSERT OR REPLACE INTO findings VALUES (?,?,?,?)",
                       (identity, doc["repo"], doc["version"], encoded))
            db.execute("INSERT INTO finding_versions VALUES (?,?,?)",
                       (identity, doc["version"], encoded))
        return doc

    def open(self, repo):
        with self.store._lock:
            rows = self.store._db.execute(
                "SELECT doc FROM findings WHERE repo = ? COLLATE NOCASE"
                " AND json_extract(doc, '$.verdict') NOT IN ('rejected','resolved')"
                " ORDER BY json_extract(doc, '$.lastSeen') DESC, id LIMIT 50",
                (repo,)).fetchall()
        return [json.loads(row[0]) for row in rows]


def _ts(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def reconcile(store, github, config, at, quiet_days=28):
    """Read each open finding's outcome from GitHub and record it: a merge is never a resolution.

    A merged pull request lands the fix ("fix_landed"); only a quiet window past `quiet_days`,
    or the owner, resolves it. Unreadable facts change nothing.
    """
    from .github import GitHubError
    from .knowledge import ToolError  # one bad comment must not stop the ledger
    for row in store._db.execute("SELECT doc FROM findings").fetchall():
        doc = json.loads(row[0])
        if doc.get("issue") is None or doc["verdict"] not in ("open", "fix_landed"):
            continue
        identity, repo = doc["id"], doc["repo"]
        try:
            issue = github.issue(repo, doc["issue"])
            if issue.get("state") != "closed":
                continue  # still open: anything could still happen, so write nothing
            pull = github.closing_pull(repo, doc["issue"])
            if pull is None:
                if doc["verdict"] != "open":
                    continue
                owner = config.owner or repo.split("/")[0]
                comments = [c for c in github.comments(repo, doc["issue"])
                            if c["user"]["login"] == owner]
                if not comments:
                    continue  # closed by whom, for why: unknown, so do not guess
                reason = text(comments[-1]["body"][:500], "reason", 500)
                store.findings._write(identity, at, verdict="rejected", reason=reason,
                                      rejectedAt=at)
            else:
                merged_at = pull["merged_at"]
                if _ts(doc["lastSeen"]) > _ts(merged_at) and doc.get("failedFix") != pull["number"]:
                    # a recurrence is named once; later passes on the same pull write nothing
                    store.findings._write(identity, at, verdict="open",
                                          reason=f"recurred after the fix in #{pull['number']}",
                                          failedFix=pull["number"])
                elif doc["verdict"] == "open" and doc.get("fixPr") != pull["number"]:
                    store.findings._write(identity, at, verdict="fix_landed",
                                          reason=f"fix landed in #{pull['number']}",
                                          fixPr=pull["number"], landedAt=merged_at)
                elif _ts(doc["lastSeen"]) <= _ts(merged_at) and _ts(merged_at) < _ts(at) - timedelta(days=quiet_days):
                    # quiet only counts since the merge: a recurrence keeps the finding open
                    store.findings._write(identity, at, verdict="resolved",
                                          reason=f"quiet for {quiet_days} days after #{pull['number']}")
        except (GitHubError, ToolError):
            continue
