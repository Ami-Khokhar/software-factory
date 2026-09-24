"""The factory tools OpenBot's Bots may call. Each one declares its effect."""
from .mcp import Tool, ToolError


def build(store, config):
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
    ]
    return {tool.name: tool for tool in tools}
