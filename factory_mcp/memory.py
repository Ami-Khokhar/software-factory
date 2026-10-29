"""Bounded, repo-scoped evidence from durable audit records; no model calls."""
from . import diagnostics
from .mcp import ToolError
from .knowledge import SECRET

# Tool arguments, prompts and arbitrary Result payloads are deliberately not exposed.
ACTIONS = ("dispatch", "attempt_failed", "result", "reconcile", "human_resolved",
           "worker_question", "question_answered")
FIELDS = ("from", "to", "workOrder", "worker", "commitSha", "headSha", "cause",
          "diagnosis", "reason", "verdict", "round", "note", "question", "questionText", "answerText")


def clean(value, limit=300):
    return "<redacted>" if SECRET.search(value) else diagnostics.sanitize(value, limit)


def events(store, config, args):
    """Read one stable snapshot page. Cursors are audit sequences, not offsets."""
    allowed = {"repo", "after", "through", "limit"}
    if set(args) - allowed:
        raise ToolError(f"unknown memory event arguments: {sorted(set(args) - allowed)}; "
                        f"allowed: {sorted(allowed)}")
    repo = args.get("repo")
    if not isinstance(repo, str) or config.listed(repo) is None:
        raise ToolError(f"{repo!r} must be enabled in FACTORY_REPOS or repos.json")
    after, limit, through = args.get("after", 0), args.get("limit", 50), args.get("through")
    if type(after) is not int or after < 0:
        raise ToolError("after must be a nonnegative integer")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ToolError("limit must be an integer from 1 to 100")
    if through is not None and (type(through) is not int or through < after):
        raise ToolError("through must be an integer at least after")
    repo = config.listed(repo)
    rows, through = store.memory_events(repo, ACTIONS, after, through, limit + 1)
    more = len(rows) > limit
    selected = []
    for row in rows[:limit]:
        detail = {}
        for key in FIELDS:
            value = row["detail"].get(key)
            if isinstance(value, str):
                detail[key] = clean(value)
            elif type(value) in (int, bool):
                detail[key] = value
        blocking = row["detail"].get("blocking")
        if isinstance(blocking, list):
            problems = [clean(item["problem"], 100) for item in blocking[:3]
                        if isinstance(item, dict) and isinstance(item.get("problem"), str)]
            if problems:
                detail["gateProblems"] = problems
        selected.append({"seq": row["seq"], "at": row["at"], "action": row["action"],
                         "workItemId": row["workItemId"], "detail": detail})
    return {"repo": repo, "events": selected, "through": through,
            "nextCursor": selected[-1]["seq"] if more else max(after, through),
            "hasMore": more}
