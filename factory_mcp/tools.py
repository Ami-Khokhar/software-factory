"""The factory tools OpenBot's Bots may call. Each one declares its effect."""
from .mcp import Tool, ToolError


def build(store, foreman):
    def status(args):
        workitem_id = args.get("id")
        if workitem_id is None:
            return {"workItems": [
                {key: wi.get(key) for key in ("id", "state", "prNumber", "updatedAt")}
                for wi in store.all()]}
        wi = store.get(workitem_id)
        if wi is None:
            raise ToolError(f"No WorkItem {workitem_id}")
        return {"workItem": wi, "history": store.history(workitem_id)}

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
    ]
    return {tool.name: tool for tool in tools}
