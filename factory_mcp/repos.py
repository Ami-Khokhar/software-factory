"""The repo manifest (repos.json): which repos the factory works on, and how.

JSON, not YAML: factory-mcp is stdlib-only, and a hand-rolled YAML subset would be fragile.
Pure functions and dataclasses; the only I/O is load and the atomic save.
"""
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
RISKS = ("normal", "local-only")
LOCALITIES = ("local", "hosted")


@dataclass(frozen=True)
class Repo:
    name: str
    enabled: bool = True
    risk: str = "normal"
    default_branch: str = "main"
    workers: tuple = ()  # ((agentId, "local"|"hosted"), ...); empty = the defaults
    trusted_authors: frozenset = frozenset()  # empty = the defaults
    protected_paths: tuple = ()  # empty = the defaults
    verify: str = ""  # copied from .github/pr-gatekeeper.json at enable time
    added_by: str = ""
    added_at: str = ""


@dataclass(frozen=True)
class Manifest:
    workers: tuple = ()
    trusted_authors: frozenset = frozenset()
    protected_paths: tuple = ()
    repos: dict = field(default_factory=dict)  # name -> Repo

    def enabled(self):
        return frozenset(name for name, repo in self.repos.items() if repo.enabled)

    def local_only(self):
        return frozenset(name for name, repo in self.repos.items()
                         if repo.enabled and repo.risk == "local-only")


def check_name(name):
    if not isinstance(name, str) or not REPO_NAME.match(name):
        raise ValueError(f"{name!r} is not a github repo reference (owner/repo)")
    return name


def parse_workers(items):
    """["agent:local", ...] -> (("agent", "local"), ...)."""
    workers = []
    for item in items or ():
        agent, _, locality = str(item).partition(":")
        if not agent or locality not in LOCALITIES:
            raise ValueError(f"worker {item!r} must be 'agentId:local' or 'agentId:hosted'")
        workers.append((agent, locality))
    return tuple(workers)


def _repo(doc):
    if doc.get("risk", "normal") not in RISKS:
        raise ValueError(f"{doc.get('name')}: risk must be one of {', '.join(RISKS)}")
    gate = doc.get("gate") or {}
    return Repo(name=check_name(doc.get("name")), enabled=bool(doc.get("enabled", True)),
                risk=doc.get("risk", "normal"), default_branch=doc.get("default_branch", "main"),
                workers=parse_workers(doc.get("workers")),
                trusted_authors=frozenset(doc.get("trusted_authors") or ()),
                protected_paths=tuple(doc.get("protected_paths") or ()),
                verify=gate.get("verify", ""), added_by=doc.get("added_by", ""),
                added_at=doc.get("added_at", ""))


def parse(doc):
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise ValueError("the repo manifest needs \"version\": 1")
    defaults = doc.get("defaults") or {}
    repos = [_repo(item) for item in doc.get("repos") or ()]
    if len({r.name for r in repos}) != len(repos):
        raise ValueError("the repo manifest names a repo twice")
    return Manifest(workers=parse_workers(defaults.get("workers")),
                    trusted_authors=frozenset(defaults.get("trusted_authors") or ()),
                    protected_paths=tuple(defaults.get("protected_paths") or ()),
                    repos={r.name: r for r in repos})


def to_doc(manifest):
    def workers(pairs):
        return [f"{agent}:{locality}" for agent, locality in pairs]

    repos = []
    for repo in sorted(manifest.repos.values(), key=lambda r: r.name):
        item = asdict(repo)
        item.update(workers=workers(repo.workers), trusted_authors=sorted(repo.trusted_authors),
                    protected_paths=list(repo.protected_paths), gate={"verify": item.pop("verify")})
        repos.append({key: value for key, value in item.items() if value not in ("", [], None)
                      or key == "enabled"})
    return {"version": 1,
            "defaults": {"workers": workers(manifest.workers),
                         "trusted_authors": sorted(manifest.trusted_authors),
                         "protected_paths": list(manifest.protected_paths)},
            "repos": repos}


def load(path):
    """The manifest at `path`; an empty one when the file does not exist."""
    if not path or not Path(path).is_file():
        return Manifest()
    try:
        return parse(json.loads(Path(path).read_text()))
    except (ValueError, AttributeError, TypeError) as exc:
        raise ValueError(f"{path}: {exc}") from None


def save(path, manifest):
    """Write atomically: a temporary file in the same directory, then rename."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target.parent, prefix=".repos-", suffix=".json",
                                     delete=False) as handle:
        json.dump(to_doc(manifest), handle, indent=2)
        handle.write("\n")
    os.replace(handle.name, target)


def upsert(path, repo):
    manifest = load(path)
    manifest = replace(manifest, repos=dict(manifest.repos, **{repo.name: repo}))
    save(path, manifest)
    return manifest
