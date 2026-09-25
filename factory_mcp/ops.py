"""Repo operations for Day Desk / factory-ops: check a repo, enable it, start an issue.

The Foreman owns the WorkItem state machine; this module owns the repo-level side effects. Every
decision here is deterministic; a Bot only asks. Nothing here merges, creates a repo or writes a
workflow: those stay human (plan.md §6.4).
"""
import base64
import json
import os
import shutil
import subprocess
from collections import Counter
from dataclasses import replace
from datetime import date

from . import repos as manifests
from . import workitem
from .github import GitHubError
from .mcp import ToolError

GATE_CONFIG = ".github/pr-gatekeeper.json"
GATE_WORKFLOW = ".github/workflows/pr-gatekeeper.yml"
LABELS = {workitem.READY: ("0E8A16", "A person approved: the factory starts this issue"),
          workitem.PROPOSED: ("C5DEF5", "Proposed by the Planner; waits for a person")}


def verify_tool(verify):
    """The program the last step of the verify command runs, e.g. `python3`."""
    words = verify.split("&&")[-1].split()
    return words[0] if words else ""


def docker_probe(image, tool):
    """True/False: does `tool` exist in `image`. None when docker itself is unavailable."""
    if not shutil.which("docker"):
        return None
    try:
        done = subprocess.run(["docker", "run", "--rm", "--network=none", image, "sh", "-c",
                               'command -v "$0"', tool], capture_output=True, timeout=20)
    except subprocess.TimeoutExpired:
        return None
    return done.returncode == 0


def git_clone(url, token, dest):
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0")
    if token:  # as in patches.apply: in the environment, not argv
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode()
        env.update(GIT_CONFIG_COUNT="1", GIT_CONFIG_KEY_0="http.extraheader",
                   GIT_CONFIG_VALUE_0=f"AUTHORIZATION: basic {basic}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    try:
        done = subprocess.run(["git", "clone", "-q", "--filter=blob:none", url, dest], env=env,
                              capture_output=True, text=True, timeout=25)
    except subprocess.TimeoutExpired:
        shutil.rmtree(dest, ignore_errors=True)
        raise ToolError(f"cloning {url} took longer than 25 s; call factory_add_repo again") from None
    if done.returncode != 0:
        shutil.rmtree(dest, ignore_errors=True)
        raise ToolError(f"git clone failed: {done.stderr.strip()[-300:]}")


class Ops:
    def __init__(self, store, github, config, foreman=None, probe=docker_probe, clone=git_clone):
        self.store = store
        self.github = github
        self.config = config
        self.foreman = foreman
        self.probe = probe
        self.clone = clone

    # -- reading ------------------------------------------------------------------------------

    def repo_check(self, repo):
        """plan.md §6.3. Read-only: no GitHub writes, no files, no store rows."""
        try:
            manifests.check_name(repo)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        checks = []

        def add(check_id, required, ok, detail, fix=""):
            checks.append({"id": check_id, "required": required, "ok": bool(ok), "detail": detail,
                           "fix": "" if ok else fix})

        try:
            meta, error = self.github.repository(repo), ""
        except GitHubError as exc:
            meta, error = None, str(exc)
        add("repo_exists", True, meta, f"{repo} is readable" if meta else error,
            f"create {repo}, or add it to the factory's fine-grained token (Repository access)")
        entry = self.config.manifest.repos.get(repo)
        risk = entry.risk if entry else (
            "local-only" if repo in self.config.local_only_repos else "normal")
        result = {"repo": repo, "ok": False, "defaultBranch": None, "private": None, "risk": risk,
                  "verify": "", "checks": checks}
        if meta:
            result.update(defaultBranch=meta.get("default_branch", "main"),
                          private=bool(meta.get("private")))
            result["verify"] = self._remote_checks(repo, meta, add)
        add("checkout_root", True, self.config.checkout_root,
            f"worker checkouts go under {self.config.checkout_root}" if self.config.checkout_root
            else "FACTORY_CHECKOUT_ROOT is not set", "set FACTORY_CHECKOUT_ROOT and restart factory-mcp")
        trusted = self.config.trusted_for(repo)
        add("trusted_authors", True, trusted, "trusted: " + ", ".join(sorted(trusted)),
            "set FACTORY_TRUSTED_AUTHORS or the manifest's trusted_authors")
        result["ok"] = all(c["ok"] for c in checks if c["required"])
        return result

    def _remote_checks(self, repo, meta, add):
        branch = meta.get("default_branch", "main")
        add("not_archived", True, not meta.get("archived"),
            "archived" if meta.get("archived") else "not archived", f"unarchive {repo}")
        verify, detail = "", f"{GATE_CONFIG} is missing"
        text = self.github.contents_text(repo, GATE_CONFIG)
        if text is not None:
            try:
                gate = json.loads(text)
                verify = gate.get("verify", "") if isinstance(gate, dict) else ""
                detail = f"verify: {verify}" if verify else f"{GATE_CONFIG} has no verify command"
            except ValueError as exc:
                detail = f"{GATE_CONFIG} is not JSON: {exc}"
        add("gate_config", True, verify, detail,
            f"install pr-gatekeeper: commit {GATE_CONFIG} with a non-empty \"verify\" command")
        present = self.github.contents(repo, GATE_WORKFLOW) is not None
        add("gate_workflow", False, present, f"{GATE_WORKFLOW} " + ("present" if present else "missing"),
            f"commit {GATE_WORKFLOW} so the gate runs on every PR")

        tool, image = verify_tool(verify), self.config.worker_image
        if not image:
            add("verify_tool", False, True, "FACTORY_WORKER_IMAGE is empty; not probed")
        elif not tool:
            add("verify_tool", True, False, "no verify command to probe", "fix gate_config first")
        else:
            found = self.probe(image, tool)
            add("verify_tool", True, found, f"{tool} " + {True: "found", False: "missing",
                                                         None: "not probed: docker unavailable"}[found]
                + f" in {image}", f"install {tool} in {image}, or start docker")

        names = {label["name"] for label in self.github.labels(repo)}
        missing = sorted(set(LABELS) - names)
        add("labels", False, not missing, "missing: " + ", ".join(missing) if missing
            else "factory labels present", "factory_add_repo creates them")

        if meta.get("private"):
            try:
                self.github.check_runs(repo, branch)
                readable, detail = True, "check runs readable"
            except GitHubError as exc:
                readable, detail = False, str(exc)
            add("token_checks", True, readable, detail,
                f"grant the factory token Checks: Read on {repo} (private repos need it)")
        else:
            add("token_checks", False, True, "public repo: check runs are readable without a grant")

        try:
            protected, detail = self.github.branch_protection(repo, branch) is not None, ""
        except GitHubError as exc:  # reading protection needs Administration: read
            protected, detail = False, f"unknown ({exc.status})"
        add("branch_protection", False, protected,
            detail or f"{branch} is " + ("protected" if protected else "not protected"),
            f"protect {branch}: require the pr-gatekeeper check (ADR-0008)")
        return verify

    def repo_status(self, repo=None, live=False):
        names = set(self.config.repos) | set(self.config.manifest.repos)
        if repo is not None:
            names = {repo}
        items = self.store.all()
        out = []
        for name in sorted(names):
            entry = self.config.manifest.repos.get(name)
            mine = [wi for wi in items if wi.get("repo") == name]
            errors = [(h["at"], h["detail"].get("error")) for wi in mine
                      for h in self.store.history(wi["id"]) if h["action"].endswith("_error")]
            row = {"name": name, "enabled": name in self.config.repos,
                   "source": "manifest" if entry else "env" if name in self.config.repos else "none",
                   "risk": "local-only" if name in self.config.local_only_repos else "normal",
                   "workers": [f"{a}:{l}" for a, l in self.config.workers_for(name)],
                   "trustedAuthors": sorted(self.config.trusted_for(name)),
                   "workItems": {"total": len(mine), "byState": dict(Counter(wi["state"] for wi in mine))},
                   "lastError": max(errors)[1] if errors else None}
            if live and repo is not None:
                row["check"] = self.repo_check(name)
            out.append(row)
        return {"repos": out}

    # -- writing ------------------------------------------------------------------------------

    def add_repo(self, repo, risk=None, trusted_authors=None, workers=None, by=""):
        """plan.md §7.3: check, clone, labels, manifest, reload. Refuses on a failing check."""
        if not self.config.repos_file:
            raise ToolError("FACTORY_REPOS_FILE is not set; there is no manifest to write")
        entry = self.config.manifest.repos.get(repo)
        try:
            risk = risk or (entry.risk if entry else "normal")
            if risk not in manifests.RISKS:
                raise ValueError(f"risk must be one of {', '.join(manifests.RISKS)}")
            pairs = manifests.parse_workers(workers) if workers is not None else (
                entry.workers if entry else ())
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        if risk == "local-only" and not any(
                locality == "local" for _, locality in pairs or self.config.workers):
            raise ToolError(f"{repo} is local-only but none of its workers is local")
        check = self.repo_check(repo)
        if not check["ok"]:
            failing = [f"{c['id']}: {c['detail']} -> {c['fix']}" for c in check["checks"]
                       if c["required"] and not c["ok"]]
            raise ToolError(f"{repo} was not enabled; nothing changed. Failing checks: "
                            + "; ".join(failing))
        dest, cloned = self.config.checkout(repo), False
        if not os.path.isdir(os.path.join(dest, ".git")):
            self.clone(self.config.git_url.format(repo=repo), self.config.github_token, dest)
            cloned = True
        present = {label["name"] for label in self.github.labels(repo)}
        created = [name for name in sorted(LABELS) if name not in present]
        for name in created:
            self.github.create_label(repo, name, *LABELS[name])
        new = manifests.Repo(
            name=repo, risk=risk, default_branch=check["defaultBranch"], workers=pairs,
            trusted_authors=frozenset(trusted_authors) if trusted_authors is not None else (
                entry.trusted_authors if entry else frozenset()),
            protected_paths=entry.protected_paths if entry else (),
            verify=check["verify"], added_by=(entry.added_by if entry else "") or by,
            added_at=(entry.added_at if entry else "") or date.today().isoformat())
        self.reload(manifests.upsert(self.config.repos_file, new))
        self.store.log("repo:added", {"repo": repo, "risk": risk, "by": by, "cloned": cloned,
                                      "labelsCreated": created})
        return {"repo": repo, "enabled": True, "checkout": dest, "cloned": cloned,
                "labelsCreated": created, "manifest": self.config.repos_file,
                "status": self.repo_status(repo)["repos"][0]}

    def reload(self, manifest):
        """Apply a new manifest to this process: Ops and the Foreman share the new Config."""
        self.config = self.config.with_manifest(manifest)
        if self.foreman is not None:
            self.foreman.config = self.config

    def approve_issue(self, repo, number, by=""):
        """plan.md §7.4: a person's go. Adds factory:ready and takes the issue in."""
        if repo not in self.config.repos:
            raise ToolError(f"{repo} is not an enabled factory repo; call factory_repo_check")
        try:
            issue = self.github.issue(repo, number)
        except GitHubError as exc:
            if exc.status == 404:
                raise ToolError(f"{repo}#{number} does not exist") from None
            raise
        if "pull_request" in issue or issue.get("state") != "open":
            raise ToolError(f"{repo}#{number} is not an open issue")
        author = (issue.get("user") or {}).get("login", "")
        if not self.config.trusts(repo, author):
            raise ToolError(f"{repo}#{number} was opened by {author or 'unknown'}, who is not a "
                            "trusted author; a trusted author must restate it (ADR-0007)")
        criteria, missing = workitem.parse_acceptance(issue.get("body"))
        if missing or not criteria:
            raise ToolError("The issue needs an '## Acceptance' list of "
                            "'- <claim> — done when: <check>' bullets")
        already = workitem.READY in workitem.labels(issue)
        if not already:
            self.github.add_labels(repo, number, [workitem.READY])
            self.store.log("issue:approved", {"by": by}, workitem.workitem_id(repo, number))
        wi = self.foreman.intake(repo, number) if self.foreman is not None else None
        return {"issue": number, "repo": repo, "alreadyReady": already, "workItem": wi}
