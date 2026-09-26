"""The factory tools that Day Desk (Hermes) and the factory CLI call. Each one declares its effect."""
from . import metrics, repos
from .mcp import Tool, ToolError


def build(store, foreman):
    def status(args):
        workitem_id = args.get("id")
        if workitem_id is None:
            workitems = store.all()
            return {"workItems": [
                {key: wi.get(key) for key in ("id", "state", "prNumber", "blockedOn", "updatedAt")}
                for wi in workitems], "autonomy": metrics.autonomy(workitems, store.history),
                "repos": sorted(foreman.config.repos)}
        wi = store.get(workitem_id)
        if wi is None:
            raise ToolError(f"No WorkItem {workitem_id}")
        history = store.history(workitem_id)
        return {"workItem": wi, "escalation": metrics.escalation(wi, history),
                "passes": len(store.workorders(workitem_id)), "history": history}

    def repo_check(args):
        repo = args["repo"]
        if not repos.NAME.match(repo):
            raise ToolError(f"{repo!r} is not a repo name in the form owner/repo")
        checks = repos.check(foreman.github, repo)
        return {"repo": repo, "enabled": foreman.config.listed(repo) is not None,
                "ready": all(c["ok"] for c in checks), "checks": checks}

    tools = [
        Tool("factory_status",
             "Show one WorkItem with its history, or, when no id is given, a summary of all "
             "WorkItems and the repos the factory works on.",
             {"type": "object", "properties": {"id": {"type": "string", "description": "owner/repo#issue"}},
              "additionalProperties": False},
             "read", status),
        Tool("factory_repo_check",
             "Check what a GitHub repo needs before the factory can work on it: the factory's token "
             "can push to it, pr-gatekeeper is installed with a verify command, and its AGENTS.md "
             "carries the factory standard. Each failed check says how to fix it. Changes nothing.",
             {"type": "object", "required": ["repo"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"}}},
             "read", repo_check),
        Tool("factory_add_repo",
             "Enable a repo for the factory once a person agrees. It must pass factory_repo_check. "
             "Clones the worker checkout, creates the factory labels and adds the repo to repos.json; "
             "the factory works on it at once. Idempotent.",
             {"type": "object", "required": ["repo", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "by": {"type": "string", "minLength": 1, "description": "who agreed"}}},
             "write", lambda args: foreman.add_repo(args["repo"], args["by"])),
        Tool("factory_intake",
             "Create a WorkItem from an open GitHub issue in an allowlisted repo. The issue needs an "
             "'## Acceptance' list of '- <claim> — done when: <check>' bullets. Idempotent.",
             {"type": "object", "required": ["repo", "issue"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "issue": {"type": "integer", "minimum": 1}}},
             "write", lambda args: {"workItem": foreman.intake(args["repo"], args["issue"])}),
        Tool("factory_approve_issue",
             "A person's go on an issue: label it factory:ready and take it in, so the factory starts "
             "work on it. The issue must be open, opened by a trusted author, and have an "
             "'## Acceptance' list of '- <claim> — done when: <check>' bullets. Idempotent.",
             {"type": "object", "required": ["repo", "issue", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "issue": {"type": "integer", "minimum": 1},
                             "by": {"type": "string", "minLength": 1, "description": "who approved"}}},
             "write", lambda args: {"workItem": foreman.approve_issue(args["repo"], args["issue"], args["by"])}),
        Tool("factory_tick",
             "Reconcile one WorkItem against GitHub and take the one legal next action. Without an "
             "id, first adopt open 'factory:ready' issues, then tick every live WorkItem. Idempotent.",
             {"type": "object", "properties": {"id": {"type": "string", "description": "owner/repo#issue"}},
              "additionalProperties": False},
             "write", lambda args: foreman.tick(args.get("id"))),
        Tool("factory_resolve_human",
             "Record a person's decision on a WorkItem in human_waiting or blocked: retry (the "
             "factory tries again, with the note as a finding), regate (the person pushed a fix) or "
             "close.",
             {"type": "object", "required": ["id", "resolution", "note", "by"],
              "additionalProperties": False,
              "properties": {"id": {"type": "string"},
                             "resolution": {"enum": ["retry", "regate", "close"]},
                             "note": {"type": "string", "minLength": 1},
                             "by": {"type": "string", "minLength": 1, "description": "who decided"}}},
             "write", lambda args: {"workItem": foreman.resolve_human(
                 args["id"], args["resolution"], args["note"], args["by"])}),
        Tool("factory_propose_issue",
             "Planner only: open an issue labelled factory:proposed. The body needs an "
             "'## Acceptance' list of '- <claim> — done when: <check>' bullets. A person adds "
             "factory:ready to start it.",
             {"type": "object", "required": ["repo", "title", "body"], "additionalProperties": False,
              "properties": {"repo": {"type": "string"}, "title": {"type": "string", "minLength": 1},
                             "body": {"type": "string", "minLength": 1}}},
             "write", lambda args: foreman.propose_issue(args["repo"], args["title"], args["body"])),
    ]
    return {tool.name: tool for tool in tools}
