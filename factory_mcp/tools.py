"""The factory tools OpenBot's Bots may call. Each one declares its effect."""
from . import metrics
from .mcp import Tool, ToolError


REPO = {"type": "string", "pattern": "^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", "description": "owner/repo"}
CONFIRM = {"const": True, "description": "true only after the person asked for this in their own words"}
BY = {"type": "string", "minLength": 1, "description": "who asked"}


def build(store, foreman, ops=None):
    def status(args):
        workitem_id = args.get("id")
        if workitem_id is None:
            workitems = store.all()
            return {"workItems": [
                {key: wi.get(key) for key in ("id", "state", "prNumber", "blockedOn", "updatedAt")}
                for wi in workitems], "autonomy": metrics.autonomy(workitems, store.history),
                "needsYou": [metrics.escalation(wi, store.history(wi["id"])) | {"id": wi["id"]}
                             for wi in workitems if wi["state"] in ("human_waiting", "blocked")]}
        wi = store.get(workitem_id)
        if wi is None:
            raise ToolError(f"No WorkItem {workitem_id}")
        history = store.history(workitem_id)
        return {"workItem": wi, "escalation": metrics.escalation(wi, history),
                "cost": metrics.cost(history, len(store.workorders(workitem_id))), "history": history}

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
        Tool("factory_repos", "List the repos the factory works on.",
             {"type": "object", "properties": {}, "additionalProperties": False},
             "read", lambda args: {"repos": sorted(ops.config.repos)}),
        Tool("factory_repo_check",
             "Read-only sanity check of a GitHub repo for the factory: exists, not archived, "
             "pr-gatekeeper config with a verify command, gate workflow, verify tool in the worker "
             "image, labels, token access, branch protection, checkout root, trusted authors. "
             "Returns ok and a checklist; every failing check has a fix to tell the person. "
             "Changes nothing.",
             {"type": "object", "required": ["repo"], "additionalProperties": False,
              "properties": {"repo": REPO}},
             "read", lambda args: ops.repo_check(args["repo"])),
        Tool("factory_repo_status",
             "Configured repos with their risk, workers, trusted authors and WorkItem counts. With "
             "repo and live=true, also runs factory_repo_check on it (reads GitHub).",
             {"type": "object", "additionalProperties": False,
              "properties": {"repo": REPO, "live": {"type": "boolean"}}},
             "read", lambda args: ops.repo_status(args.get("repo"), bool(args.get("live")))),
        Tool("factory_add_repo",
             "Enable a prepared repo: runs factory_repo_check and refuses, changing nothing, if a "
             "required check fails; else clones the worker checkout, creates the factory labels and "
             "writes the repo manifest. Idempotent. Call only after the person said to enable this "
             "repo. It never creates repos or installs workflows: a person does.",
             {"type": "object", "required": ["repo", "confirm", "by"], "additionalProperties": False,
              "properties": {"repo": REPO, "risk": {"enum": ["normal", "local-only"]},
                             "trusted_authors": {"type": "array", "items": {"type": "string"}},
                             "workers": {"type": "array", "items": {"type": "string",
                                                                    "pattern": "^[^:]+:(local|hosted)$"}},
                             "confirm": CONFIRM, "by": BY}},
             "write", lambda args: ops.add_repo(args["repo"], args.get("risk"), args.get("trusted_authors"),
                                                args.get("workers"), args["by"])
             if args.get("confirm") is True else _unconfirmed()),
        Tool("factory_approve_issue",
             "Start an issue: adds factory:ready and creates its WorkItem. Refuses an unknown repo "
             "or issue, an author who is not trusted, or a body without '## Acceptance' checks. "
             "Idempotent. Call only after the person said to start this issue.",
             {"type": "object", "required": ["repo", "issue", "confirm", "by"],
              "additionalProperties": False,
              "properties": {"repo": REPO, "issue": {"type": "integer", "minimum": 1},
                             "confirm": CONFIRM, "by": BY}},
             "write", lambda args: ops.approve_issue(args["repo"], args["issue"], args["by"])
             if args.get("confirm") is True else _unconfirmed()),
    ]
    return {tool.name: tool for tool in tools}


def _unconfirmed():
    raise ToolError("confirm must be true: ask the person first")
