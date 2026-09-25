"""The Foreman: under a per-WorkItem lease, reconcile with GitHub, then act at most once."""
import calendar
import time
import uuid

from . import documents, patches, routing, workitem
from .github import GitHubError
from .mcp import ToolError
from .relay import RelayError
from .store import Conflict
from .verdict import blocking


class Foreman:
    def __init__(self, store, github, config, relay=None):
        self.store = store
        self.github = github
        self.config = config
        self.relay = relay
        self.holder = uuid.uuid4().hex

    def intake(self, repo, number):
        if repo not in self.config.repos:
            raise ToolError(f"{repo} is not an enabled factory repo (FACTORY_REPOS or repos.json)")
        existing = self.store.get(workitem.workitem_id(repo, number))
        if existing:
            return existing
        try:
            wi = workitem.intake(repo, self.github.issue(repo, number))
        except ValueError as exc:
            raise ToolError(str(exc)) from None
        wi = self.store.put(wi)
        self.store.log("intake", {"state": wi["state"]}, wi["id"])
        return wi

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
                        self.store.log("rebuild_skipped", {"reason": str(exc)},
                                       workitem.workitem_id(repo, issue["number"]))
        return found

    def tick(self, workitem_id=None):
        """Reconcile one WorkItem, or rebuild from GitHub and reconcile every live one."""
        if workitem_id is not None:
            if self.store.get(workitem_id) is None:
                raise ToolError(f"No WorkItem {workitem_id}")
            before, wi = self.tick_one(workitem_id)
            return {"id": workitem_id, "from": before, "to": wi["state"], "workItem": wi}
        rebuilt = self.rebuild()
        ticked = []
        for wi in self.store.all():
            if wi["state"] not in workitem.TERMINAL:
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
        return stored

    def act(self, wi, facts):
        if wi["state"] == "ready":
            return self.dispatch(wi, "implement")
        if wi["state"] == "changes_requested":  # findings travel as data (D6)
            findings = [{key: f[key] for key in ("fingerprint", "path", "line", "problem")}
                        for f in (blocking(facts["verdicts"][-1]) if facts["verdicts"] else [])]
            last = next((h for h in reversed(self.store.history(wi["id"]))
                         if h["action"] in ("human_resolved", "dispatch")), None)
            if last and last["action"] == "human_resolved" and last["detail"].get("note"):
                findings.append({"fingerprint": "human", "problem": last["detail"]["note"]})
            return self.dispatch(wi, "fix", findings)
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
        return wi

    def _order(self, wi):
        return next(o for o in self.store.workorders(wi["id"]) if o["id"] == wi["activeWorkOrderId"])

    def _default_branch(self, repo):
        return self.github.repository(repo)["default_branch"]

    def dispatch(self, wi, kind, findings=()):
        orders = self.store.workorders(wi["id"])
        if len(orders) >= self.config.max_passes:
            return self._write(wi, dict(wi, state="exhausted", terminalReason=(
                f"{len(orders)} worker passes used; FACTORY_MAX_PASSES is {self.config.max_passes}")),
                "exhausted")
        failed = {h["detail"].get("worker") for h in self.store.history(wi["id"])
                  if h["action"] == "attempt_failed"}
        worker = routing.route(self.config.workers_for(wi["repo"]), wi["repo"] in self.config.local_only_repos, failed)
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
                                 order["idempotencyKey"], order["budget"]["wallMs"])
        return self._write(wi, dict(wi, state="implementing", provider="agent-relay",
                                    providerTaskId=task["id"], sessionHandle=task.get("sessionId"),
                                    sessionNative=False),
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
        result = documents.parse_result(task.get("output"))
        if result is None and task.get("outputTruncated"):
            return self.fail(wi, order, f"{task['agentId']}: the relay truncated the output before "
                             "the Result line; raise AGENT_RELAY_MAX_OUTPUT_BYTES or quiet the harness")
        if isinstance(result, dict) and isinstance(result.get("usage"), dict):
            self.store.log("usage", result["usage"], wi["id"])  # the cost ledger, pass or fail
        errors = documents.check_result(result, order)
        if not errors and result["status"] != "completed":
            errors = [f"worker reported {result['status']}: {result.get('error')}"]
        artifact = result.get("artifact") if not errors else None
        if not errors and not artifact:
            try:
                artifact = self.push_patch(wi, order, result["patch"])
            except patches.PatchError as exc:
                errors = [f"patch did not apply: {exc}"]
        files = []
        if not errors:
            errors, files = self.check_artifact(wi, order, artifact)
        if errors:
            return self.fail(wi, order, f"{task['agentId']}: " + "; ".join(errors))
        sha = artifact["commitSha"]
        self.store.log("result", {"workOrder": order["id"], "commitSha": sha}, wi["id"])
        done = dict(wi, headSha=sha, attempts=[], activeWorkOrderId=None, providerTaskId=None)
        if result.get("session"):  # the harness's own session, resumable by a later fix
            done.update(sessionHandle=result["session"].get("handle"),
                        sessionNative=result["session"].get("native"))
        if wi.get("prNumber"):
            return self._write(wi, dict(done, state="reviewing"), "pushed")
        pr = self.open_pr(wi, order, files, result["evidence"], task["agentId"])
        return self._write(wi, dict(done, state="pr_open", prNumber=pr["number"]), "pr_opened",
                           pr=pr["number"])

    def push_patch(self, wi, order, patch):
        """Apply a patch Result on the WorkOrder's base and push it; the commit is then checked
        like any worker's artifact."""
        base = patch.get("baseSha") or (order["headSha"] if order["kind"] == "fix" else order["baseSha"])
        sha = patches.apply(self.config.git_url.format(repo=wi["repo"]), self.config.github_token,
                            wi["branch"], base, patch, f"{wi['goal']} (#{wi['issueNumber']})\n\n"
                            f"WorkOrder {order['id']}", force=order["kind"] != "fix")
        return {"branch": wi["branch"], "commitSha": sha}

    def check_artifact(self, wi, order, artifact):
        """Guards on what GitHub holds, never on what the worker says it did."""
        if artifact["branch"] != wi["branch"]:
            return [f"artifact branch {artifact['branch']} is not {wi['branch']}"], []
        head = self.github.branch_head(wi["repo"], wi["branch"])
        if head != artifact["commitSha"]:
            return [f"{wi['branch']} is at {head}, not the reported {artifact['commitSha']}"], []
        files = self.github.changed_files(wi["repo"], self._default_branch(wi["repo"]), head)
        limits = order["constraints"]
        errors = [f"changes protected path {f['filename']}" for f in files
                  if any(f["filename"] == p or (p.endswith("/") and f["filename"].startswith(p))
                         for p in limits["protectedPaths"])]
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
        if repo not in self.config.repos:
            raise ToolError(f"{repo} is not an enabled factory repo (FACTORY_REPOS or repos.json)")
        criteria, missing = workitem.parse_acceptance(body)
        if missing or not criteria:
            raise ToolError("The body needs an '## Acceptance' list of "
                            "'- <claim> — done when: <check>' bullets")
        issue = self.github.create_issue(repo, title, body, [workitem.PROPOSED])
        self.store.log("proposed", {"title": title}, workitem.workitem_id(repo, issue["number"]))
        return {"issue": issue["number"], "url": issue.get("html_url")}
