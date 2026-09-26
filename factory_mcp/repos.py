"""repos.json: the repos a factory tool enabled (ADR-0014). FACTORY_REPOS lists the others.

    {"version": 1,
     "repos": [{"name": "owner/repo", "added_by": "login", "added_at": "2026-09-25T18:00:00Z"}]}

factory-mcp reads it at start. A person may read or edit it; an edit takes effect at the next start.
"""
import json
import re
from pathlib import Path

NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ENTRY_KEYS = {"name", "added_by", "added_at"}


def problems(doc):
    """Why `doc` is not a repos.json document. Empty means it is one."""
    if not isinstance(doc, dict) or doc.get("version") != 1:
        return ['it needs "version": 1']
    found = [f"unknown field {key}" for key in sorted(set(doc) - {"version", "repos"})]
    if not isinstance(doc.get("repos"), list):
        return found + ["repos must be a list"]
    seen = set()
    for entry in doc["repos"]:
        name = entry.get("name") if isinstance(entry, dict) else None
        if not isinstance(name, str) or not NAME.match(name):
            found.append(f"{json.dumps(entry)} needs a name in the form owner/repo")
            continue
        found += [f"{name}: unknown field {key}" for key in sorted(set(entry) - ENTRY_KEYS)]
        found += [f"{name}: {key} must be a string" for key in ("added_by", "added_at")
                  if key in entry and not isinstance(entry[key], str)]
        if name.lower() in seen:  # GitHub repo names ignore case
            found.append(f"{name} is listed twice")
        seen.add(name.lower())
    return found


def load(path):
    """The entries in the repos.json at `path`, or none when there is no such file."""
    if not path or not Path(path).is_file():
        return []
    try:
        doc = json.loads(Path(path).read_text())
    except ValueError as exc:
        raise ValueError(f"{path} is not JSON: {exc}") from None
    found = problems(doc)
    if found:
        raise ValueError(f"{path}: " + "; ".join(found))
    return doc["repos"]
