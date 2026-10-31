"""Factory repos: what a repo needs before the factory works on it, and repos.json (ADR-0014).

repos.json lists the repos a factory tool enabled; FACTORY_REPOS lists the others.

    {"version": 1,
     "repos": [{"name": "owner/repo", "added_by": "login", "added_at": "2026-09-25T18:00:00Z"}]}

factory-mcp reads it at start. A person may read or edit it; an edit takes effect at the next start.
"""
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path

from .github import GitHubError
from .workitem import PROPOSED, READY

NAME = re.compile(r"^(?!\.\.?/)[A-Za-z0-9_.-]+/(?!\.\.?$)[A-Za-z0-9_.-]+$")  # not "." or ".."
STANDARD = re.compile(r"<!-- factory-standard:begin -->.*?<!-- factory-standard:end -->", re.S)
TEMPLATE = Path(__file__).resolve().parent.parent / "templates" / "AGENTS.md"  # ADR-0015
ENTRY_KEYS = {"name", "added_by", "added_at"}
GATE_CONFIG, GATE_WORKFLOW = ".github/pr-gatekeeper.json", ".github/workflows/pr-gatekeeper.yml"
GATE_WORKFLOW_SOURCE = TEMPLATE.parent.parent / GATE_WORKFLOW
LABELS = ((READY, "0E8A16", "software-factory may work on this issue"),
          (PROPOSED, "C5DEF5", "proposed by the Planner; add factory:ready to accept"))
# The two lines that pin the gate revision in a pr-gatekeeper caller workflow. The wrapper's
# `uses:` sha and the `gatekeeper_ref:` input are the only lines an upgrade rewrites.
GATE_USES = re.compile(r"(uses:\s*\S+/\.github/workflows/gatekeeper\.yml@)([0-9a-fA-F]{40})")
GATE_REF = re.compile(r"(gatekeeper_ref:\s*)([0-9a-fA-F]{40})")


def protected_paths(github, repo, base_sha, required):
    """Carry explicit base-branch protections into each WorkOrder, without weakening our floor."""
    text = github.file(repo, GATE_CONFIG, ref=base_sha)
    if text is None:
        return list(required)
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise ValueError(f"{GATE_CONFIG} on the base is invalid JSON: {exc}") from None
    paths = raw.get("protected_paths", []) if isinstance(raw, dict) else None
    if not isinstance(paths, list) or any(not isinstance(p, str) or not p for p in paths):
        raise ValueError(f"{GATE_CONFIG} on the base needs a protected_paths list of nonempty strings")
    return list(dict.fromkeys([*required, *paths]))


def standard_blocks(text):
    """The factory-standard blocks in an AGENTS.md text."""
    return STANDARD.findall((text or "").replace("\r\n", "\n"))


def _fingerprint(plan):
    """A stable digest of exactly what a person reviews before bootstrap writes it."""
    encoded = json.dumps(plan, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _gate_pins(text):
    """A pr-gatekeeper caller's two gate revision pins, or None when `text` is not a caller this
    factory manages: the reusable workflow's `uses: .../gatekeeper.yml@<sha>` and its
    `gatekeeper_ref: <sha>` input, both 40-hex."""
    uses, ref = GATE_USES.search(text or ""), GATE_REF.search(text or "")
    if uses is None or ref is None:
        return None
    return {"uses": uses.group(1), "usesSha": uses.group(2), "ref": ref.group(1), "refSha": ref.group(2)}


def _upgraded_workflow(installed, canonical):
    """`installed` with only its two gate revision pins set to `canonical`'s, or None when it is not
    a managed caller or already current. Every other line of the installed file — permissions,
    secrets, runner, comments — stays exactly as the repo wrote it, so a control update never
    widens a permission or drops a setting the work did not review (ADR-0080)."""
    have = _gate_pins(installed)
    if have is None:
        return None
    want = _gate_pins(canonical)
    if want is None:
        raise ValueError("the software-factory gate workflow pins no gate revision")
    updated = GATE_USES.sub(lambda _match: have["uses"] + want["usesSha"], installed, count=1)
    updated = GATE_REF.sub(lambda _match: have["ref"] + want["refSha"], updated, count=1)
    return None if updated == installed else updated


def _standard_to_append(agents):
    """Return AGENTS.md with the canonical standard appended, or reject conflicting markers."""
    wanted = standard_blocks(TEMPLATE.read_text())
    if len(wanted) != 1:
        raise RuntimeError("templates/AGENTS.md needs exactly one factory-standard block")
    found = standard_blocks(agents)
    if found:
        if found != wanted:
            raise ValueError("AGENTS.md's factory-standard block differs from software-factory's templates/AGENTS.md")
        return None
    if agents and "factory-standard:" in agents:
        raise ValueError("AGENTS.md has malformed factory-standard markers")
    if not agents:
        return wanted[0] + "\n"
    return agents + ("\n" if agents.endswith(("\n", "\r")) else "\n\n") + wanted[0] + "\n"


def bootstrap_plan(github, repo, verify=None):
    """Describe the exact missing controls a person may approve; this only reads GitHub."""
    if not NAME.match(repo):
        raise ValueError(f"{repo!r} is not a repo name in the form owner/repo")
    meta = github.repository(repo)
    canonical, branch = meta["full_name"], meta["default_branch"]
    if meta.get("archived"):
        raise ValueError(f"{canonical} is archived")
    if not (meta.get("permissions") or {}).get("push"):
        raise ValueError(f"the factory's GitHub token cannot push to {canonical}")
    base = github.branch_head(canonical, branch)  # None: an empty repo, which gets a README first

    files = {}
    config = github.file(canonical, GATE_CONFIG)
    if config is None:
        if not isinstance(verify, str) or not verify.strip():
            raise ValueError("a nonempty verify command is required when .github/pr-gatekeeper.json is missing")
        files[GATE_CONFIG] = json.dumps({"verify": verify}, indent=2) + "\n"
    else:
        try:
            config_doc = json.loads(config)
        except ValueError as exc:
            raise ValueError(".github/pr-gatekeeper.json is not valid JSON") from exc
        if not isinstance(config_doc, dict) or not isinstance(config_doc.get("verify"), str) \
                or not config_doc["verify"].strip():
            raise ValueError(".github/pr-gatekeeper.json has no verify command")

    updates = []
    workflow = github.file(canonical, GATE_WORKFLOW)
    if workflow is None:
        files[GATE_WORKFLOW] = GATE_WORKFLOW_SOURCE.read_text()
    else:  # an installed caller: upgrade only a stale gate revision, never the rest of the file
        updated = _upgraded_workflow(workflow, GATE_WORKFLOW_SOURCE.read_text())
        if updated is not None:
            files[GATE_WORKFLOW] = updated
            updates.append(GATE_WORKFLOW)

    agents = _standard_to_append(github.file(canonical, "AGENTS.md"))
    if agents is not None:
        files["AGENTS.md"] = agents

    plan = {"repo": canonical, "default_branch": branch, "base_sha": base, "files": files}
    if updates:  # only when something is upgraded, so an install keeps its stored fingerprint
        plan["updates"] = updates
    if not base:  # a PR needs a base commit; the controls still go on the setup PR (ADR-0019)
        plan["initial_files"] = {"README.md": f"# {canonical.split('/')[1]}\n"}
    return dict(plan, fingerprint=_fingerprint(plan))


def plan_on(plan, base_sha):
    """The plan once an empty repo's default branch has its initial commit `base_sha`: the plan
    bootstrap_plan returns from then on, so a retry finds the same setup branch."""
    plan = {"repo": plan["repo"], "default_branch": plan["default_branch"], "base_sha": base_sha,
            "files": plan["files"], **({k: plan[k] for k in ("updates",) if k in plan})}
    return dict(plan, fingerprint=_fingerprint(plan))


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
    default = meta["default_branch"]
    by_hand = None  # patches.initial_commit refuses a repo with refs but no default-branch commit
    if github.branch_head(repo, default) is None and github.has_refs(repo):
        by_hand = (f"{repo} has refs but no commit on its default branch {default}, so "
                   f"factory_bootstrap_repo refuses it: push a first commit to {default} yourself, "
                   "then call factory_bootstrap_repo.")
    text = github.file(repo, GATE_CONFIG)
    try:
        verify = json.loads(text).get("verify") if text else None
    except (ValueError, AttributeError):
        verify = None
    if text is None:
        gate = _result("gate_config", False, f"{repo} has no {GATE_CONFIG}",
                       by_hand or "Call factory_bootstrap_repo with the repo's verify command: it opens a "
                       "setup PR with the gate files for you to review and merge.")
    else:
        gate = _result("gate_config", isinstance(verify, str) and bool(verify.strip()),
                       f"verify: {verify}" if verify else f"{GATE_CONFIG} has no verify command",
                       "Set verify to the command that runs the repo's tests: every worker pass must pass it.")
    workflow = github.file(repo, GATE_WORKFLOW)
    if workflow is None:
        gate_workflow = _result("gate_workflow", False, f"{repo} has no {GATE_WORKFLOW}",
                                by_hand or "Call factory_bootstrap_repo: it opens a setup PR with the gate workflow "
                                "for you to review and merge. Without the workflow no PR gets a verdict.")
    else:
        stale = _upgraded_workflow(workflow, GATE_WORKFLOW_SOURCE.read_text()) is not None
        gate_workflow = _result("gate_workflow", not stale,
                                "the gate workflow pins an older gate revision" if stale else
                                "the gate workflow is installed",
                                "Call factory_bootstrap_repo: it opens a setup PR that rewrites only the "
                                "gate revision pins to the approved ones, for you to review and merge.")
    agents = github.file(repo, "AGENTS.md")
    found, wanted = standard_blocks(agents), standard_blocks(TEMPLATE.read_text())
    if agents is None:
        detail = f"{repo} has no AGENTS.md"
    elif not found:
        detail = "AGENTS.md has no factory-standard block"
    elif found != wanted:
        detail = "AGENTS.md's factory-standard block differs from software-factory's templates/AGENTS.md"
    else:
        detail = "AGENTS.md carries the factory standard"
    if found:  # factory_bootstrap_repo refuses both of these cases (_standard_to_append)
        agents_fix = ("Replace the block with the one in software-factory's templates/AGENTS.md and "
                      "merge that PR yourself: factory_bootstrap_repo refuses a differing block.")
    elif agents and "factory-standard:" in agents:
        agents_fix = ("Remove the broken factory-standard markers from AGENTS.md by hand and merge that "
                      "PR yourself: factory_bootstrap_repo refuses malformed markers. Then call it.")
    else:
        agents_fix = by_hand or ("Call factory_bootstrap_repo: it opens a setup PR that adds the factory "
                                 "standard to AGENTS.md, for you to review and merge.")
    return [_result("repo", True, f"{repo} exists and the factory's token can push to it", None), gate,
            gate_workflow,
            _result("agents_md", bool(found) and found == wanted, detail, agents_fix)]


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


def add(path, name, by, at):
    """Add `name` to the repos.json at `path` unless it is there. True if it was added."""
    entries = load(path)
    if any(entry["name"].lower() == name.lower() for entry in entries):
        return False
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target.parent, prefix=".repos-", suffix=".json",
                                     delete=False) as handle:  # then a rename: never half a file
        json.dump({"version": 1, "repos": entries + [{"name": name, "added_by": by, "added_at": at}]},
                  handle, indent=2)
        handle.write("\n")
    os.replace(handle.name, target)
    return True
