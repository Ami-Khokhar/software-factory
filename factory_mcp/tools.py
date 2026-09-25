"""The factory tools OpenBot's Bots may call. Each one declares its effect."""
from . import metrics
from .mcp import Tool, ToolError


def build(store, foreman):
    def status(args):
        workitem_id = args.get("id")
        if workitem_id is None:
            workitems = store.all()
            return {"workItems": [
                {key: wi.get(key) for key in ("id", "state", "prNumber", "blockedOn", "updatedAt")}
                for wi in workitems], "autonomy": metrics.autonomy(workitems, store.history)}
        wi = store.get(workitem_id)
        if wi is None:
            raise ToolError(f"No WorkItem {workitem_id}")
        history = store.history(workitem_id)
        return {"workItem": wi, "escalation": metrics.escalation(wi, history),
                "passes": len(store.workorders(workitem_id)), "history": history}

    tools = [
        Tool("factory_status",
             "Show one WorkItem with its history, or a summary of all WorkItems when no id is given.",
             {"type": "object", "properties": {"id": {"type": "string", "description": "owner/repo#issue"}},
              "additionalProperties": False},
             "read", status),
        Tool("factory_intake",
             "Create a WorkItem from an open GitHub issue in an allowlisted repo. The issue needs an "
             "'## Acceptance' list of '- <claim> — done when: <check>' bullets. Idempotent.",
             {"type": "object", "required": ["repo", "issue"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "issue": {"type": "integer", "minimum": 1}}},
             "write", lambda args: {"workItem": foreman.intake(args["repo"], args["issue"])}),
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
