"""The Foreman: under a per-WorkItem lease, reconcile with GitHub, then act at most once."""
import uuid

from . import documents, workitem
from .github import GitHubError
from .mcp import ToolError
from .relay import RelayError
from .store import Conflict


class Foreman:
    def __init__(self, store, github, config, relay=None):
        self.store = store
        self.github = github
        self.config = config
        self.relay = relay
        self.holder = uuid.uuid4().hex

    def intake(self, repo, number):
        if repo not in self.config.repos:
            raise ToolError(f"{repo} is not in FACTORY_REPOS")
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
                before, after = self.tick_one(wi["id"])
                ticked.append({"id": wi["id"], "from": before, "to": after["state"]})
        return {"rebuilt": rebuilt, "ticked": ticked}

    def tick_one(self, workitem_id):
        """Returns (state before, WorkItem after)."""
        wi = self.store.get(workitem_id)
        before = wi["state"]
        if not self.store.acquire(workitem_id, self.holder):
            return before, wi
        try:
            wi = self._write(wi, workitem.reconcile(wi, workitem.observe(self.github, wi)), "reconcile")
            wi = self.act(wi)
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
        stored = self.store.put(new)
        self.store.log(action, dict(detail, **{"from": old["state"], "to": stored["state"]}), old["id"])
        return stored

    def act(self, wi):
        if wi["state"] == "ready":
            return self.dispatch(wi, "implement")
        if wi["state"] == "dispatching":  # the intent was written, the relay call may not have landed
            return self.submit(wi, self._order(wi))
        if wi["state"] == "implementing":
            return self.poll(wi)
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
        attempt = sum(1 for o in orders if o["round"] == wi.get("gateRound", 0) and o["kind"] == kind)
        base = self.github.branch_head(wi["repo"], self._default_branch(wi["repo"]))
        order = documents.workorder(wi, kind, attempt, self.config, base, findings)
        self.store.save_workorder(order)
        wi = self._write(wi, dict(wi, state="dispatching", activeWorkOrderId=order["id"]),
                         "dispatch", workOrder=order["id"])
        return self.submit(wi, order)

    def submit(self, wi, order):
        task = self.relay.submit(self.config.worker, documents.worker_input(order, wi["branch"]),
                                 order["idempotencyKey"], order["budget"]["wallMs"])
        return self._write(wi, dict(wi, state="implementing", provider="agent-relay",
                                    providerTaskId=task["id"], worker=task["agentId"],
                                    sessionHandle=task.get("sessionId"), sessionNative=False),
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
        errors = documents.check_result(result, order)
        if not errors and result["status"] != "completed":
            errors = [f"worker reported {result['status']}: {result.get('error')}"]
        if not errors and not result.get("artifact"):
            errors = ["patch Results are not handled yet"]
        files = []
        if not errors:
            errors, files = self.check_artifact(wi, order, result["artifact"])
        if errors:
            return self.fail(wi, order, f"{task['agentId']}: " + "; ".join(errors))
        sha = result["artifact"]["commitSha"]
        self.store.log("result", {"workOrder": order["id"], "commitSha": sha,
                                  "usage": result.get("usage")}, wi["id"])
        done = dict(wi, headSha=sha, attempts=[], activeWorkOrderId=None, providerTaskId=None)
        if wi.get("prNumber"):
            return self._write(wi, dict(done, state="reviewing"), "pushed")
        pr = self.open_pr(wi, order, files, result["evidence"], task["agentId"])
        return self._write(wi, dict(done, state="pr_open", prNumber=pr["number"]), "pr_opened",
                           pr=pr["number"])

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
        return self._write(wi, new, "attempt_failed", reason=reason)
