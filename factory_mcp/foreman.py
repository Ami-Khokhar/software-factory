"""The Foreman: under a per-WorkItem lease, reconcile with GitHub, then act at most once."""
import calendar
import dataclasses
import os
import re
import time
import uuid

from . import documents, gate_secrets, lessons, patches, repos, routing, workitem
from .github import GitHubError
from .mcp import ToolError
from .relay import RelayError
from .store import Conflict, now
from .verdict import blocking

# States that need a person. Entering one posts a comment on the issue, so the owner hears of it.
ATTENTION = ("human_waiting", "blocked", "exhausted")
WORKING = ("dispatching", "implementing")
BOOTSTRAP_PREFIX = "factory-bootstrap/"
MAX_PLAN_ISSUES = 10  # one approval covers this many issues at most (ADR-0024)
REGATE_MARKER = re.compile(r"<!-- factory regate (\d+) -->")


def agent_resolved(history):
    """Whether Day Desk, driving, already resolved this WorkItem once (ADR-0030)."""
    return any(h["action"] == "human_resolved" and h["detail"].get("agent") for h in history)


class RepoNotReady(ToolError):
    """The repo failed the repo check: its author can fix that, so discovery tells them (ADR-0027)."""


class Foreman:
    def __init__(self, store, github, config, relay=None):
        self.store = store
        self.github = github
        self.config = config
        self.relay = relay
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
        return "\n".join([
            "## Task", "", f"Owner-approved bootstrap for https://github.com/{repo}.", "",
            "## Intent", "", "Install the missing controls required for this repository to join the factory.", "",
            "## Why it was needed", "", "The factory cannot run reviewed PRs without its gate and written standard.", "",
            "## Why this approach", "", "The factory prepared only the missing files from trusted templates.",
            "The owner must review and merge this setup PR; normal workers still cannot change these paths.", "",
            "## What changed", "", *(f"- `{path}`: install the missing factory control" for path in paths), "",
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
            verdict = workitem.current_verdict(wi, facts)  # accepted now: keep its blocking findings
            if verdict:  # by severity: blocking() falls back to every finding, and a nit is no lesson
                detail["blocking"] = [{"fingerprint": f["fingerprint"], "problem": f["problem"]}
                                      for f in verdict["findings"] if f["severity"] == "blocking"]
            wi = self._write(wi, new, "reconcile", **detail)
            wi = self.act(wi, facts)
        except Conflict:
            wi = self.store.get(workitem_id)
        except (GitHubError, RelayError) as exc:
            self.store.log("tick_error", {"error": str(exc)}, workitem_id)
            wi = self.store.get(workitem_id)
        finally:
            self.store.release(workitem_id, self.holder)
        return before, wi

    def _write(self, old, new, action, **detail):
        if new == old:
            return old
        if new["state"] != "ready" and new.get("waitingOn") is not None:  # however it leaves ready,
            new = dict(new, waitingOn=None)  # the wait ends: say so, or status names a stale issue
        if new["state"] in ("human_waiting", "blocked") and not new.get("owner"):
            new = dict(new, owner=self.config.owner or new["repo"].split("/")[0])
        stored = self.store.put(new)
        detail = dict(detail, **{"from": old["state"], "to": stored["state"]})
        escalated = stored["state"] in ATTENTION and old["state"] != stored["state"]
        if escalated:  # the weekly report groups these (metrics.escalation_reasons)
            detail["escalation"] = self._reason(stored)
        self.store.log(action, detail, old["id"])
        if escalated:
            self._notify(stored)
        return stored

    @staticmethod
    def _reason(wi):
        """Why a WorkItem needs a person, on one line."""
        return " ".join(str(wi.get("blockedOn") or wi.get("terminalReason") or wi["state"]).split())

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
            # a retry says why the last attempt failed, or what a person said
            return self.dispatch(wi, "implement", self._human_findings(wi) + self._attempt_findings(wi),
                                 facts["issue"]["workers"])
        # GitHub runs no workflow on a PR that conflicts with its base, so the gate would never answer
        conflict = (wi["state"] in ("pr_open", "reviewing", "changes_requested") and facts["pr"] is not None
                    and facts["pr"]["mergeable"] is False)
        if wi["state"] == "changes_requested" or conflict:  # findings travel as data (ADR-0007)
            latest = facts["verdicts"][-1] if facts["verdicts"] else None
            # a redo that failed leaves the PR, and the verdict on its head, as they were
            applies = latest and (wi["state"] == "changes_requested"
                                  or conflict and latest["headSha"] == facts["pr"]["headSha"])
            findings = [{key: f[key] for key in ("fingerprint", "path", "line", "problem")}
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
        One that ended without a merge goes to a person: only the owner changes the plan's order."""
        for number in numbers:
            status, how = self._dependency(wi["repo"], number)
            if status == "open":
                return self._write(wi, dict(wi, waitingOn=number), "waiting", on=number)
            if status == "ended":
                return self._write(wi, dict(wi, state="human_waiting", waitingOn=None, blockedOn=(
                    f"this issue depends on #{number}, which {how}. Edit or remove its "
                    f"'Depends on #{number}' line, then retry; or close it")), "dependency_ended", on=number)
        return self._write(wi, dict(wi, waitingOn=None), "dependency_merged") if wi.get("waitingOn") else wi

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

    @staticmethod
    def _attempt_findings(wi):
        attempts = wi.get("attempts") or []
        return [{"fingerprint": "previous-attempt", "problem": attempts[-1]}] if attempts else []

    def _order(self, wi):
        return next(o for o in self.store.workorders(wi["id"]) if o["id"] == wi["activeWorkOrderId"])

    def _lessons(self, wi):
        """The repo's approved lessons for the WorkOrder, newest first, or [] when there are none.
        documents.workorder caps how many ride along."""
        if not self.config.lessons_file:
            return []
        try:
            return lessons.for_repo(lessons.load(self.config.lessons_file), wi["repo"])
        except (ValueError, OSError) as exc:  # OSError: a file that exists but cannot be read
            self.store.log("lessons_invalid", {"error": str(exc)}, wi["id"])
            return []  # a hand-edited or unreadable file must not stop the line; the audit log says why

    def _default_branch(self, repo):
        return self.github.repository(repo)["default_branch"]

    def dispatch(self, wi, kind, findings=(), named=()):
        # Workers of one repo share its checkout, so one pass runs per repo; the rest wait their turn.
        if any(other["repo"] == wi["repo"] and other["id"] != wi["id"] and other["state"] in WORKING
               for other in self.store.all()):
            return wi
        orders = self.store.workorders(wi["id"])
        used = len(orders) - (wi.get("passesBefore") or 0)  # a person's retry buys a fresh budget
        if used >= self.config.max_passes:
            return self._write(wi, dict(wi, state="exhausted", terminalReason=(
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
        worker = routing.route(self.config.workers, self.config.local_only(wi["repo"]), failed, named)
        if worker is None:
            return self._write(wi, dict(wi, state="human_waiting", blockedOn=(
                f"{wi['repo']} is local-only and FACTORY_WORKERS has no local worker")), "no_worker")
        attempt = sum(1 for o in orders if o["round"] == wi.get("gateRound", 0) and o["kind"] == kind)
        base = self.github.branch_head(wi["repo"], self._default_branch(wi["repo"]))
        order = documents.workorder(wi, kind, attempt, self.config, base, findings,
                                    self._lessons(wi))
        self.store.save_workorder(order)
        wi = self._write(wi, dict(wi, state="dispatching", activeWorkOrderId=order["id"], worker=worker),
                         "dispatch", workOrder=order["id"], worker=worker)
        return self.submit(wi, order)

    def submit(self, wi, order):
        task = self.relay.submit(wi["worker"], documents.worker_input(order, wi["branch"]),
                                 order["idempotencyKey"], order["budget"]["wallMs"],
                                 cwd=self.config.checkout(wi["repo"]))
        return self._write(wi, dict(wi, state="implementing", providerTaskId=task["id"]),
                           "submitted", task=task["id"])

    def poll(self, wi):
        order = self._order(wi)
        task = self.relay.get(wi["providerTaskId"])
        if task is None:
            return self.fail(wi, order, "the relay no longer knows the task (restarted?)")
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
        sha, files = None, []
        if not errors:
            try:
                sha = self.push_patch(wi, order, result["patch"])
            except patches.PatchError as exc:
                errors = [f"patch not pushed: {exc}"]
        if not errors:
            errors, files = self.check_pushed(wi, order, sha)
        if errors:
            return self.fail(wi, order, f"{task['agentId']}: " + "; ".join(errors))
        self.store.log("result", {"workOrder": order["id"], "commitSha": sha}, wi["id"])
        done = dict(wi, headSha=sha, attempts=[], activeWorkOrderId=None, providerTaskId=None)
        if wi.get("prNumber"):
            wi = self._write(wi, dict(done, state="reviewing"), "pushed")
            try:  # the gate lints the body against the PR's paths, which this push may have changed
                self.github.edit_pull(wi["repo"], wi["prNumber"],
                                      documents.pr_body(wi, order, files, result["evidence"], task["agentId"]))
            except GitHubError as exc:  # the push stands; the gate's lint names what the body lacks
                self.store.log("pr_body_failed", {"error": str(exc)}, wi["id"])
            return wi
        pr = self.open_pr(wi, order, files, result["evidence"], task["agentId"])
        return self._write(wi, dict(done, state="pr_open", prNumber=pr["number"]), "pr_opened",
                           pr=pr["number"])

    def push_patch(self, wi, order, patch):
        """Apply the Result's patch on the WorkOrder's base and push it to the WorkItem's branch
        (ADR-0017). Returns the pushed commit."""
        base = patch.get("baseSha") or (order["headSha"] if order["kind"] == "fix" else order["baseSha"])
        return patches.apply(self.config.git_url.format(repo=wi["repo"]), self.config.github_token,
                             wi["branch"], base, patch, f"{wi['goal']} (#{wi['issueNumber']})\n\n"
                             f"WorkOrder {order['id']}", force=order["kind"] != "fix",
                             protected=order["constraints"]["protectedPaths"])

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
        lines = sum(f["additions"] + f["deletions"] for f in files)
        if lines > limits["maxChangedLines"] or len(files) > limits["maxChangedFiles"]:
            errors.append(f"{lines} lines in {len(files)} files exceeds the WorkOrder constraints")
        return errors, files

    def open_pr(self, wi, order, files, evidence, worker):
        """Adopt the open PR on the branch if there is one (ADR-0003), else open it."""
        for pr in self.github.pulls_for_branch(wi["repo"], wi["branch"]):
            if pr["state"] == "open":
                return pr
        return self.github.create_pull(wi["repo"], wi["branch"], self._default_branch(wi["repo"]),
                                       f"{wi['goal']} (#{wi['issueNumber']})",
                                       documents.pr_body(wi, order, files, evidence, worker))

    def fail(self, wi, order, reason):
        """Record the attempt; two failed attempts block the WorkItem with the evidence."""
        attempts = list(wi.get("attempts") or []) + [reason]
        new = dict(wi, attempts=attempts, activeWorkOrderId=None, providerTaskId=None,
                   state="changes_requested" if order["kind"] == "fix" else "ready")
        if len(attempts) >= 2:
            new.update(state="blocked", blockedOn=(
                f"{len(attempts)} worker attempts at the {order['kind']} WorkOrder failed. Fix the "
                "cause named in attempts, then call factory_resolve_human."))
        return self._write(wi, new, "attempt_failed", reason=reason, worker=wi.get("worker"))

    def resolve_human(self, workitem_id, resolution, note, by, agent=False):
        """A person's answer to human_waiting, blocked or exhausted (ADR-0006): retry, regate or close.
        Day Desk, driving (`agent`), answers each WorkItem once; after that the owner decides (ADR-0030).
        A person's retry starts a fresh budget of passes and Day Desk's keeps the budget it has (ADR-0035),
        so Day Desk may not retry a WorkItem that used its budget, and only the owner answers `exhausted`."""
        if not self.store.acquire(workitem_id, self.holder):
            raise ToolError(f"{workitem_id} is being ticked; try again")
        try:
            wi = self.store.get(workitem_id)
            if wi is None or wi["state"] not in ("human_waiting", "blocked", "exhausted"):
                raise ToolError(f"{workitem_id} is not waiting on a person")
            passes = len(self.store.workorders(workitem_id))
            spent = passes - (wi.get("passesBefore") or 0) >= self.config.max_passes
            if agent and (wi["state"] == "exhausted" or resolution == "retry" and spent):
                raise ToolError(f"{workitem_id} used its budget of passes, so the owner decides whether to spend "
                                f"more: factory retry|close {workitem_id} <note>")
            if agent and agent_resolved(self.store.history(workitem_id)):
                raise ToolError(f"Day Desk already resolved {workitem_id} once, so the owner decides now: "
                                f"factory retry|regate|close {workitem_id} <note>")
            rerun = {}
            if resolution == "retry":
                new = dict(wi, state="changes_requested" if wi.get("prNumber") else "ready", terminalReason=None,
                           passesBefore=wi.get("passesBefore") if agent else passes)
            elif resolution == "regate" and wi.get("prNumber"):
                regated_at = now()  # before the edit, so the run the edit starts begins after it
                rerun = {"gateRerun": self._rerun_gate(wi)}
                new = dict(wi, state="reviewing", **({"regatedAt": regated_at} if rerun["gateRerun"] else {}))
            elif resolution == "close":
                new = dict(wi, state="cancelled", terminalReason=f"closed by {by}: {note}")
            else:
                raise ToolError("resolution must be retry, regate (needs a PR) or close")
            return self._write(wi, dict(new, blockedOn=None, attempts=[]), "human_resolved",
                               resolution=resolution, note=note, by=by, **rerun, **({"agent": True} if agent else {}))
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
        """The owner's one approval of a whole plan (ADR-0024): check every issue, then open them in
        order, each labelled factory:ready and depending on the one before it. A failed check opens none."""
        if not isinstance(by, str) or not by.strip():
            raise ToolError("a plan needs the name of the person who approved it")
        if not isinstance(issues, list) or not 1 <= len(issues) <= MAX_PLAN_ISSUES:
            raise ToolError(f"a plan has 1 to {MAX_PLAN_ISSUES} issues; split a bigger goal into more plans")
        for position, item in enumerate(issues, 1):
            problem = self._plan_problem(item)
            if problem:
                raise ToolError(f"issue {position} of the plan {problem}. No issue was opened")
        repo = self._enabled(repo, by)  # after the plan's checks: a refused plan enables nothing
        opened, taken = [], []
        for item in issues:
            body = f"Depends on #{opened[-1]['issue']}\n\n{item['body']}" if opened else item["body"]
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
