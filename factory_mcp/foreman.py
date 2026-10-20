"""The Foreman: under a per-WorkItem lease, reconcile with GitHub, then act at most once."""
import calendar
import dataclasses
import fnmatch
import hashlib
import json
import os
import re
import secrets
import time
import threading
import uuid

from . import (diagnostics, documents, gate_secrets, jev_rank, lessons, patches, registry, repos,
               routing, setup_merge, sizing, workitem)
from .github import GitHubError
from .mcp import ToolError
from .relay import RelayError, RelayLimitError
from .store import REQUEST_PHASES, Conflict, LeaseBusy, now
from .verdict import blocking, fingerprint

# States that need a person. Entering one posts a comment on the issue, so the owner hears of it.
ATTENTION = ("human_waiting", "blocked", "exhausted")
WORKING = ("dispatching", "implementing")
# A node holds its `Paths:` files while it is one of these (ADR-0088); waiting for a person
# holds nothing, so a stalled node never blocks its neighbours' files.
PATHS_HOLDING = ("dispatching", "implementing", "pr_open", "reviewing", "changes_requested",
                 "merge_pending")


def _covers(prefix, entry):
    """`prefix` covers `entry` when its literal head, cut at the first wildcard, is `entry` or a
    directory prefix of it."""
    stem = prefix.split("*")[0].rstrip("/")
    return bool(stem) and (entry == stem or entry.startswith(stem + "/"))


def _overlaps(entry, other):
    """Two `Paths:` entries meet (ADR-0088): a glob match in either direction, or one's literal
    head covering the other as a directory."""
    return (fnmatch.fnmatchcase(entry, other) or fnmatch.fnmatchcase(other, entry)
            or _covers(entry, other) or _covers(other, entry))
from .setup_merge import BOOTSTRAP_PREFIX
MAX_PLAN_ISSUES = 10  # one approval covers this many issues at most (ADR-0024)
REGATE_MARKER = re.compile(r"<!-- factory regate (\d+) -->")
# The plan recovery allowance (#239): what one owner approval covers, and how far it reaches.
RECOVERY_ACTIONS = ("retry", "replace")
MAX_ALLOWANCE = 20
# Access escalations must reach a person here, so these name what the factory's own access
# troubles emit, not a phrase nothing writes (round 1 finding, #239): the remote-verify 403 points
# at FACTORY_GITHUB_TOKEN and docs, and a denied git push says "Permission to ... denied".
SAFETY_STOPS = ("pr-gatekeeper decided human", "protected path", "access denied",
                "resource not accessible", "permission to", "may read actions")
# the identity marker a replacement the factory opens carries: this request, at this position
REPLACEMENT_MARKER = re.compile(r"<!-- factory replace (\S+) (\d+) -->")
REMOTE_VERIFY_WORKFLOW = ".github/workflows/factory-verify.yml"  # the workflow's name there is `factory-verify`
# An accepted relay task the relay no longer knows is an infrastructure loss, not the code's
# (ADR-0081). The same WorkOrder is re-queued this many times per WorkItem before the ordinary
# failed attempt stands; a successful Result resets the count.
RELAY_RETRIES_MAX = 2
# A pass that ran out of the clock: the harness's own timeout or the relay's `timed_out` status.
# This is the worker-health signal recovery acts on; whether the cause is evidence-backed is the
# attempt's business, never this one (ADR-0081).
OUT_OF_TIME = re.compile(r"task timed_out|the harness timed out")
DEFAULT_REVIEWER = "deepseek-v4.1-flash"  # pr-gatekeeper's reviewer_model when the key is absent
WIP_MOVES_MAX = 2  # how far a salvage wip may move forward before timeouts stop costing pushes
PARTIAL_WORK = ("A worker ran out of time. Its unfinished work is the commit you start from. "
                "Check each acceptance bullet against the code, not against any report. "
                "Keep what is right, fix or remove the rest, and finish.")
# Reason text is incident identity (ADR-0081), and a commit sha or the attempt count a reason
# narrates is not material: both read the same every time they are said again.
SHA40 = re.compile(r"\b[0-9a-f]{40}\b")
ATTEMPT_COUNT = re.compile(r"\b\d+ worker attempts\b")
# The budget boundaries: the writes that reset `attempts` and start a fresh budget of passes. A
# one-per-budget recovery is looked for after the last of these.
BUDGET_RESETS = ("human_resolved", "allowance_retry", "result")


def agent_resolved(history):
    """Whether Day Desk, driving, already resolved this WorkItem once (ADR-0030)."""
    return any(h["action"] == "human_resolved" and h["detail"].get("agent") and not h["detail"].get("ownerAsked")
               for h in history)


def agent_may_resolve(wi, history):
    """Whether Day Desk, driving, may answer this escalation (ADR-0030, ADR-0081): each incident —
    the escalation and its material facts — gets one answer, and a materially changed incident gets
    one fresh one. A WorkItem escalated before the incident field falls back to the one-answer rule
    over its history."""
    incident = wi.get("incident")
    if incident is None:
        return not agent_resolved(history)
    return not incident.get("agentAnswered")


class RepoNotReady(ToolError):
    """The repo failed the repo check: its author can fix that, so discovery tells them (ADR-0027)."""


class Foreman:
    def __init__(self, store, github, config, relay=None):
        self.store = store
        self.github = github
        self.config = config
        self.relay = relay
        self.runtime_lock = threading.RLock()
        self.holder = uuid.uuid4().hex
        self._skipped = set()  # rebuild refusals already logged by this process

    def intake(self, repo, number):
        repo = self._allowlisted(repo)
        existing = self.store.get(workitem.workitem_id(repo, number))
        if existing:
            return existing
        wi = self._admit(repo, self._issue(repo, number))
        try:
            wi = self.store.put(wi)
        except Conflict:  # a concurrent tick took it in first
            return self.store.get(wi["id"])
        self.store.log("intake", {"state": wi["state"]}, wi["id"])
        return wi

    def _enabled(self, repo, by):
        """The allowlist's spelling of `repo`. A repo that is not listed yet is enabled first when it
        passes the repo check, and refused with the failed checks when it does not (ADR-0027)."""
        listed = self.config.listed(repo)
        return listed if listed is not None else self.add_repo(repo, by)["repo"]

    def _allowlisted(self, repo):
        """The allowlist's spelling of `repo`, so one repo has one WorkItem id."""
        listed = self.config.listed(repo)
        if listed is None:
            raise ToolError(f"{repo} is not in FACTORY_REPOS or repos.json")
        return listed

    def _issue(self, repo, number):
        try:
            return self.github.issue(repo, number)
        except GitHubError as exc:
            if exc.status == 404:
                raise ToolError(f"{repo}#{number} does not exist") from None
            raise

    def _admit(self, repo, issue):
        """The new WorkItem for `issue`, or a ToolError saying why the factory may not take it."""
        author = (issue.get("user") or {}).get("login")
        if not self.config.trusts(repo, author):
            raise ToolError(f"{repo}#{issue['number']} was opened by {author}, who is not a trusted "
                            "author (FACTORY_TRUSTED_AUTHORS); a trusted person must open the issue")
        try:
            return workitem.intake(repo, issue)
        except ValueError as exc:
            raise ToolError(str(exc)) from None

    def approve_issue(self, repo, number, by):
        """A person's go on an issue: label it factory:ready and take it in. Idempotent."""
        repo = self._enabled(repo, by)
        existing = self.store.get(workitem.workitem_id(repo, number))
        if existing and existing["state"] in workitem.TERMINAL:
            raise ToolError(f"{existing['id']} is {existing['state']}; open a new issue for more work")
        issue = self._issue(repo, number)
        self._admit(repo, issue)  # refuse before labelling: a rebuild adopts any factory:ready issue
        if workitem.READY not in workitem.labels(issue):
            self.github.add_labels(repo, number, [workitem.READY])
            self.store.log("approved", {"by": by}, workitem.workitem_id(repo, number))
        return self.intake(repo, number)

    def add_repo(self, repo, by):
        """Enable a repo that passes the checks: its worker checkout, the factory labels, and an
        entry in repos.json (ADR-0014). A failing check refuses it, changing nothing. Idempotent."""
        if not repos.NAME.match(repo):
            raise ToolError(f"{repo!r} is not a repo name in the form owner/repo")
        if not self.config.repos_file:
            raise ToolError("FACTORY_REPOS_FILE is not set, so there is no repos.json to add the repo to")
        if not self.config.checkout_root:
            raise ToolError("FACTORY_CHECKOUT_ROOT is not set, so there is no place for the worker checkout")
        failed = [c for c in repos.check(self.github, repo) if not c["ok"]]
        if failed:
            raise RepoNotReady(f"{repo} is not ready: " + " ".join(f"{c['detail']}. {c['fix']}" for c in failed))
        # One spelling per repo: the allowlist's if it lists the repo, else GitHub's
        listed = self.config.listed(repo)
        repo = listed or self.github.repository(repo)["full_name"]
        checkout = self.config.checkout(repo)
        if checkout and not os.path.isdir(os.path.join(checkout, ".git")):
            try:
                patches.clone(self.config.git_url.format(repo=repo), self.config.github_token, checkout)
            except patches.PatchError as exc:
                raise ToolError(f"the worker checkout for {repo} was not cloned: {exc}") from None
        for name, color, text in repos.LABELS:
            self.github.create_label(repo, name, color, text)
        added = not listed and repos.add(self.config.repos_file, repo, by, now())  # who and when
        if not listed:  # works at once, in the spelling repos.json holds (a person may have added it)
            repo = next(e["name"] for e in repos.load(self.config.repos_file) if e["name"].lower() == repo.lower())
            self.config = dataclasses.replace(self.config, repos=self.config.repos | {repo})
        return {"repo": repo, "added": added, "checkout": checkout}

    def bootstrap_repo(self, repo, verify, by):
        """Prepare only missing factory controls on a setup PR for the owner's merge."""
        if not isinstance(by, str) or not by.strip():
            raise ToolError("bootstrap needs the name of the person who approved it")
        try:
            plan = repos.bootstrap_plan(self.github, repo, verify)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        repo, files = plan["repo"], plan["files"]
        if not files:
            return {"repo": repo, "ready": True, "needsHumanMerge": False, "prNumber": None,
                    "secrets": self._gate_secrets(repo, by)}
        # before any write: a plan read from a default branch that has moved since must not be pushed
        if self.github.branch_head(repo, plan["default_branch"]) != plan["base_sha"]:
            raise ToolError(f"{repo}'s default branch changed during bootstrap; check the repo and try again")
        branch = BOOTSTRAP_PREFIX + plan["fingerprint"][:16]
        response = {"repo": repo, "ready": False, "needsHumanMerge": True, "changedPaths": sorted(files)}
        if plan.get("updates"):
            response["updatedPaths"] = plan["updates"]  # an upgrade, not a first install
        existing = self._open_setup_pr(repo, plan["default_branch"])
        if existing:  # a retry: the owner reviews one setup PR, brought up to date with the plan
            head = self._update_setup_pr(repo, existing, files, by)
            secrets = self._gate_secrets(repo, by)
            if head:
                response["updatedSha"] = head
            body = self._setup_pr_body(repo, plan, secrets)
            if body != existing.get("body"):  # it names the files and secret statuses: keep it true
                self.github.edit_pull(repo, existing["number"], body)
            return dict(response, prNumber=existing["number"], secrets=secrets,
                        url=existing.get("html_url") or f"https://github.com/{repo}/pull/{existing['number']}")
        if plan["base_sha"]:  # an empty repo has no branches; its setup branch is named after the README
            self._refuse_existing_branch(repo, branch)
        secrets = response["secrets"] = self._gate_secrets(repo, by)  # after the refusals: they write nothing
        initial = None
        if plan["base_sha"] is None:  # an empty repo: only a README goes straight to the default branch
            try:
                initial = patches.initial_commit(self.config.git_url.format(repo=repo), self.config.github_token,
                                                 plan["default_branch"], plan["initial_files"],
                                                 "Start the repository")
            except patches.PatchError as exc:
                raise ToolError(str(exc)) from None
            # logged at once too: a later step may fail, and this write stays on the default branch
            self.store.log("bootstrap_initial_commit", {"repo": repo, "by": by, "sha": initial,
                                                        "paths": sorted(plan["initial_files"])})
            plan = repos.plan_on(plan, initial)
            branch = BOOTSTRAP_PREFIX + plan["fingerprint"][:16]
            response["initialCommit"] = initial
            self._refuse_existing_branch(repo, branch)
        try:
            sha = patches.bootstrap(self.config.git_url.format(repo=repo), self.config.github_token,
                                    branch, plan["base_sha"], files, "Set up software factory")
        except patches.PatchError as exc:
            raise ToolError(str(exc)) from None
        paths = sorted(files)
        try:
            pr = self.github.create_pull(repo, branch, plan["default_branch"],
                                         f"Set up software factory for {repo}",
                                         self._setup_pr_body(repo, plan, secrets))
        except GitHubError:
            pr = next((item for item in self.github.pulls_for_branch(repo, branch)
                       if item["state"] == "open"), None)
            if pr is None:
                raise
        self.store.log("bootstrap_pr", {"repo": repo, "by": by, "branch": branch,
                                        "baseSha": plan["base_sha"], "headSha": sha,
                                        "paths": paths, "prNumber": pr["number"], "initialCommit": initial})
        return dict(response, prNumber=pr["number"],
                    url=pr.get("html_url") or f"https://github.com/{repo}/pull/{pr['number']}")

    @staticmethod
    def _setup_pr_body(repo, plan, secrets):
        """The setup PR's body in the gate's format: the plan's files and each secret's status."""
        paths = sorted(plan["files"])
        updates = set(plan.get("updates", ()))
        upgraded = bool(updates)
        return "\n".join([
            "## Task", "", f"Owner-approved {'control update' if upgraded else 'bootstrap'} for https://github.com/{repo}.", "",
            "## Intent", "",
            ("Update the installed gate caller to the approved gate revision, leaving its verify "
             "command, permissions, secrets and unrelated files as they are." if upgraded else
             "Install the missing controls required for this repository to join the factory."), "",
            "## Why it was needed", "",
            ("The installed gate caller pins an older gate revision than software-factory ships." if upgraded else
             "The factory cannot run reviewed PRs without its gate and written standard."), "",
            "## Why this approach", "",
            ("The factory rewrote only the two gate revision pins in the installed workflow, "
             "preserving every other line, and left the repo's verify command untouched." if upgraded else
             "The factory prepared only the missing files from trusted templates."),
            "The owner must review and merge this setup PR; normal workers still cannot change these paths.", "",
            "## What changed", "",
            *(f"- `{path}`: {'update' if path in updates else 'install'} the factory control" for path in paths), "",
            "## Proof of work", "", "```text",
            f"$ git diff --name-only --no-renames -z {plan['base_sha']} HEAD", *paths, "```", "",
            "The factory set the gate's Actions secrets from the owner's Keychain:",
            *(f"- `{item['name']}`: {item['status']}" for item in secrets), "",
            "After the owner merges: ask Day Desk to check and enable the repo.", "",
        ])

    def _open_setup_pr(self, repo, base):
        """The one open PR from a setup branch of `repo` itself into `base`, or None."""
        found = sorted((pr for pr in self.github.open_pulls(repo, base)
                        if pr["head"]["ref"].startswith(BOOTSTRAP_PREFIX)
                        and ((pr["head"].get("repo") or {}).get("full_name") or "").lower() == repo.lower()),
                       key=lambda pr: pr["number"])
        if len(found) > 1:
            numbers = ", ".join(f"#{pr['number']}" for pr in found)
            raise ToolError(f"{repo} has several open setup PRs ({numbers}); close all but one and try again")
        return found[0] if found else None

    def _update_setup_pr(self, repo, pr, files, by):
        """Commit the plan's files on the adopted PR's branch. The new head, or None if it has them."""
        branch = pr["head"]["ref"]
        try:
            sha = patches.update_bootstrap(self.config.git_url.format(repo=repo), self.config.github_token,
                                           branch, pr["head"]["sha"], files, "Update the software factory setup")
        except patches.PatchError as exc:
            raise ToolError(f"setup PR #{pr['number']} was not updated to the current plan: {exc}; "
                            "a person must inspect it") from None
        if sha:
            self.store.log("bootstrap_pr_updated", {"repo": repo, "by": by, "branch": branch,
                                                    "prNumber": pr["number"], "headSha": sha,
                                                    "paths": sorted(files)})
        return sha

    def _refuse_existing_branch(self, repo, branch):
        if self.github.branch_head(repo, branch):
            raise ToolError(f"bootstrap branch {branch} exists without an open PR; a person must inspect it")

    def _gate_secrets(self, repo, by):
        """Set the repo's missing gate secrets from the Keychain; log only names and statuses."""
        secrets = gate_secrets.ensure(repo, self.config.github_token)
        self.store.log("bootstrap_secrets", {"repo": repo, "by": by, "secrets": secrets})
        return secrets

    def rebuild(self):
        """Intake every open `factory:ready` issue the store does not know (ADR-0009: GitHub is the truth),
        in the listed repos and in any repo where a trusted author marked one (ADR-0027)."""
        self._discover()
        found = []
        for repo in sorted(self.config.repos):
            for issue in self.github.labelled_issues(repo, workitem.READY):
                if self.store.get(workitem.workitem_id(repo, issue["number"])) is None:
                    try:
                        found.append(self.intake(repo, issue["number"])["id"])
                        if self.github.pulls_for_branch(repo, workitem.branch_for(issue["number"])):
                            self.store.log("rebuilt", {}, found[-1])  # its earlier history is gone
                    except ToolError as exc:
                        key = (repo, issue["number"], str(exc))
                        if key not in self._skipped:  # once per process, not once a minute
                            self._skipped.add(key)
                            self.store.log("rebuild_skipped", {"reason": str(exc)},
                                           workitem.workitem_id(repo, issue["number"]))
        return found

    def _discover(self):
        """Enable each repo, not listed yet, where a trusted author has an open factory:ready issue.
        A repo that fails the check gets one comment on the issue, so the author learns why."""
        authors = self.config.trusted_authors or ({self.config.owner} if self.config.owner else set())
        if not authors and "no-authors" not in self._skipped:  # once per process, not once a minute
            self._skipped.add("no-authors")
            self.store.log("discover_skipped", {"reason": "set FACTORY_TRUSTED_AUTHORS or FACTORY_OWNER, "
                                                "so the factory knows whose factory:ready issues to search for"})
        for author in sorted(authors):
            try:
                issues = self.github.labelled_issues_by(author, workitem.READY)
            except GitHubError as exc:
                key = ("search", author, str(exc))
                if key not in self._skipped:  # once per process, not once a minute
                    self._skipped.add(key)
                    self.store.log("search_failed", {"author": author, "error": str(exc)})
                continue
            for issue in issues:
                repo, number = issue["repo"], issue["number"]
                if self.config.listed(repo) is not None:
                    continue
                try:
                    self._enabled(repo, f"{author}, who marked {repo}#{number} factory:ready")
                except RepoNotReady as exc:
                    self._refuse_repo(repo, number, str(exc))
                except (ToolError, GitHubError) as exc:  # the factory's own failure: the log, not the issue
                    key = ("discover", repo, str(exc))
                    if key not in self._skipped:
                        self._skipped.add(key)
                        self.store.log("discover_failed", {"repo": repo, "error": str(exc)},
                                       workitem.workitem_id(repo, number))

    def _refuse_repo(self, repo, number, reason):
        wid = workitem.workitem_id(repo, number)
        if any(e["action"] == "repo_not_ready" and e["detail"].get("reason") == reason
               for e in self.store.history(wid)):
            return
        detail = {"reason": reason}  # the reason alone, so the next rebuild knows it was already said
        try:
            self.github.comment(repo, number, f"software-factory cannot take this issue yet: {reason}\n\n"
                                "When the repo passes the check, the factory takes the issue on its own.")
        except GitHubError as exc:  # the token may not reach the repo: say so in the log, keep ticking
            detail["commentError"] = str(exc)
        self.store.log("repo_not_ready", detail, wid)

    def tick(self, workitem_id=None):
        """Reconcile one WorkItem, or rebuild from GitHub and reconcile every live one. A WorkItem
        whose repo left FACTORY_REPOS is left as it is, and carries on if the repo comes back."""
        if workitem_id is not None:
            wi = self.store.get(workitem_id)
            if wi is None:
                raise ToolError(f"No WorkItem {workitem_id}")
            if self.config.listed(wi["repo"]) is None:
                raise ToolError(f"{wi['repo']} is not in FACTORY_REPOS or repos.json; the factory "
                                "leaves it alone")
            before, wi = self.tick_one(workitem_id)
            return {"id": workitem_id, "from": before, "to": wi["state"], "workItem": wi}
        rebuilt = self.rebuild()
        ticked = []
        for wi in self.store.all():
            if wi["state"] not in workitem.TERMINAL and self.config.listed(wi["repo"]):
                try:
                    before, after = self.tick_one(wi["id"])
                except Exception as exc:  # noqa: BLE001 - one bad WorkItem must not starve the rest
                    self.store.log("tick_error", {"error": f"{type(exc).__name__}: {exc}"}, wi["id"])
                    continue
                ticked.append({"id": wi["id"], "from": before, "to": after["state"]})
        return {"rebuilt": rebuilt, "ticked": ticked}

    def tick_one(self, workitem_id):
        """Returns (state before, WorkItem after)."""
        wi = self.store.get(workitem_id)
        before = wi["state"]
        if not self.store.acquire(workitem_id, self.holder):
            return before, wi
        try:
            facts = workitem.observe(self.github, wi)
            new = workitem.reconcile(wi, facts)
            detail = {"headSha": new.get("headSha")}
            if self._gate_merged_base(wi, new.get("headSha")):  # not a person's commit (ADR-0039)
                detail["mergedBase"] = True
            verdict = workitem.current_verdict(wi, facts)  # accepted now: keep its blocking findings
            if verdict:  # by severity: blocking() falls back to every finding, and a nit is no lesson
                detail["verdict"] = verdict["outcome"]  # the first-pass gauges read these (ADR-0083)
                detail["round"] = verdict["round"]
                detail["blocking"] = [{"fingerprint": f["fingerprint"], "problem": f["problem"]}
                                      for f in verdict["findings"] if f["severity"] == "blocking"]
            wi = self._write(wi, new, "reconcile", **detail)
            wi = self._retire_stale_wip(wi, facts)
            wi = self.act(wi, facts)
            if wi.get("wip") and (wi["state"] in workitem.TERMINAL or wi["state"] == "merged"):
                wi = self._retire_wip(wi)  # a merge or an end ends salvage (ADR-0086)
        except Conflict:
            wi = self.store.get(workitem_id)
        except (GitHubError, RelayError) as exc:
            self.store.log("tick_error", {"error": str(exc)}, workitem_id)
            wi = self.store.get(workitem_id)
        finally:
            self.store.release(workitem_id, self.holder)
        return before, wi

    def _gate_merged_base(self, wi, head):
        """Whether a new PR head, one the factory did not push, is the gate's merge of the base branch.
        A commit GitHub no longer shows or will not show counts as a person's."""
        if not wi.get("headSha") or head in (None, wi["headSha"]):
            return False
        try:
            return workitem.gate_merged_base(self.github.commit(wi["repo"], head), wi["headSha"])
        except GitHubError as exc:
            if exc.status not in (403, 404):
                raise
            return False

    def _write(self, old, new, action, **detail):
        if new == old:
            return old
        if new["state"] != "ready" and new.get("waitingOn") is not None:  # however it leaves ready,
            new = dict(new, waitingOn=None)  # the wait ends: say so, or status names a stale issue
        if new["state"] in ("human_waiting", "blocked") and not new.get("owner"):
            new = dict(new, owner=self.config.owner or new["repo"].split("/")[0])
        incident = self._next_incident(old, new, action, detail)
        if incident is not None:
            new = dict(new, incident=incident)
        detail = dict(detail, **{"from": old["state"], "to": new["state"]})
        # stored carries the same state and reason as new, so the escalation is the same either way
        escalated = new["state"] in ATTENTION and old["state"] != new["state"]
        if escalated:  # the weekly report groups these (metrics.escalation_reasons)
            detail["escalation"] = self._reason(new)
        pending = old.get("pendingRetry")
        if pending and new.get("pendingRetry") is None:
            stored = self.store.cancel_retry(new, pending["requestId"], action, detail, self.holder)
        else:
            stored = self.store.put(new)
            self.store.log(action, detail, old["id"])
        if escalated:
            self._notify(stored)
        return stored

    @staticmethod
    def _reason(wi):
        """Why a WorkItem needs a person, on one line."""
        return " ".join(str(wi.get("blockedOn") or wi.get("terminalReason") or wi["state"]).split())

    def _norm_reason(self, wi):
        """The reason as incident identity (ADR-0081): one line, with commit shas and the attempt
        count it narrates read as the same wherever they appear again."""
        reason = self._reason(wi)
        return ATTEMPT_COUNT.sub("<n> worker attempts", SHA40.sub("<sha>", reason))

    @staticmethod
    def _cause_of(entry):
        """The diagnosis cause an attempt entry carries, or "unknown"."""
        return next((cause for cause in diagnostics.CAUSES if f"diagnosis: {cause}" in entry), "unknown")

    def _incident_identity(self, new, action, detail):
        """The material facts an incident is about (ADR-0081): what escalated the WorkItem, its
        normalized reason, and the action's own facts. Only these compare; a timestamp or the
        one-answer flag never re-arms an incident by itself."""
        identity = {"action": action, "reason": self._norm_reason(new)}
        if action == "attempt_failed":
            identity["causes"] = sorted({self._cause_of(entry) for entry in new.get("attempts") or []})
        elif action == "dependency_ended":
            identity["dependency"] = detail.get("on")
            identity["end"] = detail.get("how")
        elif action == "reconcile":
            identity["gateRound"] = new.get("gateRound")
        return identity

    def _next_incident(self, old, new, action, detail):
        """The incident record the write stores, or None to leave the stored one alone (ADR-0081).
        A record with the same material identity keeps its one-answer flag — that is what stops a
        tick or a repeat of the same facts from re-arming. A first record inherits the one-answer
        rule over the history, so an escalation that predates this rule cannot be answered twice;
        a materially changed incident is a new record, with one fresh evaluation."""
        if new["state"] not in ATTENTION:
            return None
        identity = self._incident_identity(new, action, detail)
        current = old.get("incident")
        if current is not None:
            if all(current.get(key) == value for key, value in identity.items()):
                return None
            number, answered = current.get("id", 0) + 1, False
        else:
            number, answered = 1, agent_resolved(self.store.history(old["id"]))
        return dict(identity, id=number, since=now(), agentAnswered=answered)

    def _notify(self, wi):
        """Tell the owner on the issue. A failed comment is logged; it never blocks the transition."""
        reason = self._reason(wi)
        ask = ("Nothing more will happen until a person decides: call `factory_resolve_human` with "
               "`retry`, `regate` or `close`." if wi["state"] != "exhausted" else
               "The WorkItem has used its budget and stopped.")
        body = (f"**software-factory needs a person.** WorkItem `{wi['id']}` is `{wi['state']}`.\n\n"
                f"```\n{reason[:600]}\n```\n\n{ask}\n\n@{wi.get('owner') or wi['repo'].split('/')[0]}")
        try:
            self.github.comment(wi["repo"], wi["issueNumber"], body)
        except GitHubError as exc:
            self.store.log("notify_failed", {"error": str(exc)}, wi["id"])

    def act(self, wi, facts):
        if wi["state"] == "ready":
            wi = self._wait_for(wi, facts["issue"]["dependsOn"])
            if wi["state"] != "ready" or wi.get("waitingOn"):
                return wi
            held = self._paths_held_by(wi)  # two nodes that list the same files run in turn (ADR-0088)
            if held:
                self.store.log("waiting", {"on": held["id"], "paths": held["share"]}, wi["id"])
                return wi  # still ready; the next tick retries the hold
            # a retry says why the last attempt failed, or what a person said
            return self.dispatch(wi, "implement", self._human_findings(wi) + self._attempt_findings(wi),
                                 facts["issue"]["workers"])
        if wi["state"] == "human_waiting":
            return self._recheck_dependency(wi)
        # GitHub runs no workflow on a PR that conflicts with its base, so the gate would never answer
        conflict = (wi["state"] in ("pr_open", "reviewing", "changes_requested") and facts["pr"] is not None
                    and facts["pr"]["mergeable"] is False)
        if wi["state"] == "changes_requested" or conflict:  # findings travel as data (ADR-0007)
            latest = workitem.verdict_for_head(facts)
            # a redo that failed leaves the PR, and the verdict on its head, as they were
            applies = latest and (wi["state"] == "changes_requested"
                                  or conflict and latest["headSha"] == facts["pr"]["headSha"])
            findings = [{key: f[key] for key in ("fingerprint", "path", "line", "problem", "fix", "test")}
                        for f in (blocking(latest) if applies else [])]
            if conflict:  # a fix builds on the PR's head and keeps the conflict: make it again (ADR-0032)
                default = self._default_branch(wi["repo"])
                findings.insert(0, {"fingerprint": "merge-conflict", "problem": (
                    f"PR #{facts['pr']['number']} conflicts with {default}, which changed after this branch "
                    f"was made. Make the change again on the current {default}.")})
            return self.dispatch(wi, "implement" if conflict else "fix",
                                 findings + self._human_findings(wi) + self._attempt_findings(wi),
                                 facts["issue"]["workers"])
        if wi["state"] == "dispatching":  # the intent was written, the relay call may not have landed
            return self.submit(wi, self._order(wi))
        if wi["state"] == "implementing":
            return self.poll(wi)
        if wi["state"] in ("pr_open", "reviewing"):
            since = calendar.timegm(time.strptime(wi["updatedAt"], "%Y-%m-%dT%H:%M:%SZ"))
            if time.time() - since > self.config.gate_wait_seconds:
                return self._write(wi, dict(wi, state="human_waiting", blockedOn=(
                    f"no pr-gatekeeper verdict on {wi.get('headSha')} after "
                    f"{self.config.gate_wait_seconds // 60} min; check the gate workflow run")),
                    "gate_silent")
        if wi["state"] == "merged":  # GitHub skips `Closes #n` when the gate's workflow token merges
            self.github.close_issue(wi["repo"], wi["issueNumber"])
            return self._write(wi, dict(wi, state="closed"), "issue_closed", pr=wi.get("prNumber"))
        return wi

    def _wait_for(self, wi, numbers):
        """Hold a ready WorkItem until each issue its `Depends on #n` lines name has merged (ADR-0024).
        One that ended without a merge goes to a person, who may change the order with
        factory_set_dependencies (ADR-0042); a dependency that changes materially re-arms that
        incident (ADR-0081, _recheck_dependency)."""
        for number in numbers:
            status, how = self._dependency(wi["repo"], number)
            if status == "open":
                return self._write(wi, dict(wi, waitingOn=number), "waiting", on=number)
            if status == "ended":
                self._finish_recovery(wi, False)  # a wait ending in an escalation concludes any open recovery
                return self._write(wi, dict(wi, state="human_waiting", waitingOn=None, recovery=None,
                                            blockedOn=self._dependency_reason(number, how)),
                                   "dependency_ended", on=number, how=how)
        return self._write(wi, dict(wi, waitingOn=None), "dependency_merged") if wi.get("waitingOn") else wi

    def _paths_held_by(self, wi):
        """The active WorkItem of this repo that holds files this node's `Paths:` also lists
        (ADR-0088), or None: two nodes that list the same source files run one after the other.
        A node without Paths waits for nothing, and one waiting for a person holds nothing."""
        if not wi.get("paths"):
            return None
        for other in self.store.all():
            if (other["id"] == wi["id"] or other["repo"] != wi["repo"]
                    or other["state"] not in PATHS_HOLDING or not other.get("paths")):
                continue
            share = [entry for entry in wi["paths"]
                     if any(_overlaps(entry, held) for held in other["paths"])]
            if share:
                return {"id": other["id"], "share": share}
        return None

    @staticmethod
    def _dependency_reason(number, how):
        return (f"this issue depends on #{number}, which {how}. Edit or remove its "
                f"'Depends on #{number}' line, then retry; or close it")

    def _recheck_dependency(self, wi):
        """A dependency that ended left this WorkItem waiting (ADR-0024). Each tick re-reads it
        (ADR-0081): one that merged or opened again clears the wait and the WorkItem resumes, one
        that ended differently re-arms the incident for one fresh evaluation, and an unchanged one
        does nothing — a tick never re-arms anything by itself. The dependency is named by the
        incident, or read from the escalation text for a WorkItem that escalated before the field."""
        incident = wi.get("incident")
        if incident is None:
            match = re.search(r"this issue depends on #(\d+)", wi.get("blockedOn") or "")
            number = int(match.group(1)) if match else None
        else:
            number = incident.get("dependency") if incident.get("action") == "dependency_ended" else None
        if number is None:
            return wi
        status, how = self._dependency(wi["repo"], number)
        if status == "ended":
            reason = self._dependency_reason(number, how)
            if reason == (wi.get("blockedOn") or ""):
                return wi  # the same incident, unchanged: a legacy escalation gets its record
                # (backfilled with the one-answer rule) when its facts first change, not per tick
            self.store.log("recovery", self._recovery_detail(
                "incident_rearmed", "dependency", wi, wi.get("worker"), wi.get("worker"),
                succeeded=True, escalation_avoided=False, dependency=number, end=how), wi["id"])
            return self._write(wi, dict(wi, blockedOn=reason), "dependency_ended", on=number, how=how)
        resumed = {"id": (incident or {}).get("id", 0) + 1, "action": "dependency_ended",
                   "reason": self._norm_reason(wi), "dependency": number, "end": status,
                   "since": now(), "agentAnswered": True}
        self.store.log("recovery", self._recovery_detail(
            "dependency_resumed", "dependency", wi, wi.get("worker"), wi.get("worker"),
            succeeded=True, escalation_avoided=True, dependency=number, end=status), wi["id"])
        return self._write(wi, dict(wi, state="ready", blockedOn=None, incident=resumed,
                                    waitingOn=number if status == "open" else None),
                           "dependency_recovered", on=number, status=status)

    def _dependency(self, repo, number):
        """("merged" | "open" | "ended", how it ended) for issue #number. A merge is a GitHub fact; a
        dependency that is cancelled or exhausted may keep its issue open, so the store says that."""
        try:
            issue = self.github.issue(repo, number)
        except GitHubError as exc:
            if exc.status == 404:
                return "ended", "does not exist"
            raise
        if issue["state"] == "closed":
            if any(pr.get("merged_at") for pr in self.github.pulls_for_branch(repo, workitem.branch_for(number))):
                return "merged", None
            return "ended", "was closed without a merged PR"
        other = self.store.get(workitem.workitem_id(repo, number))
        if other and other["state"] in ("cancelled", "exhausted"):
            return "ended", f"ended {other['state']}: {other.get('terminalReason')}"
        return "open", None

    def _human_findings(self, wi):
        """The note from factory_resolve_human, if no pass was dispatched after it."""
        last = next((h for h in reversed(self.store.history(wi["id"]))
                     if h["action"] in ("human_resolved", "dispatch")), None)
        if last and last["action"] == "human_resolved" and last["detail"].get("note"):
            return [{"fingerprint": "human", "problem": last["detail"]["note"]}]
        return []

    def _attempt_findings(self, wi):
        """The `previous-attempt` findings a retry carries: the latest attempt and, when that one
        is not an actionable failing test, the last one before it that is. The current `attempts`
        list is cleared by the retry that unblocks a WorkItem, so the durable `attempt_failed`
        audit is the record across that boundary; a successful push ends a round, so attempts
        before it do not resurface. A retry with no actionable failure keeps the person's note as
        the only finding, as before (ADR-0078)."""
        history = self.store.history(wi["id"])
        start = 1 + max((i for i, h in enumerate(history)
                         if h["action"] in ("pushed", "pr_opened")), default=-1)
        relevant = history[start:]
        attempts = [diagnostics.entry(h["detail"].get("reason", ""),
                                      {"cause": h["detail"].get("cause"),
                                       "context": h["detail"].get("diagnosis") or ""})
                    for h in relevant if h["action"] == "attempt_failed"]
        marker = f"diagnosis: {diagnostics.ACTIONABLE}:"
        if (relevant and relevant[-1]["action"] in ("human_resolved", "allowance_retry")
                and not any(marker in attempt for attempt in attempts)):
            return []
        return diagnostics.attempt_findings(attempts)

    def _order(self, wi):
        return next(o for o in self.store.workorders(wi["id"]) if o["id"] == wi["activeWorkOrderId"])

    def _lessons(self, wi, findings=()):
        """The repo's approved lessons for the WorkOrder, newest first, or [] when there are none.
        documents.workorder caps how many ride along. With lesson ranking enabled and a usable
        answer from the provider, the winning lesson comes first; otherwise the order is the
        newest approval first, whatever the ranking may have said (jev_rank, ADR-0058)."""
        if not self.config.lessons_file:
            return []
        try:
            found = lessons.for_repo(lessons.load(self.config.lessons_file), wi["repo"])
            return jev_rank.rank(found, wi, list(findings), self.store, self.config) or found
        except (ValueError, OSError) as exc:  # OSError: a file that exists but cannot be read
            self.store.log("lessons_invalid", {"error": str(exc)}, wi["id"])
            return []  # a hand-edited or unreadable file must not stop the line; the audit log says why

    def _default_branch(self, repo):
        return self.github.repository(repo)["default_branch"]

    def _barrier(self, wi):
        """Active pause barriers holding this WorkItem (roles of the recovery graph), or [] (#233, #234).
        The check must be the last one before a worker runs: an operation may pause the graph after
        the dispatch decision and before the relay call, so submit() rechecks at the submission point."""
        return self.store.active_pauses(wi["id"])

    def dispatch(self, wi, kind, findings=(), named=()):
        with self.runtime_lock:
            return self._dispatch(wi, kind, findings, named)

    def _reviewer_families(self, repo):
        """The model families the gate's reviewers belong to (ADR-0085), read from the default
        branch's `.github/pr-gatekeeper.json` — the gate's config, which the factory reads and
        never writes. The file's absence means the gate does not run on this repo, so it has no
        reviewer family; a missing `reviewer_model` key means pr-gatekeeper's default, and an
        absent or empty `strong_reviewer_model` excludes nothing (it is off by default)."""
        text = self.github.file(repo, ".github/pr-gatekeeper.json")
        if not text:
            return set()
        try:
            gate = json.loads(text)
        except ValueError:  # a malformed config is every key missing: the gate's defaults stand
            gate = {}
        models = [gate.get("reviewer_model") or DEFAULT_REVIEWER, gate.get("strong_reviewer_model")]
        return {routing.family(model) for model in models if model}

    def _dispatch(self, wi, kind, findings=(), named=()):
        """Send one WorkOrder. `findings` are this dispatch's findingsToAddress, exactly what the
        WorkOrder will hold; _lessons passes the same to the ranker, never a reconstruction."""
        # Up to passes_per_repo passes of one repo run at once, and the runner gives them the shared
        # checkout in turns (ADR-0036). The rest wait. Two ticks at once may each start one, so the
        # limit is soft; the runner's lock is what keeps the checkout safe.
        paused = self._barrier(wi)
        if paused:  # fail-closed for the held graph; a release lets the next tick dispatch (#234)
            self.store.log("dispatch_paused", {"pausedBy": [row["requestId"] for row in paused]},
                           wi["id"])
            return wi
        running = sum(other["repo"] == wi["repo"] and other["id"] != wi["id"] and other["state"] in WORKING
                      for other in self.store.all())
        if running >= self.config.passes_per_repo:
            return wi
        orders = self.store.workorders(wi["id"])
        used = len(orders) - (wi.get("passesBefore") or 0)  # a person's retry buys a fresh budget
        if used >= self.config.max_passes:
            self._finish_recovery(wi, False)  # the budget stopped the attempt the recovery chose
            return self._write(wi, dict(wi, state="exhausted", recovery=None, terminalReason=(
                f"{used} worker passes used; FACTORY_MAX_PASSES is {self.config.max_passes}")),
                "exhausted")
        allowed = [agent for agent, _ in self.config.workers]
        unknown = [agent for agent in named if agent not in allowed]
        if unknown:  # the relay also holds agents with no sandbox; an issue must not reach them
            return self._write(wi, dict(wi, state="human_waiting", blockedOn=(
                f"the issue's Worker: line names {', '.join(unknown)}, which is not in FACTORY_WORKERS "
                f"({', '.join(allowed)}). Name one of those, or add the agent to FACTORY_WORKERS, then retry")),
                "unknown_worker", workers=unknown)
        failed = {h["detail"].get("worker") for h in self.store.history(wi["id"])
                  if h["action"] == "attempt_failed"}
        # After a timeout-class failure the next attempt prefers another model family (ADR-0081): the
        # family that just ran out of the clock is the suspect, and this preference runs before the
        # two-strike block ever sees the case. The gate reviewer's family is excluded outright (ADR-0085).
        avoid, from_worker, category = self._timeout_causes(wi)
        excluded = self._reviewer_families(wi["repo"])
        worker = routing.route(self.config.workers, self.config.local_only(wi["repo"]), failed, named,
                               avoid_families=avoid, exclude_families=excluded)
        if worker is None:
            self._finish_recovery(wi, False)  # no attempt will run, so the open recovery finds no outcome
            local_only = self.config.local_only(wi["repo"])
            candidates = [agent for agent, place in self.config.workers
                          if (place == "local" or not local_only) and (not named or agent in named)]
            if candidates and all(routing.family(agent) in excluded for agent in candidates):
                return self._write(wi, dict(wi, state="human_waiting", recovery=None, blockedOn=(
                    "every allowed worker is in the reviewer family "
                    + ", ".join(sorted(excluded)))), "no_worker")
            return self._write(wi, dict(wi, state="human_waiting", recovery=None, blockedOn=(
                f"{wi['repo']} is local-only and FACTORY_WORKERS has no local worker")), "no_worker")
        if wi.get("wallMsOverride") is not None:
            try:
                self._check_timeout(wi["wallMsOverride"])
            except RelayLimitError as exc:
                self._finish_recovery(wi, False)
                return self._write(wi, dict(wi, state="human_waiting", recovery=None, blockedOn=str(exc)),
                                   "timeout_unavailable")
        recovery = self._recovery_decision(wi, avoid, from_worker, category, worker)
        attempt = sum(1 for o in orders if o["round"] == wi.get("gateRound", 0) and o["kind"] == kind)
        base = self.github.branch_head(wi["repo"], self._default_branch(wi["repo"]))
        continuation = wi.get("wip")
        if continuation and continuation.get("kind") != kind:
            wi = self._retire_wip(wi)
            continuation = None
        if continuation:  # the rescue pass builds on the kept partial (ADR-0086)
            findings = [dict(fingerprint=fingerprint(None, PARTIAL_WORK), path=None, line=None,
                             problem=PARTIAL_WORK, fix=None, test=None)] + list(findings)
        order = documents.workorder(wi, kind, attempt, self.config, base, findings,
                                    self._lessons(wi, findings),
                                    continuation.get("sha") if continuation else None)
        order["ask"] = {"url": self.config.ask_url, "token": secrets.token_hex(16)}  # ADR-0098
        self.store.save_workorder(order)
        wi = self._write(wi, dict(wi, state="dispatching", activeWorkOrderId=order["id"], worker=worker,
                                  recovery=recovery, question=None),
                         "dispatch", workOrder=order["id"], worker=worker)
        return self.submit(wi, order)

    def submit(self, wi, order):
        paused = self._barrier(wi)  # the race guard: rechecked immediately before the worker runs (#234)
        if paused:  # the intent was written (state dispatching); a released barrier resumes it
            self.store.log("submit_paused", {"order": order["id"],
                                             "pausedBy": [row["requestId"] for row in paused]}, wi["id"])
            return wi
        task = self.relay.submit(wi["worker"], documents.worker_input(order, wi["branch"]),
                                 order["idempotencyKey"], order["budget"]["wallMs"],
                                 cwd=self.config.checkout(wi["repo"]))
        return self._write(wi, dict(wi, state="implementing", providerTaskId=task["id"]),
                           "submitted", task=task["id"])

    def worker_ask(self, token, question, wait_seconds=240):
        """A running pass asks Day Desk a question (ADR-0098). Stores it on the WorkItem and waits,
        polling the store, for factory_answer_question to answer it from another request. Returns
        {"answer": ...}, {"pending": True} when nobody answered in time, or {"gone": True} when the
        pass ended first. The token is the whole capability: one live WorkOrder."""
        found = self.store.order_by_ask_token(token)
        if found is None:
            raise ToolError("this pass is not running, so it has no ask channel")
        wi, order = found
        text = " ".join(str(question or "").split())[:2000]
        if not text:
            raise ToolError("the question is empty")
        try:  # before storing: a bad wait must not leave a question nobody answers
            wait = int(wait_seconds if wait_seconds is not None else 240)
        except (TypeError, ValueError, OverflowError):
            raise ToolError("the wait is not a number of seconds") from None
        question_id = hashlib.sha256(f"{order['id']}\n{text}".encode()).hexdigest()[:16]
        current = wi.get("question") or {}
        if current.get("text") and current.get("id") != question_id and not current.get("answer"):
            raise ToolError("a question from this pass is already waiting for an answer; wait for it, "
                            "or decide from the issue")
        if current.get("id") != question_id and not self._store_question(
                wi["id"], {"id": question_id, "text": text, "askedAt": now()}):
            return {"gone": True}
        deadline = time.time() + max(1, min(wait, 600))
        while time.time() < deadline:
            wi = self.store.get(wi["id"])
            if wi is None:
                return {"gone": True}
            current = wi.get("question") or {}
            if current.get("id") == question_id and current.get("answer"):
                return {"answer": current["answer"], "by": current.get("by")}
            if wi.get("state") not in ("dispatching", "implementing"):
                return {"gone": True}
            time.sleep(0.5)
        return {"pending": True}

    def _store_question(self, workitem_id, question):
        """Write the question, retrying the optimistic write the poller may have raced."""
        for _ in range(5):
            wi = self.store.get(workitem_id)
            if wi is None or wi.get("state") not in ("dispatching", "implementing"):
                return None
            try:
                return self._write(wi, dict(wi, question=question), "worker_question",
                                   question=question["id"])
            except Conflict:
                continue
        return None

    def answer_question(self, workitem_id, question_id, answer, by):
        """Day Desk answers the running pass's question (ADR-0098). The answer wakes the worker's
        blocked ask and stays on the WorkItem as the record of what the pass was told."""
        for _ in range(5):
            wi = self.store.get(workitem_id)
            if wi is None:
                raise ToolError(f"No WorkItem {workitem_id}")
            question = wi.get("question") or {}
            if question.get("id") != question_id:
                raise ToolError(f"{workitem_id} has no question {question_id}: a newer one replaced it")
            if question.get("answer"):
                raise ToolError(f"{workitem_id} already answered question {question_id}")
            if wi.get("state") not in ("dispatching", "implementing"):
                raise ToolError(f"{workitem_id} is not running a pass; the question is stale")
            updated = dict(question, answer=str(answer), by=by, answeredAt=now())
            try:
                return self._write(wi, dict(wi, question=updated), "question_answered",
                                   question=question_id, by=by)
            except Conflict:
                continue
        raise ToolError(f"{workitem_id} changed while the answer was written; try again")

    def poll(self, wi):
        order = self._order(wi)
        task = self.relay.get(wi["providerTaskId"])
        if task is None:
            return self._relay_lost(wi, order)
        if task["status"] in ("queued", "running"):
            return wi
        if task["status"] != "completed":
            return self.fail(wi, order, f"{task['agentId']}: task {task['status']}: {task.get('error')}")
        # The runner prints its Result last, so a truncation cut it off, and any Result line left is one
        # the harness printed
        if task.get("outputTruncated"):
            return self.fail(wi, order, f"{task['agentId']}: the relay truncated the output, so the "
                             "Result line is lost; raise AGENT_RELAY_MAX_OUTPUT_BYTES or quiet the harness")
        result = documents.parse_result(task.get("output"))
        errors = documents.check_result(result, order)
        if not errors and result["status"] != "completed":
            errors = [f"worker reported {result['status']}: {result.get('error')}"]
            if (self.config.salvage and result.get("patch")
                    and OUT_OF_TIME.search(result.get("error") or "")):
                wi = self._salvage(wi, order, result)  # keep the partial; the fail below still runs
        diagnosis = diagnostics.diagnose(order, result)
        sha, files, evidence = None, [], (result or {}).get("evidence", [])
        if not errors and order.get("remoteVerify"):
            wi, errors, evidence, waiting = self._remote_verify(wi, order, result["patch"])
            if waiting:
                return wi
        if not errors:
            try:
                sha = self.push_patch(wi, order, result["patch"])
            except patches.PatchError as exc:
                errors = [f"patch not pushed: {exc}"]
        if not errors:
            errors, files = self.check_pushed(wi, order, sha)
        if errors and all(sizing.is_ceiling_error(e) for e in errors):
            return self._size_recovery(wi, order, errors)
        if errors:
            return self.fail(wi, order, f"{task['agentId']}: " + "; ".join(errors), diagnosis=diagnosis)
        self._finish_recovery(wi, True)
        wi = self._retire_wip(wi)  # a completed Result ends salvage (ADR-0086): the scratch branch goes
        self.store.log("result", {"workOrder": order["id"], "commitSha": sha}, wi["id"])
        done = dict(wi, headSha=sha, attempts=[], activeWorkOrderId=None, providerTaskId=None, remoteVerify=None,
                    recovery=None, infraRetries=0)
        if wi.get("prNumber"):
            wi = self._write(wi, dict(done, state="reviewing"), "pushed")
            try:  # the gate lints the body against the PR's paths, which this push may have changed
                self.github.edit_pull(wi["repo"], wi["prNumber"],
                                      documents.pr_body(wi, order, files, evidence, task["agentId"]))
            except GitHubError as exc:  # the push stands; the gate's lint names what the body lacks
                self.store.log("pr_body_failed", {"error": str(exc)}, wi["id"])
            return wi
        pr = self.open_pr(wi, order, files, evidence, task["agentId"])
        return self._write(wi, dict(done, state="pr_open", prNumber=pr["number"]), "pr_opened",
                           pr=pr["number"])

    def _salvage(self, wi, order, result):
        """Keep a timed-out pass's partial work (ADR-0086): push its patch to the scratch branch
        `factory-wip/<issue>` and record it on the WorkItem, so the next implement pass bases
        itself on the kept work instead of restarting. A patch touching a protected path is not
        kept — push_patch refuses it as at any other push — and the wip moves forward at most
        WIP_MOVES_MAX times, so a pass that keeps timing out stops costing salvage pushes. The
        WorkItem as it stands is returned in every case; fail() still runs as today."""
        moves = ((wi.get("wip") or {}).get("moves") or 0) + 1
        if moves > WIP_MOVES_MAX:
            self.store.log("salvage_declined", {"reason": "the wip has moved forward twice"}, wi["id"])
            return wi
        branch = f"factory-wip/{wi['issueNumber']}"
        expected_base = ((order.get("continuationSha") or order.get("headSha"))
                         if order["kind"] == "fix" else order.get("continuationSha") or order.get("baseSha"))
        if not expected_base or result["patch"].get("baseSha") != expected_base:
            self.store.log("salvage_declined", {"reason": "the partial patch does not name the WorkOrder base",
                                                 "workOrder": order["id"]}, wi["id"])
            return wi
        try:
            sha = self.push_patch(wi, order, result["patch"], branch=branch)
        except patches.PatchError as exc:  # a refused patch is no partial to keep
            self.store.log("salvage_declined", {"reason": str(exc)}, wi["id"])
            return wi
        return self._write(wi, dict(wi, wip={"branch": branch, "sha": sha, "workOrder": order["id"],
                                             "kind": order["kind"], "headSha": order.get("headSha"),
                                             "baseSha": order.get("baseSha"), "moves": moves,
                                             "provenanceVersion": 1}), "salvaged",
                           workOrder=order["id"], sha=sha)

    def _retire_stale_wip(self, wi, facts):
        """Discard a checkpoint when the PR head or default base it continues from has moved.
        A checkpoint the in-flight pass was given as its base is kept until that pass concludes:
        its Result must apply on the checkpoint's exact SHA, and deleting the scratch branch
        mid-pass would discard the pass's work and strike the attempt."""
        wip = wi.get("wip")
        if not wip:
            return wi
        active = next((order for order in self.store.workorders(wi["id"])
                       if wi.get("activeWorkOrderId") and order["id"] == wi["activeWorkOrderId"]), None)
        if active and active.get("continuationSha") == wip.get("sha"):
            return wi
        current_base = self.github.branch_head(wi["repo"], self._default_branch(wi["repo"]))
        current_head = facts.get("pr", {}).get("headSha") if facts.get("pr") else None
        source = next((order for order in self.store.workorders(wi["id"])
                       if order["id"] == wip.get("workOrder")), None)
        current_wip = self.github.branch_head(wi["repo"], wip["branch"])
        stale = (wip.get("provenanceVersion") != 1 or source is None
                 or source.get("kind") != wip.get("kind")
                 or source.get("headSha") != wip.get("headSha")
                 or source.get("baseSha") != wip.get("baseSha")
                 or current_wip != wip.get("sha")
                 or current_base != wip.get("baseSha")
                 or (wip.get("kind") == "fix" and current_head != wip.get("headSha")))
        return self._retire_wip(wi) if stale else wi

    def _retire_wip(self, wi):
        """A completed Result, a merge or an end retires the salvage scratch branch (ADR-0086):
        delete it and clear `wip`. A branch GitHub no longer holds is already retired."""
        wip = wi.get("wip")
        if not wip:
            return wi
        try:
            self.github.delete_branch(wi["repo"], wip["branch"])
        except GitHubError as exc:
            if exc.status != 404:
                raise
        return self._write(wi, dict(wi, wip=None), "wip_retired", branch=wip["branch"])

    def _relay_lost(self, wi, order):
        """The relay no longer knows an accepted task (ADR-0081): an infrastructure loss, not the
        code's failed attempt. The facts are reconciled first — this tick rebuilt them, workers hold
        no credential, and a pending remote-verify scratch branch is checked — so nothing external
        can have happened; then the same WorkOrder is re-queued under the same idempotency key, so
        the relay can hold at most one task for it. The re-queue is bounded per WorkItem
        (RELAY_RETRIES_MAX); beyond the bound, or with any uncertainty, the ordinary failed attempt
        stands."""
        retries = wi.get("infraRetries") or 0
        if wi.get("remoteVerify") is not None:
            return self.fail(wi, order, "the relay no longer knows the task (restarted?) while a remote "
                                        "verify was pending, so its scratch branch cannot be reconciled")
        if retries >= RELAY_RETRIES_MAX:
            return self.fail(wi, order, f"the relay no longer knows the task (restarted?); it was re-queued "
                                        f"{retries} times already")
        self._finish_recovery(wi, False)  # a previous re-queue was lost again; its outcome is now known
        detail = self._recovery_detail("relay_lost", "relay lost the task", wi, wi.get("worker"),
                                       wi.get("worker"), task=wi.get("providerTaskId"),
                                       retries=retries + 1)
        self.store.log("recovery", detail, wi["id"])
        record = {"id": detail["recoveryId"], "action": "relay_lost", "category": detail["category"],
                  "fromWorker": wi.get("worker"), "toWorker": wi.get("worker"), "at": now()}
        return self._write(wi, dict(wi, state="dispatching", providerTaskId=None, recovery=record,
                                    infraRetries=retries + 1), "relay_lost",
                           task=wi.get("providerTaskId"), retries=retries + 1)

    def _remote_verify(self, wi, order, patch):
        """Have a GitHub macOS runner run the repo's verify on the patch (ADR-0045), without blocking the
        tick. The first call pushes the patch to a scratch branch and keeps it in `remoteVerify`; each
        later call looks for the run. Returns (WorkItem, errors, evidence, waiting): errors say why the
        attempt failed, evidence is the passing run, and waiting means the WorkItem is held for the run
        or for the owner."""
        pending = wi.get("remoteVerify")
        repo = wi["repo"]
        if pending is None:
            base = patch.get("baseSha") or (order.get("continuationSha") or order["headSha"]
                                             if order["kind"] == "fix" else
                                             order.get("continuationSha") or order["baseSha"])
            if self.github.file(repo, REMOTE_VERIFY_WORKFLOW, base) is None:  # the scratch branch is built on it
                reason = (f"{repo} has no {REMOTE_VERIFY_WORKFLOW}, which runs its verify on a macOS runner: "
                          "add it (docs/RUNBOOK.md, Remote verify setup), then retry")
                self._finish_recovery(wi, False)
                return self._write(wi, dict(wi, state="human_waiting", activeWorkOrderId=None,
                                            providerTaskId=None, recovery=None, blockedOn=reason),
                                   "remote_verify_unavailable", error=reason), [], [], True
            branch = f"factory-verify/{wi['issueNumber']}-{len(self.store.workorders(wi['id']))}"
            try:
                sha = self.push_patch(wi, order, patch, branch, force=True)
            except patches.PatchError as exc:
                return wi, [f"patch not pushed: {exc}"], [], False
            wi = self._write(wi, dict(wi, remoteVerify={"branch": branch, "sha": sha, "startedAt": now()}),
                             "remote_verify_started", branch=branch, sha=sha)
            return wi, [], [], True
        since = calendar.timegm(time.strptime(pending["startedAt"], "%Y-%m-%dT%H:%M:%SZ"))
        late = time.time() - since > self.config.remote_verify_seconds
        runs, query_error = [], None
        try:
            runs = [run for run in self.github.workflow_runs(repo, pending["sha"], "factory-verify")
                    if run["status"] == "completed"]
        except GitHubError as exc:  # the deadline applies whatever the query does
            query_error = str(exc)
            self.store.log("remote_verify_query_failed", {"error": query_error}, wi["id"])
        if not runs and not late:
            return wi, [], [], True
        try:  # the scratch branch has done its job; a stale one is harmless, so a failure is only logged
            self.github.delete_branch(repo, pending["branch"])
        except GitHubError as exc:
            self.store.log("scratch_branch_not_deleted", {"branch": pending["branch"], "error": str(exc)}, wi["id"])
        if query_error:
            return wi, [f"remote verify: the runs of factory-verify could not be read ({query_error}); check "
                        "that FACTORY_GITHUB_TOKEN may read Actions (docs/RUNBOOK.md, Remote verify setup)"], [], False
        if not runs:
            return wi, [f"remote verify: no finished run of factory-verify on {pending['sha'][:12]} after "
                        f"{self.config.remote_verify_seconds // 60} min; check that workflow's runs"], [], False
        run = runs[0]
        if run.get("conclusion") == "success":
            return wi, [], [{"kind": "test", "command": "factory-verify workflow (GitHub macOS runner)",
                             "exit": 0, "tail": f"run {run.get('html_url') or run['id']}: success"}], False
        tail = " ".join(self.github.run_log_tail(repo, run["id"], 1500).replace("```", "").split())
        return wi, [f"remote verify {run.get('conclusion')}: {tail}"], [], False

    def push_patch(self, wi, order, patch, branch=None, force=None):
        """Apply the Result's patch on the WorkOrder's base and push it to the WorkItem's branch
        (ADR-0017), or to a scratch `branch`. Returns the pushed commit."""
        base = patch.get("baseSha") or (order.get("continuationSha") or order["headSha"]
                                         if order["kind"] == "fix" else
                                         order.get("continuationSha") or order["baseSha"])
        limits = order["constraints"]
        return patches.apply(self.config.git_url.format(repo=wi["repo"]), self.config.github_token,
                             branch or wi["branch"], base, patch, f"{wi['goal']} (#{wi['issueNumber']})\n\n"
                             f"WorkOrder {order['id']}", force=order["kind"] != "fix" if force is None else force,
                             protected=limits["protectedPaths"],
                             limits=(self._default_branch(wi["repo"]), limits["maxChangedLines"],
                                     limits["maxChangedFiles"],
                                     limits.get("maxChangedLinesAdvisory", True),
                                     self._source_spec(limits)))

    @staticmethod
    def _source_spec(limits):
        """The source-only sizing spec a WorkOrder carries, or None for an older all-lines one."""
        return sizing.source_spec(limits)

    def check_pushed(self, wi, order, sha):
        """Guards on what GitHub holds after the push, never on what the worker says it did."""
        head = self.github.branch_head(wi["repo"], wi["branch"])
        if head != sha:
            return [f"{wi['branch']} is at {head}, not the pushed {sha}"], []
        files = self.github.changed_files(wi["repo"], self._default_branch(wi["repo"]), head)
        limits = order["constraints"]
        errors = [f"changes protected path {name}" for f in files  # a rename moves the old path too
                  for name in (f["filename"], f.get("previous_filename")) if name
                  and patches.is_protected(name, limits["protectedPaths"])]
        allowed = limits.get("allowedPaths")
        if allowed:  # the node's own fence (ADR-0087); a rename counts, as at the protected check
            outside = sorted({name for f in files
                              for name in (f["filename"], f.get("previous_filename")) if name
                              and sizing.kind(name) == "source"
                              and not any(fnmatch.fnmatchcase(name, glob) for glob in allowed)})
            errors += [f"changes {name}, outside this node's Paths of {', '.join(sorted(allowed))}"
                       for name in outside]
        if len(files) > limits["maxChangedFiles"]:
            errors.append(f"{self._changed_lines(files)} lines in {len(files)} files exceeds the "
                          f"WorkOrder constraint of {limits['maxChangedFiles']} files")
            return errors, files
        source = self._source_spec(limits)
        totals = self._changed_totals(files)
        if source:
            if totals["source"] > source["ceiling"]:
                errors.append(
                    f"{sizing.SIZE_CEILING_MARKER}: the whole pull request changes {totals['source']} "
                    f"source lines ({totals['tests']} test, {totals['docs']} doc) in {totals['files']} "
                    f"files, over the {source['ceiling']}-line reviewable ceiling; this is a size-only "
                    "excess, not a failed verify")
            elif totals["source"] > source["target"] and not source["advisory"]:
                errors.append(f"the proposed pull request changes {totals['source']} source lines "
                              f"({totals['tests']} test, {totals['docs']} doc); the WorkOrder allows "
                              f"{source['target']} source lines")
            elif totals["source"] > source["target"]:
                self.store.log("pr_size_advisory", {"source": totals["source"], "tests": totals["tests"],
                                                    "docs": totals["docs"], "total": totals["total"],
                                                    "target": source["target"], "files": totals["files"]},
                               wi["id"])
        elif totals["total"] > limits["maxChangedLines"]:
            # Advisory (ADR-0070): recorded, not failed. The file count, protected paths and the
            # runner's patch-byte ceiling stay binding.
            self.store.log("pr_size_advisory", {"lines": totals["total"],
                                                "target": limits["maxChangedLines"],
                                                "files": totals["files"]}, wi["id"])
        return errors, files

    @staticmethod
    def _changed_totals(files):
        return sizing.measure((f["filename"], f.get("additions"), f.get("deletions")) for f in files)

    @classmethod
    def _changed_lines(cls, files):
        return cls._changed_totals(files)["total"]

    def _size_recovery(self, wi, order, errors):
        """A size-only excess becomes a bounded, acceptance-preserving decomposition, never a
        fabricated success and never a fresh budget (ADR-0079). The plan is pure; creating the
        replacement issues is a material scope change, so it still needs the owner's approval, and
        an approved allowance never buys new titles-and-bodies. The journal, graph and dependent
        cleanup run only when the owner activates the plan through factory_replace_issue."""
        plan = workitem.decomposition_plan(order)
        limits = order["constraints"]
        source = self._source_spec(limits) or {}
        self.store.log("size_recovery", {
            "sourceCeiling": source.get("ceiling"), "slices": plan, "errors": list(errors),
            "authorized": self._replacement_authorized(wi),
        }, wi["id"])
        reason = (f"the whole pull request is over the {source.get('ceiling')}-source-line reviewable "
                  f"ceiling; this is size-only, so the factory proposes {len(plan)} acceptance-preserving "
                  "sequential slice(s). Creating the replacement issues is a scope change, so the owner "
                  "approves it: call factory_replace_issue with the slices (ownerAsked), or "
                  "factory_resolve_human to retry or close. No worker pass or budget was spent.")
        new = dict(wi, state="human_waiting", blockedOn=reason, activeWorkOrderId=None,
                   providerTaskId=None, remoteVerify=None, recovery=None)
        self._finish_recovery(wi, False)
        return self._write(wi, new, "size_recovery_escalated", slices=plan)

    def _replacement_authorized(self, wi):
        """Whether an owner-approved scoped allowance already covers a replacement for `wi`. The
        plan still needs ownerAsked to open issues, so this only records that the allowance exists."""
        return any("replace" in entry["actions"] and entry["remaining"] >= 1
                   and entry.get("scope") and entry["scope"]["repo"] == wi["repo"]
                   and (not entry["scope"]["items"] or wi["id"] in entry["scope"]["items"])
                   for entry in self.store.allowances())

    def open_pr(self, wi, order, files, evidence, worker):
        """Adopt the open PR on the branch if there is one (ADR-0003), else open it."""
        for pr in self.github.pulls_for_branch(wi["repo"], wi["branch"]):
            if pr["state"] == "open":
                return pr
        return self.github.create_pull(wi["repo"], wi["branch"], self._default_branch(wi["repo"]),
                                       f"{wi['goal']} (#{wi['issueNumber']})",
                                       documents.pr_body(wi, order, files, evidence, worker))

    def _recovery_budget(self, wi, strikes=None):
        """What a recovery decision costs, for the audit (ADR-0081): the passes spent against the
        budget and the strikes the attempts hold. A recovery decision charges neither; `strikes`
        overrides the document's count for a decision made on a failure the document does not hold
        yet (a deferral, whose failure is being recorded by the same write)."""
        return {"passesUsed": len(self.store.workorders(wi["id"])) - (wi.get("passesBefore") or 0),
                "maxPasses": self.config.max_passes,
                "strikes": len(wi.get("attempts") or []) if strikes is None else strikes}

    def _recovery_detail(self, action, category, wi, from_worker, to_worker, succeeded=None,
                         escalation_avoided=None, strikes=None, **extra):
        """One autonomous recovery decision or outcome, as the audit stores it (ADR-0081): the
        failure category, the action, the worker before and after, the budget impact, whether the
        recovery succeeded and whether it spared a person. `succeeded` and `escalationAvoided` are
        null on a decision whose outcome is still open; _finish_recovery says it."""
        detail = {"recoveryId": uuid.uuid4().hex[:12], "action": action, "category": category,
                  "fromWorker": from_worker, "toWorker": to_worker,
                  "budget": self._recovery_budget(wi, strikes),
                  "succeeded": succeeded, "escalationAvoided": escalation_avoided}
        detail.update(extra)
        return detail

    def _finish_recovery(self, wi, succeeded):
        """The outcome of the open recovery decision (ADR-0081): logged when the attempt it chose
        concluded. A result that stood spares a person; a failed result does not."""
        record = wi.get("recovery")
        if not record:
            return
        self.store.log("recovery_result", {"recoveryId": record.get("id"), "action": record.get("action"),
                                           "category": record.get("category"),
                                           "fromWorker": record.get("fromWorker"),
                                           "toWorker": record.get("toWorker"),
                                           "succeeded": succeeded, "escalationAvoided": succeeded},
                       wi["id"])

    def _timeout_causes(self, wi):
        """The model families, the worker and the category of the current budget's timeout-class
        failures (ADR-0081), read from the durable `attempt_failed` audit rows — one per attempt,
        so they outlive the retry that clears `attempts`. Returns (families, fromWorker, category)."""
        attempts = len(wi.get("attempts") or [])
        if not attempts:
            return set(), None, None
        entries = [h for h in self.store.history(wi["id"]) if h["action"] == "attempt_failed"][-attempts:]
        families, from_worker, category = set(), None, None
        for entry in entries:
            detail = entry["detail"]
            worker = detail.get("worker")
            if not worker:
                continue
            cause = detail.get("cause")
            if cause == "harness timeout" or OUT_OF_TIME.search(detail.get("reason") or ""):
                families.add(routing.family(worker))
                from_worker, category = worker, cause or "timeout (unknown cause)"
        return families, from_worker, category

    def _recovery_decision(self, wi, avoid, from_worker, category, worker):
        """Record the worker-swap decision (ADR-0081) and return the open recovery for the WorkItem,
        or None when the chosen worker is inside the avoided families: no swap, one audit row. A
        deferral or relay loss already open for this very worker keeps its record — the dispatch is
        the attempt that recovery chose."""
        pending = wi.get("recovery")
        if pending:
            if pending.get("toWorker") == worker:
                return pending
            self._finish_recovery(wi, False)  # the attempt it chose will not run as chosen
        if not avoid:
            return None
        swapped = routing.family(worker) not in avoid
        detail = self._recovery_detail("worker_swap", category, wi, from_worker, worker, swapped=swapped)
        self.store.log("recovery", detail, wi["id"])
        if not swapped:
            return None
        return {"id": detail["recoveryId"], "action": "worker_swap", "category": category,
                "fromWorker": from_worker, "toWorker": worker, "at": now()}

    def _defer_block(self, wi, order, attempts, reason, diagnosis):
        """Instead of blocking on a second failure, spend the policy's next authorized attempt
        (ADR-0081): after a worker ran out of the clock, when routing would still choose a worker
        this WorkItem has never tried — the failover the policy already covers — that attempt runs
        before the block, once per budget. The block, the pass budget and the strikes are otherwise
        untouched. Returns the WorkItem to write, or None when the block stands."""
        if not (diagnosis.get("cause") == "harness timeout" or OUT_OF_TIME.search(reason or "")):
            return None  # a real failure, not the worker's clock: the block stands
        if self._deferred_already(wi):
            return None
        families, _, _ = self._timeout_causes(wi)
        families.update({routing.family(wi.get("worker") or "")})
        families.discard("")  # an unknown worker names no family to avoid
        try:
            issue = self.github.issue(wi["repo"], wi["issueNumber"])
        except GitHubError:  # the issue's Worker: lines cannot be read: the block stands
            return None
        failed = {h["detail"].get("worker") for h in self.store.history(wi["id"])
                  if h["action"] == "attempt_failed"}
        failed.add(wi.get("worker"))  # this very attempt just failed, whatever the audit has logged
        failed.discard(None)
        worker = routing.route(self.config.workers, self.config.local_only(wi["repo"]), failed,
                               workitem.workers_named(issue.get("body")), avoid_families=families,
                               exclude_families=self._reviewer_families(wi["repo"]))
        if worker is None or worker in failed:
            return None  # nobody untried the policy would choose: the block stands
        category = diagnosis.get("cause") or "timeout (unknown cause)"
        detail = self._recovery_detail("block_deferred", category, wi, wi.get("worker"), worker,
                                       strikes=len(attempts))  # this failure is the second strike
        self.store.log("recovery", detail, wi["id"])
        return dict(wi, attempts=attempts, activeWorkOrderId=None, providerTaskId=None, remoteVerify=None,
                    state="changes_requested" if order["kind"] == "fix" else "ready", blockedOn=None,
                    recovery={"id": detail["recoveryId"], "action": "block_deferred",
                              "category": category, "fromWorker": wi.get("worker"),
                              "toWorker": worker, "at": now()})

    def _deferred_already(self, wi):
        """Whether the block was already deferred once in this budget: the boundary is the last
        write that reset `attempts` (BUDGET_RESETS)."""
        for entry in reversed(self.store.history(wi["id"])):
            if entry["action"] in BUDGET_RESETS:
                return False
            if entry["action"] == "recovery" and entry["detail"].get("action") == "block_deferred":
                return True
        return False

    def fail(self, wi, order, reason, diagnosis=None):
        """Record the attempt; two failed attempts block the WorkItem with the evidence, unless the
        failure ran out of the clock and routing still has a worker this WorkItem never tried: that
        recovery attempt is granted before the block (ADR-0081, _defer_block). The diagnosis
        (diagnostics.diagnose) rides with the reason: an evidence-backed cause with its sanitized
        bounded context, nothing when a cause stays unknown. A diagnosis that names the environment
        (diagnostics.environment_fault) pauses the WorkItem on the first attempt instead: a second
        pass would meet the same broken environment (ADR-0051)."""
        diagnosis = diagnosis or {"cause": None, "context": ""}
        self._finish_recovery(wi, False)  # the attempt a recovery chose just concluded, and did not stand
        detail = dict(reason=reason, diagnosis=diagnosis["context"], worker=wi.get("worker"))
        if diagnosis["cause"]:
            detail["cause"] = diagnosis["cause"]
        attempts = list(wi.get("attempts") or []) + [diagnostics.entry(reason, diagnosis)]
        new = dict(wi, attempts=attempts, activeWorkOrderId=None, providerTaskId=None, remoteVerify=None,
                   recovery=None, state="changes_requested" if order["kind"] == "fix" else "ready")
        if diagnostics.environment_fault(diagnosis["cause"]):
            # A confirmed environment fault pauses for repair (ADR-0051): a second pass, whichever
            # worker it lands on, would meet the same broken checkout, so nobody runs until a person
            # repairs it and retries.
            new.update(state="human_waiting", blockedOn=(
                f"a worker's environment failed ({diagnosis['cause']}). Repair the environment, "
                "then call factory_resolve_human with retry; no worker runs until then."))
        elif len(attempts) >= 2:
            deferred = self._defer_block(wi, order, attempts, reason, diagnosis)
            if deferred is not None:
                new = deferred
            else:
                new.update(state="blocked",
                           blockedOn=f"{len(attempts)} worker attempts at the {order['kind']} WorkOrder failed. "
                                     "Fix the cause named in attempts, then call factory_resolve_human.")
        return self._write(wi, new, "attempt_failed", **detail)

    def resolve_human(self, workitem_id, resolution, note, by, agent=False, owner_asked=False):
        """A person's answer to human_waiting, blocked or exhausted (ADR-0006): retry, regate or close.
        Day Desk, driving (`agent`), answers each incident once — the escalation and its material facts;
        a materially changed incident earns one fresh answer (ADR-0030, ADR-0081). After that the owner
        decides. A person's retry starts a fresh budget of passes and Day Desk's keeps the budget it has
        (ADR-0035), so Day Desk may not retry a WorkItem that used its budget on its own. When the owner
        asked in chat (`owner_asked`, ADR-0043, extended by ADR-0046), it is the owner's decision, not
        Day Desk's: it answers a WorkItem Day Desk already answered, for any of the three resolutions,
        and a retry buys a fresh budget."""
        if owner_asked and not agent:
            raise ToolError("ownerAsked is Day Desk's flag for a decision the owner asked for in chat; the "
                            "owner's own call does not need it")
        if not self.store.acquire(workitem_id, self.holder):
            raise ToolError(f"{workitem_id} is being ticked; try again")
        try:
            wi = self.store.get(workitem_id)
            if wi is None or wi["state"] not in ("human_waiting", "blocked", "exhausted"):
                raise ToolError(f"{workitem_id} is not waiting on a person")
            passes = len(self.store.workorders(workitem_id))
            spent = passes - (wi.get("passesBefore") or 0) >= self.config.max_passes
            if agent and not owner_asked and (wi["state"] == "exhausted" or resolution == "retry" and spent):
                raise ToolError(f"{workitem_id} used its budget of passes, so the owner decides whether to spend "
                                f"more: factory retry|close {workitem_id} <note>")
            if agent and not owner_asked and not agent_may_resolve(wi, self.store.history(workitem_id)):
                if wi.get("incident") is not None:
                    raise ToolError(f"Day Desk already answered {workitem_id}'s current escalation; the tool "
                                    f"re-arms it only when its blocking facts change, so the owner decides now: "
                                    f"factory retry|regate|close {workitem_id} <note>")
                raise ToolError(f"Day Desk already resolved {workitem_id} once, so the owner decides now: "
                                f"factory retry|regate|close {workitem_id} <note>")
            rerun = {}
            if resolution == "retry":
                new = dict(wi, state="changes_requested" if wi.get("prNumber") else "ready", terminalReason=None,
                           passesBefore=wi.get("passesBefore") if agent and not owner_asked else passes)
            elif resolution == "regate" and wi.get("prNumber"):
                regated_at = now()  # before the edit, so the run the edit starts begins after it
                rerun = {"gateRerun": self._rerun_gate(wi)}
                new = dict(wi, state="reviewing", **({"regatedAt": regated_at} if rerun["gateRerun"] else {}))
            elif resolution == "close":
                new = dict(wi, state="cancelled", terminalReason=f"closed by {by}: {note}")
            else:
                raise ToolError("resolution must be retry, regate (needs a PR) or close")
            if agent and not owner_asked and wi.get("incident") is not None:
                # Day Desk's answer is spent on this incident; the next one it may answer is a new record
                new = dict(new, incident=dict(wi["incident"], agentAnswered=True))
            return self._write(wi, dict(new, blockedOn=None, attempts=[], recovery=None, pendingRetry=None,
                                        infraRetries=0), "human_resolved",
                               resolution=resolution, note=note, by=by, **rerun, **({"agent": True} if agent else {}),
                               **({"ownerAsked": True} if owner_asked else {}))
        finally:
            self.store.release(workitem_id, self.holder)

    def _rerun_gate(self, wi):
        """Start a gate run on the open PR's head, unless a run is going or a verdict newer than the
        accepted one is there: a regate after the gate failed itself would otherwise wait for a run
        that never comes. An edit of the PR body starts one, and the factory's token may make it
        where it may not dispatch a workflow. Returns whether it edited the body."""
        facts = workitem.observe(self.github, wi)
        running = facts["gate"] and facts["gate"]["status"] != "completed"
        if not facts["pr"] or facts["pr"]["state"] != "open" or running or workitem.current_verdict(wi, facts):
            return False
        number = facts["pr"]["number"]
        body = self.github.pull(wi["repo"], number).get("body") or ""
        seen = REGATE_MARKER.search(body)
        marker = f"<!-- factory regate {int(seen.group(1)) + 1 if seen else 1} -->"
        self.github.edit_pull(wi["repo"], number,
                              REGATE_MARKER.sub(marker, body) if seen else f"{body.rstrip()}\n\n{marker}\n")
        return True

    def approve_plan_allowance(self, plan_id, actions, remaining, by, repo, items=None):
        """The owner's one approval of bounded plan-level recovery (ADR-0069): within the plan's
        scope, Day Desk may spend the allowance on evidence-based retries and on replacements that
        name open issues of the same repo, up to `remaining` uses, without the owner answering each
        one and without falsely setting ownerAsked. The approval is durable in the store and one per
        plan id: a second approval of the same plan refuses, and
        nothing unlimited is approved. The approval names its membership too (#274): the repo the
        plan covers, and optionally the exact WorkItem members it allows; a spend outside that
        membership refuses, and an approval without one cannot happen. Safety stops and confirmed
        environment faults stay with a person whatever the allowance says.
        """
        if not isinstance(by, str) or not by.strip() or not isinstance(plan_id, str) or not plan_id.strip():
            raise ToolError("approving a plan allowance needs its plan identity and the name of the "
                            "person who approved it")
        if not isinstance(repo, str) or not repo.strip():
            raise ToolError("approving a plan allowance needs the repo the plan covers, so no "
                            "allowance spends outside its owner-approved membership")
        if items is not None and (not isinstance(items, list) or not items
                                  or any(not isinstance(item, str) for item in items)):
            raise ToolError("items, when named, is a non-empty list of the plan's WorkItem ids, "
                            f"like {[f'{repo}#1']}; a repo-level approval omits it")
        scope_repo = repo.strip()
        if items and any(not item.startswith(f"{scope_repo}#") or not item[len(scope_repo) + 1:].isdigit()
                         for item in items):
            raise ToolError(f"each member of the plan is a WorkItem id of {scope_repo}, like "
                            f"{scope_repo}#1; the membership binds the members, not another repo's")
        scope = {"repo": scope_repo, "items": sorted(set(items)) if items else []}
        if len(scope["items"]) > MAX_PLAN_ISSUES:
            raise ToolError(f"a plan allowance names at most {MAX_PLAN_ISSUES} exact members; "
                            "scope the rest to the repo")
        if (not isinstance(actions, list) or not all(isinstance(a, str) for a in actions)
                or not actions or len(set(actions)) != len(actions)
                or any(a not in RECOVERY_ACTIONS for a in actions)):
            raise ToolError(f"actions is a non-empty list drawn from {RECOVERY_ACTIONS}, once each")
        if isinstance(remaining, bool) or not isinstance(remaining, int) or not 1 <= remaining <= MAX_ALLOWANCE:
            raise ToolError(f"remaining is 1 to {MAX_ALLOWANCE} recovery uses; nothing unlimited "
                            "is approved")
        plan_id = plan_id.strip()
        try:
            self.store.approve_allowance(plan_id, actions, remaining, by, scope=scope)
        except Conflict as exc:
            raise ToolError(str(exc)) from None
        self.store.log("allowance_approved", {"planId": plan_id, "actions": actions,
                                              "remaining": remaining, "by": by, "scope": scope})
        return {"planId": plan_id, "actions": actions, "remaining": remaining, "scope": scope}

    def queue_retry(self, workitem_id, plan_id, request_id, by, evidence, agent=False):
        """Persist one scoped Day Desk retry decision without spending or dispatching it."""
        fields = (workitem_id, plan_id, request_id, by, evidence)
        if not all(isinstance(value, str) and value.strip() for value in fields):
            raise ToolError("queueing a retry needs the WorkItem, plan id, requestId, caller and evidence")
        if len(request_id) > 128 or len(plan_id) > 128 or len(by) > 100 or len(evidence) > 5000:
            raise ToolError("requestId and planId are at most 128 characters, by is at most 100, and "
                            "evidence is at most 5000")
        workitem_id, plan_id, request_id, by = (value.strip() for value in
                                                 (workitem_id, plan_id, request_id, by))
        if agent and by.lower() != "day-desk":
            raise ToolError("Day Desk queues retries with `day-desk` as by; it cannot act as the owner")
        evidence = diagnostics.sanitize(evidence)
        if not evidence:
            raise ToolError("retry evidence is empty after sanitization")
        if not self.store.acquire(workitem_id, self.holder):
            return {"status": "retryable", "reason": "workitem_busy", "requestId": request_id}
        try:
            wi = self.store.get(workitem_id)
            inputs = {"workItemId": workitem_id, "planId": plan_id, "by": by, "evidence": evidence}
            if self.store.request(request_id):
                replay = self.store.queue_retry({"id": workitem_id}, request_id, inputs, self.holder)
                return {"status": replay["phase"], "requestId": request_id, "workItem": replay["workItem"]}
            if wi is None:
                raise ToolError(f"No WorkItem {workitem_id}")
            if wi["state"] not in ("human_waiting", "blocked"):
                raise ToolError(f"{workitem_id} is {wi['state']}; a retry queues only while it waits on a person")
            if wi.get("waitingOn") is not None or self._worker_live(wi):
                raise ToolError(f"{workitem_id} is waiting on a dependency or has a live worker")
            stop = self._safety_stop(wi)
            if stop:
                raise ToolError(f"{workitem_id}'s escalation falls under a safety stop ({stop}); the owner decides")
            cause = self._env_paused(wi)
            if cause:
                raise ToolError(f"{workitem_id}'s environment failed ({cause}) and is not repaired; the owner decides")
            used = len(self.store.workorders(workitem_id)) - (wi.get("passesBefore") or 0)
            if used >= self.config.max_passes:
                raise ToolError(f"{workitem_id} used its {used} passes of budget; only the owner's retry buys "
                                "a fresh budget")
            allowance = self.store.allowance(plan_id)
            scope = allowance.get("scope") if allowance else None
            if not allowance or "retry" not in allowance["actions"] or not scope \
                    or allowance["remaining"] < 1:
                raise ToolError(f"plan allowance {plan_id!r} does not currently cover this retry")
            if wi["repo"] != scope["repo"] or scope["items"] and workitem_id not in scope["items"]:
                raise ToolError(f"plan allowance {plan_id!r} does not cover {workitem_id}")
            incident = wi.get("incident") or {}
            pending = {"requestId": request_id, "planId": plan_id, "by": by, "evidence": evidence,
                       "status": "queued", "queuedAt": now(), "waitingState": wi["state"],
                       "incidentId": incident.get("id"), "gateRound": wi.get("gateRound", 0),
                       "headSha": wi.get("headSha")}
            queued = self.store.queue_retry(dict(wi, pendingRetry=pending), request_id, inputs, self.holder)
            return {"status": queued["phase"], "requestId": request_id, "workItem": queued["workItem"]}
        except LeaseBusy:
            return {"status": "retryable", "reason": "workitem_busy", "requestId": request_id}
        except Conflict as exc:
            raise ToolError(str(exc)) from None
        finally:
            self.store.release(workitem_id, self.holder)

    def use_allowance(self, workitem_id, plan_id, action, note, by):
        """Day Desk's recovery decision inside an owner-approved plan allowance (ADR-0069): a retry
        on the decision evidence, without ownerAsked and without spending Day Desk's one own answer.
        The per-item pass budget stays as it is: an allowance retry buys no fresh budget, and when
        the allowance itself is exhausted the WorkItem goes back to the owner. A replacement is a
        different tool: factory_replace_issue carries the allowance's planId.
        """
        if not all(isinstance(value, str) and value.strip()
                   for value in (workitem_id, plan_id, action, note, by)):
            raise ToolError("using an allowance needs the WorkItem, the plan id, the action, the "
                            "decision evidence and who decided")
        if not self.store.acquire(workitem_id, self.holder):
            raise ToolError(f"{workitem_id} is being ticked; try again")
        try:
            wi = self.store.get(workitem_id)
            if wi is None or wi["state"] not in ("human_waiting", "blocked"):
                raise ToolError(f"{workitem_id} is not waiting on a person")
            stop = self._safety_stop(wi)
            if stop:
                raise ToolError(f"{workitem_id}'s escalation falls under a safety stop ({stop}); the "
                                "owner decides that: factory retry|regate|close " + workitem_id)
            cause = self._env_paused(wi)
            if cause:
                raise ToolError(f"{workitem_id}'s environment failed ({cause}); the fault is confirmed "
                                "and not repaired, so no recovery runs: ask the owner to repair it, "
                                "then answer with factory_resolve_human")
            if action == "replace":
                raise ToolError("an allowance replacement runs through factory_replace_issue, with "
                                "this planId as allowancePlanId")
            # Budget preflight (#274): an owner-approved allowance recovers an attempt, never buys
            # fresh budget; an item that used its passes must go back to the owner, so the refusal
            # happens before the count could be charged for a state nothing could ever reach.
            used = len(self.store.workorders(workitem_id)) - (wi.get("passesBefore") or 0)
            if used >= self.config.max_passes:
                raise ToolError(f"{workitem_id} used its {used} passes of budget, the allowance buys "
                                f"none fresh, dispatch would stop it, so the owner decides: "
                                f"factory retry|close {workitem_id} <note>")
            try:
                spent = self.store.spend_allowance(plan_id, action, workitem_id=workitem_id,
                                                   evidence=diagnostics.sanitize(note))
            except Conflict as exc:
                raise ToolError(str(exc)) from None
            new = dict(wi, state="changes_requested" if wi.get("prNumber") else "ready",
                       blockedOn=None, attempts=[], recovery=None, infraRetries=0, terminalReason=None)
            return self._write(wi, new, "allowance_retry", planId=plan_id, evidence=note, by=by,
                               remaining=spent["remaining"])
        finally:
            self.store.release(workitem_id, self.holder)

    def authorize_setup_merge(self, repo, verify, by):
        """The owner's explicit grant that this repo's canonical setup PR may merge without a
        person (ADR-0074): it pins the plan's digest, computed from GitHub now, and the verify
        command when the gate config is still missing. One row per repo; re-approving for a
        changed plan replaces the row, and both land in the audit. Revoking is explicit."""
        if not all(isinstance(value, str) and value.strip() for value in (repo, by)):
            raise ToolError("authorizing a setup merge needs the repo and who grants it")
        try:
            plan = repos.bootstrap_plan(self.github, repo, verify)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        self.store.approve_setup_merge(plan["repo"], plan["fingerprint"], (verify or "").strip(), by)
        return {"repo": plan["repo"], "fingerprint": plan["fingerprint"], "authorizedBy": by,
                "changedPaths": sorted(plan["files"])}

    def revoke_setup_merge(self, repo, by):
        """An owner's no: the factory cannot merge this repo's setup PR again until it is granted."""
        if not all(isinstance(value, str) and value.strip() for value in (repo, by)):
            raise ToolError("revoking a setup merge needs the repo and who revokes it")
        try:
            self.store.revoke_setup_merge(repo, by)
        except Conflict as exc:
            raise ToolError(str(exc)) from None
        return {"repo": repo, "authorized": False}

    def merge_setup_pr(self, repo, by, request_id):
        """The one guarded merge of the authorized setup PR; see factory_mcp.setup_merge (ADR-0074)."""
        if not all(isinstance(value, str) and value.strip() for value in (repo, by, request_id)):
            raise ToolError("merging a setup PR needs the repo, who answers for the merge and a "
                            "requestId, so an interrupted merge never merges twice")
        return setup_merge.merge(self.store, self.github, repo, by, request_id)

    def _safety_stop(self, wi):
        """The safety stop this WorkItem's escalation names (ADR-0007's risk classes and the guarded
        push), or None: a HUMAN verdict, a protected path or an access trouble needs a person,
        whatever an allowance says (#239). The phrases name what a HUMAN verdict, a refused
        protected path and the factory's real access troubles (a remote-verify 403, a denied git
        push) emit; the escalation and the attempts' text is data about this WorkItem's own facts,
        not untrusted instruction."""
        text = " ".join([wi.get("blockedOn") or "", *(wi.get("attempts") or [])]).lower()
        return next((stop for stop in SAFETY_STOPS if stop in text), None)

    def _env_paused(self, wi):
        """The confirmed environment fault the WorkItem is still paused on, or None (#239, ADR-0051):
        the last attempt that failed named the environment and no human_resolved has followed it."""
        for h in reversed(self.store.history(wi["id"])):
            if h["action"] == "human_resolved":
                return None
            if (h["action"] == "attempt_failed"
                    and diagnostics.environment_fault(h["detail"].get("cause"))):
                return h["detail"].get("cause")
        return None

    def propose_issue(self, repo, title, body):
        """Planner: open an issue labelled factory:proposed. A person adds factory:ready."""
        repo = self._enabled(repo, "factory_propose_issue")
        criteria, missing = workitem.parse_acceptance(body)
        if missing or not criteria:
            raise ToolError("The body needs an '## Acceptance' list of "
                            "'- <claim> — done when: <check>' bullets")
        issue = self.github.create_issue(repo, title, body, [workitem.PROPOSED])
        self.store.log("proposed", {"title": title}, workitem.workitem_id(repo, issue["number"]))
        return {"issue": issue["number"], "url": issue.get("html_url")}

    def start_plan(self, repo, issues, by):
        """The owner's one approval of a whole plan (ADR-0024, ADR-0082): check every issue, then
        open them in order, each labelled factory:ready and depending on the one before it, or on
        the earlier issues its `after` names. A failed check opens none."""
        if not isinstance(by, str) or not by.strip():
            raise ToolError("a plan needs the name of the person who approved it")
        if not isinstance(issues, list) or not 1 <= len(issues) <= MAX_PLAN_ISSUES:
            raise ToolError(f"a plan has 1 to {MAX_PLAN_ISSUES} issues; split a bigger goal into more plans")
        for position, item in enumerate(issues, 1):
            problem = self._plan_problem(item) or self._after_problem(item, position)
            if problem:
                raise ToolError(f"issue {position} of the plan {problem}. No issue was opened")
        repo = self._enabled(repo, by)  # after the plan's checks: a refused plan enables nothing
        opened, taken = [], []
        for position, item in enumerate(issues, 1):
            after = item.get("after")
            positions = ([position - 1] if opened else []) if after is None else after
            lines = "\n".join(f"Depends on #{opened[n - 1]['issue']}" for n in positions)
            body = f"{lines}\n\n{item['body']}" if lines else item["body"]
            try:  # the checks passed, so only GitHub or the author's trust can stop the plan here
                issue = self.github.create_issue(repo, item["title"], body, [workitem.READY])
                opened.append({"issue": issue["number"], "url": issue.get("html_url")})
                self.store.log("approved", {"by": by}, workitem.workitem_id(repo, issue["number"]))
                self.intake(repo, issue["number"])
                taken.append(issue["number"])
            except (GitHubError, ToolError) as exc:
                message = (f"{exc}. The plan stopped. Issues opened, which the factory works on: "
                           + (", ".join(f"#{number}" for number in taken) or "none"))
                if len(opened) > len(taken):  # created on GitHub, then refused at intake
                    number = opened[-1]["issue"]
                    message += (f". #{number} was opened too, but the factory will not run it: fix the "
                                f"cause above, or close #{number}")
                raise ToolError(message) from None
        return {"repo": repo, "issues": opened}

    def set_dependencies(self, workitem_id, depends_on, by, agent=False):
        """Replace the `Depends on #n` lines of a WorkItem no worker has touched (ADR-0042), so
        independent issues of a plan can run side by side. A WorkItem waiting on a dependency that
        ended is human_waiting with no pass, so its order may change too (ADR-0047). The order lives
        in the issue body (ADR-0024), so this edits the issue; the next tick reads it, as it reads
        any edit."""
        if not isinstance(by, str) or not by.strip():
            raise ToolError("changing dependencies needs the name of the person who asked")
        if (not isinstance(depends_on, list) or len(depends_on) > MAX_PLAN_ISSUES
                or any(isinstance(n, bool) or not isinstance(n, int) or n < 1 for n in depends_on)):
            raise ToolError(f"dependsOn is a list of at most {MAX_PLAN_ISSUES} issue numbers in the WorkItem's "
                            "own repo, like [12, 14]; [] lets it start at once")
        if len(set(depends_on)) != len(depends_on):
            raise ToolError("dependsOn names an issue twice")
        if not self.store.acquire(workitem_id, self.holder):
            raise ToolError(f"{workitem_id} is being ticked; try again")
        try:
            wi = self.store.get(workitem_id)
            if wi is None:
                raise ToolError(f"{workitem_id} is not a WorkItem the factory knows")
            if wi["state"] not in ("issued", "planned", "ready", "human_waiting"):
                raise ToolError(f"{workitem_id} is {wi['state']}; dependencies change only on an issued, planned, "
                                "ready or human_waiting WorkItem. Answer it with factory_resolve_human instead")
            if self.store.workorders(workitem_id):
                raise ToolError(f"{workitem_id} already had a worker pass, so its dependencies stay. Close it and "
                                "open a new issue to run it in another order")
            repo = wi["repo"]
            for number in depends_on:
                self._check_dependency(wi, number)
            issue = self._issue(repo, wi["issueNumber"])
            before = workitem.depends_on(issue.get("body"))
            body = workitem.with_dependencies(issue.get("body"), depends_on)
            if body != (issue.get("body") or ""):
                self.github.edit_issue(repo, wi["issueNumber"], body)
            self.store.log("dependencies_changed", {"by": by, "was": before, "now": depends_on,
                                                    **({"agent": True} if agent else {})}, workitem_id)
            if wi.get("waitingOn") is not None:  # the next tick sets it again if the wait goes on
                wi = self.store.put(dict(wi, waitingOn=None))
            return wi
        finally:
            self.store.release(workitem_id, self.holder)

    def _check_timeout(self, wall_ms):
        cap = self.relay.limits()["maxTimeoutMs"]
        if cap is not None and wall_ms > cap:
            raise RelayLimitError(f"requested wallMs {wall_ms} exceeds relay maxTimeoutMs {cap}; "
                                  "raise the relay cap or clear the override before retrying")

    def set_timeout(self, workitem_id, wall_ms, by, agent=False):
        """Set or clear a bounded timeout between passes, without retrying or buying passes."""
        if not isinstance(by, str) or not by.strip():
            raise ToolError("changing a timeout needs the name of the person who asked")
        if not isinstance(workitem_id, str) or not workitem_id:
            raise ToolError("id must name a known WorkItem")
        minimum = max(60000, self.config.wall_ms)
        if wall_ms is not None and minimum > 3600000:
            raise ToolError("FACTORY_WALL_MS already exceeds the largest supported override (3600000); "
                            "clear the override or lower the service default before setting one")
        if wall_ms is not None and (type(wall_ms) is not int or not minimum <= wall_ms <= 3600000):
            raise ToolError(f"wallMs must be an integer from {minimum} to 3600000, or null for the default")
        if not self.store.acquire(workitem_id, self.holder):
            raise ToolError(f"{workitem_id} is being ticked; try again")
        try:
            wi = self.store.get(workitem_id)
            if wi is None:
                raise ToolError(f"{workitem_id} is not a WorkItem the factory knows")
            if wi["state"] in workitem.TERMINAL or wi["state"] in ("merged", "merge_pending"):
                raise ToolError(f"{workitem_id} is {wi['state']}; its timeout cannot change")
            if self._worker_live(wi):
                raise ToolError(f"{workitem_id} has a worker running now; wait for it to finish")
            effective = wall_ms if wall_ms is not None else self.config.wall_ms
            if wall_ms is not None:
                try:
                    self._check_timeout(effective)
                except RelayError as exc:
                    raise ToolError(str(exc)) from None
            before = wi.get("wallMsOverride")
            wi = self.store.put(dict(wi, wallMsOverride=wall_ms))
            self.store.log("timeout_changed", {"by": by, "was": before, "now": wall_ms,
                                              "effectiveWallMs": effective,
                                              **({"agent": True} if agent else {})}, workitem_id)
            return wi
        finally:
            self.store.release(workitem_id, self.holder)

    def set_worker(self, workitem_id, workers, by, agent=False):
        """Choose which of the allowlisted workers run a WorkItem (ADR-0029): replaces its issue's
        `Worker:` lines. The choice lives in the issue body, so this edits the issue; the next tick
        reads it, as it reads any edit. Only agents in FACTORY_WORKERS may be named — a name outside
        it would be refused at dispatch, so it is refused here too, before the edit. Refused while a
        worker is live or on a terminal WorkItem; unlike dependencies, the worker may change between
        attempts, which is how an owner or Day Desk steers a retry to another model."""
        if not isinstance(by, str) or not by.strip():
            raise ToolError("choosing a worker needs the name of the person who asked")
        allowed = [agent_id for agent_id, _ in self.config.workers]
        if not isinstance(workers, list) or any(not isinstance(a, str) or not a for a in workers):
            raise ToolError(f"workers is a list of relay agent ids from FACTORY_WORKERS "
                            f"({', '.join(allowed)}), like [\"pi-deepseek-factory\"]; [] falls back "
                            "to FACTORY_WORKERS order")
        if len(workers) > len(allowed):
            raise ToolError(f"workers names {len(workers)} agents but FACTORY_WORKERS has "
                            f"{len(allowed)}")
        if len(set(workers)) != len(workers):
            raise ToolError("workers names an agent twice")
        unknown = [agent_id for agent_id in workers if agent_id not in allowed]
        if unknown:  # the relay also holds agents with no sandbox; an issue must not reach them
            raise ToolError(f"{', '.join(unknown)} is not in FACTORY_WORKERS ({', '.join(allowed)}); "
                            "add the agent to FACTORY_WORKERS, or name one of those")
        if not self.store.acquire(workitem_id, self.holder):
            raise ToolError(f"{workitem_id} is being ticked; try again")
        try:
            wi = self.store.get(workitem_id)
            if wi is None:
                raise ToolError(f"{workitem_id} is not a WorkItem the factory knows")
            if wi["state"] in workitem.TERMINAL:
                raise ToolError(f"{workitem_id} is {wi['state']}; its worker no longer matters")
            if self._worker_live(wi):
                raise ToolError(f"{workitem_id} has a worker running now; wait for it to finish "
                                "before choosing another")
            repo = wi["repo"]
            issue = self._issue(repo, wi["issueNumber"])
            before = workitem.workers_named(issue.get("body"))
            body = workitem.with_workers(issue.get("body"), workers)
            if body != (issue.get("body") or ""):
                self.github.edit_issue(repo, wi["issueNumber"], body)
            self.store.log("worker_changed", {"by": by, "was": before, "now": workers,
                                              **({"agent": True} if agent else {})}, workitem_id)
            return wi
        finally:
            self.store.release(workitem_id, self.holder)

    def register_worker(self, worker_id, model, by, agent=False, template=None):
        """Register a new sandboxed factory worker in the relay registry (ADR-0077). It copies an
        approved sandboxed template — `template`, or FACTORY_WORKER_TEMPLATE when it is not named —
        with the same command, credential mount, env, cwd, allowedRoots, image and timeout, and pins
        only `model` under that template's provider, so it cannot reach anything a factory worker
        could. An unsandboxed, unknown or cross-provider template is refused before the registry is
        touched. The registry is the relay's own agents.json, which the relay has no API to write,
        so this edits that file and reloads the relay. It never touches FACTORY_WORKERS: a
        registered worker receives no WorkOrder until the owner lists it there and its
        provider-health pilot is green. Returns the new agent and whether the running relay
        reloaded it."""
        if not isinstance(by, str) or not by.strip():
            raise ToolError("registering a worker needs the name of the person who asked")
        if not self.config.agents_file:
            raise ToolError("FACTORY_AGENTS_FILE is not set, so the factory may not write the relay "
                            "registry; register the worker by hand (RUNBOOK)")
        template_id = self.config.worker_template if template is None else template
        try:
            agent_def = registry.register(self.config.agents_file, template_id,
                                          worker_id, model)
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        reloaded, note = False, ""
        if self.relay is not None:
            try:
                self.relay.reload()
                reloaded = True
            except RelayError as exc:  # registered on disk; the next relay start picks it up
                note = (f"{worker_id} is registered on disk, but the running relay did not reload "
                        f"({exc}); it will be discovered when agent-relay next restarts")
        self.store.log("worker_registered", {"worker": worker_id, "model": model, "by": by,
                                             "reloaded": reloaded,
                                             **({"agent": True} if agent else {})})
        return {"agent": agent_def, "reloaded": reloaded,
                "note": note or (f"{worker_id} is registered but NOT in FACTORY_WORKERS; add it "
                                 f"there and run scripts/provider_health.py before any WorkOrder "
                                 f"goes to it")}

    def replace_issue(self, workitem_id, replacements, by, note, request_id, *, agent=False,
                      owner_asked=False, allowance_plan_id=None):
        """Replace a WorkItem waiting on recovery with ordered replacement issues (ADR-0059). The
        owner approves the replacement: Day Desk calls it only when the owner asked for it in chat
        (`owner_asked`, ADR-0035 / ADR-0043), with the owner's approval words as the note; the owner's
        own call carries its approval as the note and leaves ownerAsked unset. The caller passes a
        stable request ID: the journal is keyed by the WorkItem, so the same request ID with the same
        inputs replays where the run stopped, and a different request ID for the same original
        conflicts and refuses before any write, so a lost or forked caller cannot run a replacement
        again under a different key. The whole graph is planned and validated before anything is
        written (guard 8): the first
        replacement inherits the original's upstream dependencies, the rest chain onto it, and each
        direct dependent of the original moves its edge to the last replacement, keeping its other
        dependencies. A replacement that had a worker pass, a dependent that had a worker pass, or a plan
        that would make a cycle, refuses before any GitHub call. The plan itself is pure (workitem.replacement_plan) and not a tool.
        The original is journalled, re-pointed on GitHub with each rewired body verified by exact
        readback (ADR-0065), then retired: its own PR, if one is open and unmerged, is closed and
        verified by readback, and only then is the WorkItem cancelled, closed and the graph
        released (ADR-0066). A repeat of
        the same request goes by the journal and the barrier (ADR-0063): a done journal releases
        its active barrier and returns; a partly run journal replays the remaining writes; a
        member a person took over meanwhile ends the run, and the barrier releases so the
        person's route stands."""
        if owner_asked and not agent:
            raise ToolError("ownerAsked is Day Desk's flag for a decision the owner asked for in chat; the "
                            "owner's own call does not need it")
        if agent and not owner_asked and not allowance_plan_id:
            raise ToolError(f"replacing {workitem_id} is the owner's decision, because it cancels "
                            "the owner's work: get the owner's words of approval in chat, quote them as "
                            "the note, name the owner as by, and set ownerAsked true; or run it inside "
                            "an owner-approved plan allowance with allowancePlanId")
        if not agent and allowance_plan_id:
            raise ToolError("allowancePlanId is Day Desk's allowance-backed replacement (ADR-0069); "
                            "the owner's own call carries its approval as the note")
        if allowance_plan_id and any(isinstance(entry, dict) for entry in replacements or []):
            raise ToolError("an allowance replacement may only name open issues the repo already "
                            "has; a planned title-and-body item changes the material scope, so the "
                            "owner approves that with ownerAsked instead")
        if not isinstance(by, str) or not by.strip():
            raise ToolError("replacing an issue needs the name of the person who asked")
        if not isinstance(note, str) or not note.strip():
            raise ToolError("replacing an issue needs the owner's approval note")
        if not isinstance(request_id, str) or not request_id.strip():
            raise ToolError("replacing an issue needs a stable requestId: the same call repeats under "
                            "it, so a repeat shares the replacement's journal and a different one for "
                            "the same WorkItem refuses as a conflict")
        if (not isinstance(replacements, list) or not replacements or len(replacements) > MAX_PLAN_ISSUES
                or any(isinstance(n, bool) or not isinstance(n, (int, dict))
                       or (isinstance(n, int) and n < 1)
                       or (isinstance(n, dict) and not isinstance(n.get("title"), str)
                           and not isinstance(n.get("body"), str)) for n in replacements)):
            raise ToolError(f"replacements is a non-empty list of at most {MAX_PLAN_ISSUES} issue numbers "
                            "in the WorkItem's own repo, like [12, 14]; an issue the factory opens itself "
                            "is a title-and-body item instead")
        seen, twice = [], False  # a planned item is unhashable, so name the entries one by one
        for entry in replacements:
            key = entry if isinstance(entry, int) else f"{entry.get('title')}\n{entry.get('body')}"
            if key in seen:
                twice = True
            seen.append(key)
        if twice:
            raise ToolError("replacements names an issue twice")
        if not self.store.acquire(workitem_id, self.holder):
            raise ToolError(f"{workitem_id} is being ticked; try again")
        try:
            wi = self.store.get(workitem_id)
            if wi is None:
                raise ToolError(f"{workitem_id} is not a WorkItem the factory knows")
            journal_id = f"replace/{workitem_id}"
            record = self.store.request(journal_id)
            active = {row["requestId"] for row in self.store.active_pauses()}
            if record and record["phase"] == REQUEST_PHASES[-1] and journal_id in active:
                # the run completed; only its release was killed. The journal says done, so the
                # barrier gives way even though the WorkItem it names may now be refused (#235)
                self.store.release_pause(journal_id)
            # A kill between the journal's advance to pushing and the release leaves the writes
            # partly done and the barrier held. A resume replays the remaining writes; a member a
            # person moved in the meantime is a takeover: the run ends without further writes, so
            # the barrier holds nothing the person's route now owns (ADR-0063).
            resuming = bool(record) and record["phase"] in REQUEST_PHASES[:2] and journal_id in active
            takeover = None
            if resuming:
                ours = (wi["state"] == "cancelled"
                        and (wi.get("terminalReason") or "").startswith("replaced by"))
                if wi["state"] not in ("human_waiting", "blocked") and not ours:
                    takeover = record["phase"]  # a person moved the original while the run was down
            else:
                if wi["state"] not in ("human_waiting", "blocked"):
                    raise ToolError(f"{workitem_id} is {wi['state']}; only a WorkItem waiting on recovery "
                                    "is replaced. Answer it with factory_resolve_human instead")
            if self.store.workorders(workitem_id) and self._worker_live(wi):
                raise ToolError(f"{workitem_id} had a worker pass whose work is still live "
                                "(an active WorkOrder or relay task), so it cannot be replaced. Wait "
                                "for it to fail or answer it, then replace it")
            repo, mine = wi["repo"], wi["issueNumber"]
            if mine in replacements:
                raise ToolError(f"#{mine} cannot be replaced by itself")
            # The graph comes from the factory's WorkItems in this repo; bodies are read, never
            # written, until every check below has passed.
            graph = {other["issueNumber"]: workitem.depends_on(self.github.issue(repo, other["issueNumber"]).get("body"))
                     for other in self.store.all() if other["repo"] == repo}
            ended = None  # a member a person closed while the run was down (#235)
            for position, entry in enumerate(replacements, 1):
                if not isinstance(entry, (int, dict)) or isinstance(entry, bool):
                    raise ToolError("each replacement is an issue number, or a title-and-body plan "
                                    "item the factory opens itself, like start_plan's")
                if isinstance(entry, dict):
                    problem = self._plan_problem(entry)
                    if problem:
                        raise ToolError(f"the planned replacement at position {position} {problem}. "
                                        "No issue was opened")
                    if workitem.depends_on(entry["body"]):
                        raise ToolError(f"the planned replacement at position {position} carries its "
                                        "own 'Depends on' lines; the factory gives it the chain: the "
                                        "original's upstream, then each earlier replacement")
                    continue
                number = entry
                issue = self._issue(repo, number)
                if "pull_request" in issue:
                    raise ToolError(f"#{number} is a pull request, not an issue")
                deps = workitem.depends_on(issue.get("body"))
                if mine in deps:
                    raise ToolError(f"#{number} depends on #{mine}; name a fresh issue that waits for "
                                    "nothing, not one that waits for the issue it replaces")
                replacement_id = workitem.workitem_id(repo, number)
                status, how = self._dependency(repo, number)
                if resuming and (status == "ended"
                                 or (self.store.get(replacement_id) and self.store.workorders(replacement_id))):
                    ended = number
                elif status == "ended":
                    raise ToolError(f"#{number} {how}, so nothing could wait for it")
                elif self.store.get(replacement_id) and self.store.workorders(replacement_id):
                    raise ToolError(f"#{number} already had a worker pass, so it cannot be a replacement; "
                                    "close it or open a fresh issue instead")
                if not resuming or not ended:
                    graph.setdefault(number, deps)
            for number, deps in graph.items():
                if mine in deps:
                    other = self.store.get(workitem.workitem_id(repo, number))
                    if other and self.store.workorders(other["id"]):
                        raise ToolError(f"#{number} already had a worker pass, so its dependency on "
                                        f"#{mine} stays; close #{mine} instead")
            # the cycle read is pre-write even for the planned items: each is a symbol until the
            # run opens it, at a position the plan can name without ever colliding with a number
            symbols = [entry if isinstance(entry, int) else -(10 ** 6 + position)
                       for position, entry in enumerate(replacements, 1)]
            plan = workitem.replacement_plan(graph, mine, symbols)
            cycle = workitem.cycle_in({**graph, **plan})
            if cycle:
                named = " depends on ".join(f"#{number}" for number in cycle)
                raise ToolError(f"replacing #{mine} would make a cycle: {named}")
            if allowance_plan_id and record is None:
                # the first run spends one use; a journalled replay resumes it without a new spend
                # (ADR-0069), so an interrupted replacement is never charged twice
                try:
                    spent = self.store.spend_allowance(allowance_plan_id, "replace",
                                                       workitem_id=workitem_id)
                except Conflict as exc:
                    raise ToolError(str(exc)) from None
            try:  # a different request ID for the same original conflicts with its journal (#203)
                record = self.store.open_request(journal_id, "replace",
                                                 {"workItemId": workitem_id, "replacements": replacements,
                                                  "requestId": request_id})
            except Conflict as exc:
                raise ToolError(f"requestId {request_id!r} conflicts with the replacement journal for "
                                f"{workitem_id}: {exc}") from None
            if resuming and (takeover is not None or ended is not None):
                # a person's takeover closes the run: take the journal to done and release the
                # barrier it holds, so the person's route is the one that runs (ADR-0063)
                if record["phase"] == REQUEST_PHASES[0]:
                    self.store.advance_request(journal_id, REQUEST_PHASES[1])
                self.store.advance_request(journal_id, REQUEST_PHASES[-1])
                self.store.log("replace_abandoned", {"replacements": replacements}, workitem_id)
                self.store.release_pause(journal_id)
                return self.store.get(workitem_id)
            # A planned replacement becomes a factory:proposed issue now, or the issue a lost
            # create response already opened is adopted by its marker. Creating touches only the
            # new issue, and a proposal is never factory:ready, so nothing can dispatch while the
            # barrier being recorded next is still absent.
            numbers = self._resolve_replacements(repo, journal_id, mine, graph, replacements)
            plan = workitem.replacement_plan(graph, mine, numbers)
            barrier = {"original": [workitem_id],
                       "replacement": [workitem.workitem_id(repo, n) for n in numbers],
                       "dependent": [workitem.workitem_id(repo, n) for n in plan
                                     if n not in numbers]}
            active = [row["requestId"] for row in self.store.active_pauses()]
            if journal_id in active:
                # the run is resumed: it keeps the barrier its first attempt recorded, whose graph
                # was read before any write and cannot be derived again after the edits
                if record["phase"] == REQUEST_PHASES[-1]:  # done, but a kill before the release
                    self.store.release_pause(journal_id)
            elif record["reused"] and record["phase"] == REQUEST_PHASES[-1]:
                # the journal says this run already ended (a takeover, or nothing left to run):
                # return what it found without holding the graph again, or no retry could ever
                # free a barrier recorded over a done journal (ADR-0063, #235)
                return self.store.get(workitem_id)
            else:
                # recorded before any GitHub write, so no tick takes the ready-labelled graph
                # members during the operation, on this process or after a restart (#233, #235)
                self.store.pause(journal_id, "replace", barrier)
            if record["reused"] and record["phase"] == "done":
                return self.store.get(workitem_id)  # the replacement already ran; do not run it again
            if record["phase"] == REQUEST_PHASES[0]:
                self.store.advance_request(journal_id, "pushing")
            # Every plan edge is written and then verified by exact readback (ADR-0065): a lost
            # edit response or drifted body cannot be trusted, so it raises while the barrier and
            # the pushing journal hold, and the same call replays. The write sets the whole
            # Depends-on list each time, so a replayed edge is never doubled and an unrelated
            # edge is never lost.
            for number in sorted(plan):
                body = self.github.issue(repo, number).get("body")
                updated = workitem.with_dependencies(body, plan[number])
                if updated != body:
                    self.github.edit_issue(repo, number, updated)
                if self.github.issue(repo, number).get("body") != updated:
                    raise ToolError(
                        f"#{number}'s body on GitHub does not read back as the dependency lines "
                        "the rewiring wrote (a lost response or a drift), so the recovery stays "
                        "paused: clear the problem, then run the replacement again")
            # Cleanup precedes activation (ADR-0066): close the original's unmerged PR and retire
            # the original issue first, both verified by readback, before the store write, the
            # done journal and the barrier release can let a replacement run. A merged or
            # unverifiable original PR stops the run here with the barrier held, so nothing past
            # the replacement can dispatch while a person resolves it.
            self._retire_original(repo, wi)
            self.github.close_issue(repo, mine)
            if self.github.issue(repo, mine).get("state") != "closed":
                raise ToolError(f"#{mine}'s close on GitHub does not read back as closed (a lost "
                                "response or a reopened issue), so the recovery stays paused: clear "
                                "the problem, then run the replacement again")
            names = ", ".join(f"#{number}" for number in replacements)
            # what the cancelled WorkItem tried stays on it: the replacement is the recovery
            # record's continuation, not a wipe of its history
            new = self._write(wi, dict(wi, state="cancelled", terminalReason=f"replaced by {names}",
                                       blockedOn=None, waitingOn=None), "replaced",
                              replacements=replacements, note=note, requestId=request_id,
                              **({"agent": True} if agent else {}),
                              **({"ownerAsked": True} if owner_asked else {}),
                              **({"allowance": allowance_plan_id} if allowance_plan_id else {}))
            self.store.advance_request(journal_id, "done")
            self.store.release_pause(journal_id)  # the operation completed: the graph runs again
            return new
        finally:
            self.store.release(workitem_id, self.holder)

    def _resolve_replacements(self, repo, journal_id, mine, graph, replacements):
        """Every replacement as an issue number. A title-and-body item is a replacement the factory
        opens itself: a factory:proposed issue, never factory:ready. The first one waits where the
        original waited, each later one on the replacement before it, the same chain the plan
        writes (ADR-0059)."""
        numbers = []
        for position, entry in enumerate(replacements, 1):
            if not isinstance(entry, dict):
                numbers.append(entry)
                continue
            deps = list(graph.get(mine) or ()) if len(numbers) == 0 else [numbers[-1]]
            numbers.append(self._open_replacement(repo, journal_id, position, entry, deps))
        return numbers

    def _open_replacement(self, repo, journal_id, position, item, deps):
        """Open one planned replacement at `position` as a factory:proposed issue, or adopt the one
        already there. The issue body carries a durable identity marker naming this request and
        position. Before it creates, the call looks for the marker among the repo's
        factory:proposed issues and verifies the marker, the title and the body the request named
        before it adopts that exact issue; so an ambiguous create response, or a restart of the
        same journalled call, picks up the issue GitHub already holds instead of opening a
        duplicate. An issue the marker names whose words were changed after the fact also refuses,
        so a person's edit can never be silently overwritten."""
        marker = f"<!-- factory replace {journal_id} {position} -->"
        body = workitem.with_dependencies(item["body"], deps).rstrip() + "\n\n" + marker
        matches = [issue for issue in self.github.labelled_issues(repo, workitem.PROPOSED)
                   if any(found == (journal_id, str(position))
                          for found in REPLACEMENT_MARKER.findall(issue.get("body") or ""))]
        if len(matches) > 1:
            raise ToolError(f"the marker this request writes appears on "
                            f"{', '.join('#' + str(issue['number']) for issue in matches)}; close the "
                            "extra ones, then try the replacement again")
        if matches:
            match = matches[0]
            if match["title"] != item["title"] or match.get("body") != body:
                raise ToolError(f"#{match['number']} carries this request's marker but its title or body "
                                "is no longer what the request named; close it or put the words back, "
                                "then try the replacement again")
            return match["number"]
        issue = self.github.create_issue(repo, item["title"], body, [workitem.PROPOSED])
        self.store.log("proposed", {"title": item["title"]},
                       workitem.workitem_id(repo, issue["number"]))
        return issue["number"]

    def _retire_original(self, repo, wi):
        """Close the original's own PR, when the WorkItem opened one (ADR-0066). The merge state is
        read from GitHub before any write: a merged PR is the original already delivered, so the
        replacement must never run against the work and the run stops with the barrier held for a
        person to resolve (resolve or close the replacements, then the retry's takeover route
        releases). A PR the store names that GitHub does not hold as this repo's own branch's PR is
        a state no cleanup could act on, and stops the same way. Closing repeats stand idempotently
        against a replay: one that read back closed is left closed, as no new write."""
        number = wi.get("prNumber")
        if number is None:
            return
        try:
            pr = self.github.pull(repo, number)
        except GitHubError as exc:
            if exc.status != 404:
                raise
            pr = None
        head = (pr or {}).get("head") or {}
        if not pr or head.get("ref") != wi.get("branch") or (head.get("repo") or {}).get("full_name") != repo:
            raise ToolError(f"PR #{number}, the one the store names for #{wi['issueNumber']}, is not "
                            f"{wi.get('branch')} in {repo} on GitHub, so the original's state is "
                            "unverifiable: put the number back or fix the branch, then try again")
        if pr.get("merged_at") or pr.get("merged"):
            raise ToolError(f"PR #{number} is already merged, so #{wi['issueNumber']} delivered its "
                            "work and the replacement must not run: close the replacement issues, "
                            "then resolve this WorkItem instead")
        if pr["state"] != "closed":
            self.github.close_issue(repo, number)
        # the readback repeats the merge check: the gate or a person can merge in the window
        # between the read above and the readback, and a merged PR does read back closed
        readback = self.github.pull(repo, number)
        if readback.get("merged_at") or readback.get("merged"):
            raise ToolError(f"PR #{number} merged after the run read it as unmerged, so "
                            f"#{wi['issueNumber']} delivered its work and the replacement must not "
                            "run: close the replacement issues, then resolve this WorkItem instead")
        if readback.get("state") != "closed":
            raise ToolError(f"#{number}'s close on GitHub does not read back as closed (a lost "
                            "response or a reopened PR), so the recovery stays paused: clear the "
                            "problem, then run the replacement again")

    @staticmethod
    def _worker_live(wi):
        """A worker pass may still be running on the WorkItem: an active WorkOrder, a live relay
        task, or a state whose next step dispatches or polls one. The per-WorkItem lease in
        replace_issue already excludes a concurrent tick. Past attempts that failed or were
        exhausted and wrote no live marker are history, not a live worker."""
        return bool(wi.get("activeWorkOrderId") or wi.get("providerTaskId")) or wi["state"] in WORKING

    def _check_dependency(self, wi, number):
        """Refuse a dependency that is this issue, not an issue, ended, or leads back to this issue."""
        repo, mine = wi["repo"], wi["issueNumber"]
        if number == mine:
            raise ToolError(f"#{number} is this issue: it cannot depend on itself")
        if "pull_request" in self._issue(repo, number):
            raise ToolError(f"#{number} is a pull request, not an issue")
        status, how = self._dependency(repo, number)
        if status == "ended":
            raise ToolError(f"#{number} {how}, so nothing could wait for it")
        pending, seen = [number], set()
        while pending:  # a path of Depends-on lines from #number back to this issue is a cycle
            current = pending.pop()
            if current in seen:
                continue
            seen.add(current)
            try:
                further = workitem.depends_on(self.github.issue(repo, current).get("body"))
            except GitHubError as exc:
                if exc.status != 404:
                    raise
                further = []
            if mine in further:
                raise ToolError(f"#{current} depends on #{mine}, so #{mine} cannot depend on #{number}: "
                                "that would make a cycle")
            pending.extend(further)

    def _plan_problem(self, item):
        """Why one issue of a plan may not be opened, or None."""
        if not isinstance(item, dict) or not all(isinstance(item.get(key), str) and item[key].strip()
                                                 for key in ("title", "body")):
            return "needs a title and a body"
        criteria, missing = workitem.parse_acceptance(item["body"])
        if missing or not criteria:
            return ("needs an '## Acceptance' list of '- <claim> — done when: <check>' bullets. "
                    "Without a check: " + ("; ".join(missing) or "no bullets found"))
        text = f"{item['title']}\n{item['body']}".lower()  # code cannot tell a request from a mention
        # a whole path word only: raw.githubusercontent.com or AGENTS.mdx names no protected path
        named = [path for path in self.config.protected_paths
                 if re.search(rf"(?<![\w.-]){re.escape(path.rstrip('/').lower())}(?![\w-])", text)]
        if named:
            return f"names {', '.join(named)}, which only a person changes; leave it out of the plan"
        if not workitem.paths_listed(item["body"]):
            return ("needs a 'Paths: <glob>, <glob>' line naming the source files this issue may "
                    "change; every pass is fenced to them (ADR-0087)")
        return None

    @staticmethod
    def _after_problem(item, position):
        """Why one issue of a plan may not carry its `after` positions (ADR-0082), or None: each is a
        whole number earlier than its own item, and none repeats. The positions map to the issues
        this call opens, so a position that is not earlier could never resolve."""
        after = item.get("after")
        if after is None:
            return None
        if (not isinstance(after, list)
                or any(isinstance(n, bool) or not isinstance(n, int) or not 1 <= n < position for n in after)
                or len(set(after)) != len(after)):
            return ("needs after to list only the earlier positions it waits for, each a whole number "
                    "before this issue and not repeated")
        return None

    def approve_lesson(self, repo, fingerprint, text, by):
        """A person's go on one lesson (ADR-0020): the code found the candidate, the owner approved
        the wording. Refused when the fingerprint is not a candidate, or is already a lesson in the repo."""
        repo = self._allowlisted(repo)
        if not self.config.lessons_file:
            raise ToolError("FACTORY_LESSONS_FILE is not set, so the factory keeps no lessons")
        if not isinstance(by, str) or not by.strip():
            raise ToolError("approving a lesson needs the name of the person who approved it")
        try:
            entry = lessons.approve(self.config.lessons_file, repo, fingerprint, text, by,
                                    lambda r: lessons.candidates(self.store, r))
        except (ValueError, OSError) as exc:  # OSError: the file cannot be read or written
            raise ToolError(str(exc)) from None
        self.store.log("lesson_approved", {"repo": entry["repo"], "fingerprint": fingerprint,
                                           "by": by})
        return entry

    def retire_lesson(self, repo, fingerprint, by):
        """A person's no on one lesson: workers stop seeing it. Refused when it is not a lesson."""
        repo = self._allowlisted(repo)
        if not self.config.lessons_file:
            raise ToolError("FACTORY_LESSONS_FILE is not set, so the factory keeps no lessons")
        if not isinstance(by, str) or not by.strip():
            raise ToolError("retiring a lesson needs the name of the person who retired it")
        try:
            lessons.retire(self.config.lessons_file, repo, fingerprint)
        except (ValueError, OSError) as exc:  # OSError: the file cannot be read or written
            raise ToolError(str(exc)) from None
        self.store.log("lesson_retired", {"repo": repo, "fingerprint": fingerprint, "by": by})
        return {"repo": repo, "fingerprint": fingerprint, "retired": True}
