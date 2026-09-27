"""The Foreman: under a per-WorkItem lease, reconcile with GitHub, then act at most once."""
import calendar
import dataclasses
import os
import time
import uuid

from . import documents, patches, repos, routing, workitem
from .github import GitHubError
from .mcp import ToolError
from .relay import RelayError
from .store import Conflict, now
from .verdict import blocking

# States that need a person. Entering one posts a comment on the issue, so the owner hears of it.
ATTENTION = ("human_waiting", "blocked", "exhausted")
WORKING = ("dispatching", "implementing")


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
        repo = self._allowlisted(repo)
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
            raise ToolError(f"{repo} is not ready: " + " ".join(f"{c['detail']}. {c['fix']}" for c in failed))
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

    def rebuild(self):
        """Intake every open `factory:ready` issue the store does not know (D8: GitHub is the truth)."""
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
            wi = self._write(wi, new, "reconcile", headSha=new.get("headSha"))
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
        if new["state"] in ("human_waiting", "blocked") and not new.get("owner"):
            new = dict(new, owner=self.config.owner or new["repo"].split("/")[0])
        stored = self.store.put(new)
        self.store.log(action, dict(detail, **{"from": old["state"], "to": stored["state"]}), old["id"])
        if stored["state"] in ATTENTION and old["state"] != stored["state"]:
            self._notify(stored)
        return stored

    def _notify(self, wi):
        """Tell the owner on the issue. A failed comment is logged; it never blocks the transition."""
        reason = " ".join(str(wi.get("blockedOn") or wi.get("terminalReason") or wi["state"]).split())
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
        if wi["state"] == "ready":  # a retry says why the last attempt failed, or what a person said
            return self.dispatch(wi, "implement", self._human_findings(wi) + self._attempt_findings(wi))
        if wi["state"] == "changes_requested":  # findings travel as data (D6)
            findings = [{key: f[key] for key in ("fingerprint", "path", "line", "problem")}
                        for f in (blocking(facts["verdicts"][-1]) if facts["verdicts"] else [])]
            return self.dispatch(wi, "fix", findings + self._human_findings(wi) + self._attempt_findings(wi))
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

    def _default_branch(self, repo):
        return self.github.repository(repo)["default_branch"]

    def dispatch(self, wi, kind, findings=()):
        # Workers of one repo share its checkout, so one pass runs per repo; the rest wait their turn.
        if any(other["repo"] == wi["repo"] and other["id"] != wi["id"] and other["state"] in WORKING
               for other in self.store.all()):
            return wi
        orders = self.store.workorders(wi["id"])
        if len(orders) >= self.config.max_passes:
            return self._write(wi, dict(wi, state="exhausted", terminalReason=(
                f"{len(orders)} worker passes used; FACTORY_MAX_PASSES is {self.config.max_passes}")),
                "exhausted")
        failed = {h["detail"].get("worker") for h in self.store.history(wi["id"])
                  if h["action"] == "attempt_failed"}
        worker = routing.route(self.config.workers, self.config.local_only(wi["repo"]), failed)
        if worker is None:
            return self._write(wi, dict(wi, state="human_waiting", blockedOn=(
                f"{wi['repo']} is local-only and FACTORY_WORKERS has no local worker")), "no_worker")
        attempt = sum(1 for o in orders if o["round"] == wi.get("gateRound", 0) and o["kind"] == kind)
        base = self.github.branch_head(wi["repo"], self._default_branch(wi["repo"]))
        order = documents.workorder(wi, kind, attempt, self.config, base, findings)
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
            return self._write(wi, dict(done, state="reviewing"), "pushed")
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

    def resolve_human(self, workitem_id, resolution, note, by):
        """A person's answer to human_waiting or blocked (D5): retry, regate or close."""
        if not self.store.acquire(workitem_id, self.holder):
            raise ToolError(f"{workitem_id} is being ticked; try again")
        try:
            wi = self.store.get(workitem_id)
            if wi is None or wi["state"] not in ("human_waiting", "blocked"):
                raise ToolError(f"{workitem_id} is not waiting on a person")
            if resolution == "retry":
                new = dict(wi, state="changes_requested" if wi.get("prNumber") else "ready")
            elif resolution == "regate" and wi.get("prNumber"):
                new = dict(wi, state="reviewing")
            elif resolution == "close":
                new = dict(wi, state="cancelled", terminalReason=f"closed by {by}: {note}")
            else:
                raise ToolError("resolution must be retry, regate (needs a PR) or close")
            return self._write(wi, dict(new, blockedOn=None, attempts=[]), "human_resolved",
                               resolution=resolution, note=note, by=by)
        finally:
            self.store.release(workitem_id, self.holder)

    def propose_issue(self, repo, title, body):
        """Planner: open an issue labelled factory:proposed. A person adds factory:ready."""
        repo = self._allowlisted(repo)
        criteria, missing = workitem.parse_acceptance(body)
        if missing or not criteria:
            raise ToolError("The body needs an '## Acceptance' list of "
                            "'- <claim> — done when: <check>' bullets")
        issue = self.github.create_issue(repo, title, body, [workitem.PROPOSED])
        self.store.log("proposed", {"title": title}, workitem.workitem_id(repo, issue["number"]))
        return {"issue": issue["number"], "url": issue.get("html_url")}
