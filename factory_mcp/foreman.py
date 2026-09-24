"""The Foreman: runs intake and ticks against the store, GitHub and (later) the relay."""
from . import workitem
from .mcp import ToolError
from .store import Conflict


class Foreman:
    def __init__(self, store, github, config):
        self.store = store
        self.github = github
        self.config = config

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
        return self.tick_one(wi["id"])[1]

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
        new = workitem.reconcile(wi, workitem.observe(self.github, wi))
        if new != wi:
            try:
                wi = self.store.put(new)
            except Conflict:
                return before, self.store.get(workitem_id)  # another tick wrote first; it stands
            self.store.log("reconcile", {"from": before, "to": wi["state"]}, workitem_id)
        return before, wi
