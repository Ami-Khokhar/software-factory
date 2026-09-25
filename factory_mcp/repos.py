"""Factory repos: what a repo needs before the factory works on it, and repos.json (ADR-0014).

repos.json lists the repos a factory tool enabled; FACTORY_REPOS lists the others.

    {"version": 1,
     "repos": [{"name": "owner/repo", "added_by": "login", "added_at": "2026-09-25T18:00:00Z"}]}

factory-mcp reads it at start. A person may read or edit it; an edit takes effect at the next start.
"""
import json
import re
from pathlib import Path

from .github import GitHubError

NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
ENTRY_KEYS = {"name", "added_by", "added_at"}
GATE_CONFIG, GATE_WORKFLOW = ".github/pr-gatekeeper.json", ".github/workflows/pr-gatekeeper.yml"


def _result(check_id, ok, detail, fix):
    return {"id": check_id, "ok": ok, "detail": detail, "fix": None if ok else fix}


def check(github, repo):
    """What the factory needs of `repo`, as [{id, ok, detail, fix}]. It only reads."""
    try:
        meta = github.repository(repo)
    except GitHubError as exc:
        if exc.status != 404:
            raise
        return [_result("repo", False, f"GitHub shows the factory's token no repo {repo}",
                        "Check the name, or give FACTORY_GITHUB_TOKEN access to the repo.")]
    if meta.get("archived"):
        return [_result("repo", False, f"{repo} is archived", "Unarchive it on GitHub.")]
    if not (meta.get("permissions") or {}).get("push"):
        return [_result("repo", False, f"GitHub says the factory's token cannot push to {repo}",
                        "Give FACTORY_GITHUB_TOKEN write access to its contents, issues and pull requests.")]
    text = github.file(repo, GATE_CONFIG)
    try:
        verify = json.loads(text).get("verify") if text else None
    except (ValueError, AttributeError):
        verify = None
    if text is None:
        gate = _result("gate_config", False, f"{repo} has no {GATE_CONFIG}",
                       "Install pr-gatekeeper in the repo: the gate reviews and merges every factory PR.")
    else:
        gate = _result("gate_config", isinstance(verify, str) and bool(verify.strip()),
                       f"verify: {verify}" if verify else f"{GATE_CONFIG} has no verify command",
                       "Set verify to the command that runs the repo's tests: every worker pass must pass it.")
    workflow = github.file(repo, GATE_WORKFLOW) is not None
    return [_result("repo", True, f"{repo} exists and the factory's token can push to it", None), gate,
            _result("gate_workflow", workflow, "the gate workflow is installed" if workflow else
                    f"{repo} has no {GATE_WORKFLOW}",
                    "Install pr-gatekeeper's workflow: without it no PR gets a verdict.")]


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
