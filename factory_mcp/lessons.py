"""Lessons: what the factory saw at the gate, and what the owner approved to pass on.

A candidate is a blocking finding fingerprint (ADR-0005) the gate reported on two or more
WorkItems in one repo, computed from the audit log. The owner approves one as a lesson in
lessons.json (next to repos.json, ADR-0014/0020), for the WorkOrders of that repo to carry
as data for the worker.

    {"version": 1,
     "lessons": [{"fingerprint": "...", "repo": "owner/repo", "text": "...",
                  "approved_by": "login", "approved_at": "2026-09-27T18:00:00Z"}]}

The factory never approves a lesson itself: gate findings are model text (ADR-0007), so an
unreviewed lesson would be an injection path into every later worker.
"""
import json
import os
import tempfile
from pathlib import Path

from .store import now

ENTRY_KEYS = {"repo", "fingerprint", "text", "approved_by", "approved_at"}
MAX_TEXT = 300  # a lesson rides in every WorkOrder: it stays one line a worker reads


def _blocking_fingerprints(history):
    """Each blocking finding in the verdicts the audit log holds, as (fingerprint, problem)."""
    for entry in history:
        if entry["action"] == "reconcile":
            for finding in entry["detail"].get("blocking") or []:
                yield finding["fingerprint"], finding["problem"]


def candidates(store, repo):
    """Blocking findings the gate reported on 2+ WorkItems of one repo: the lessons to offer.

    Distinct WorkItems, not rounds: one problem stuck across one WorkItem's rounds is one sight
    of it, not two (the fingerprint already sent the second round to a person, ADR-0005).
    """
    seen = {}
    for wi in store.all():
        if wi["repo"].lower() != repo.lower():
            continue
        for fingerprint in {fp for fp, _ in _blocking_fingerprints(store.history(wi["id"]))}:
            known = seen.setdefault(fingerprint, {"fingerprint": fingerprint, "workItems": 0})
            known["workItems"] += 1
    return sorted((known for known in seen.values() if known["workItems"] > 1),
                  key=lambda known: -known["workItems"])


def problems(doc):
    """Why `doc` is not a lessons.json document. Empty means it is one."""
    if not isinstance(doc, dict) or doc.get("version") != 1:
        return ['it needs "version": 1']
    found = [f"unknown field {key}" for key in sorted(set(doc) - {"version", "lessons"})]
    if not isinstance(doc.get("lessons"), list):
        return found + ["lessons must be a list"]
    seen = set()
    for entry in doc["lessons"]:
        if not isinstance(entry, dict):
            found.append(f"{json.dumps(entry)} is not a lesson entry")
            continue
        repo, fingerprint = entry.get("repo"), entry.get("fingerprint")
        if not isinstance(repo, str) or not repo:
            found.append(f"{json.dumps(entry)} needs a repo")
        if not isinstance(fingerprint, str) or not fingerprint:
            found.append(f"{json.dumps(entry)} needs a fingerprint")
        elif fingerprint.lower() in seen:
            found.append(f"{fingerprint} is listed twice")
        seen.add((fingerprint or "").lower())
        found += [f"{fingerprint}: unknown field {key}" for key in sorted(set(entry) - ENTRY_KEYS)]
        found += [f"{fingerprint}: {key} must be text" for key in ("text", "approved_by", "approved_at")
                  if key in entry and not isinstance(entry[key], str)]
        if not isinstance(entry.get("text"), str) or not entry.get("text"):
            found.append(f"{fingerprint}: a lesson needs text")
    return found


def load(path):
    """The lessons in the lessons.json at `path`, or [] when there is no such file."""
    if not path or not Path(path).is_file():
        return []
    doc = json.loads(Path(path).read_text())
    found = problems(doc)
    if found:
        raise ValueError(f"{path}: " + "; ".join(found))
    return doc["lessons"]


def for_repo(lessons, repo):
    """The approved lessons for one repo, newest first."""
    return sorted((entry for entry in lessons if entry["repo"].lower() == repo.lower()),
                  key=lambda entry: entry["approved_at"], reverse=True)


def _write(path, lessons):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target.parent, prefix=".lessons-", suffix=".json",
                                     delete=False) as handle:  # then a rename: never half a file
        json.dump({"version": 1, "lessons": lessons}, handle, indent=2)
        handle.write("\n")
    os.replace(handle.name, target)


def approve(path, repo, fingerprint, text, by, candidates_fn, at=None):
    """Add one owner-approved lesson, or refuse it. The fingerprint must be a candidate for the
    repo: the owner approves what the factory saw, and edits the wording."""
    text = " ".join((text or "").split())[:MAX_TEXT]
    if not text:
        raise ValueError("a lesson needs text")
    if not any(c["fingerprint"] == fingerprint for c in candidates_fn(repo)):
        raise ValueError(f"{fingerprint} is not a candidate in {repo}: the gate must have blocked "
                         "on it on two or more WorkItems for a lesson to be approved")
    lessons = load(path)
    if any(entry["fingerprint"].lower() == fingerprint.lower() for entry in lessons):
        raise ValueError(f"{fingerprint} is already a lesson")
    entry = {"repo": repo, "fingerprint": fingerprint, "text": text,
             "approved_by": by, "approved_at": at or now()}
    _write(path, lessons + [entry])
    return entry


def retire(path, fingerprint):
    """Remove a lesson (the owner rewrote or replaced it), or refuse when it is not one."""
    lessons = load(path)
    left = [entry for entry in lessons if entry["fingerprint"].lower() != fingerprint.lower()]
    if len(left) == len(lessons):
        raise ValueError(f"{fingerprint} is not a lesson")
    _write(path, left)
