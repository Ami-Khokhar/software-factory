"""Link repairs to context and separate merge facts from reported runtime measurements."""
import json
import re

from .knowledge import record_id, text
from .mcp import Tool, ToolError
from .github import GitHubError


def records(store, config, repo):
    if not isinstance(repo, str) or config.listed(repo) is None:
        raise ToolError("repo must be enabled")
    with store._lock:
        rows = store._db.execute("SELECT doc FROM dream_issues WHERE repo=? COLLATE NOCASE "
                                 "ORDER BY rowid DESC LIMIT 100", (repo,)).fetchall()
    return [json.loads(row[0]) for row in rows]


def phase(store, record):
    wi = store.get(f"{record['repo']}#{record['issueNumber']}") if record.get("issueNumber") else None
    state = "merged" if wi and wi["state"] in ("merged", "closed") else wi["state"] if wi else "proposed"
    return state, wi


def refresh(foreman, at):
    """Persist factual merge context. Do not infer deployment or a successful repair."""
    from .improvements import save
    store, config = foreman.store, foreman.config
    heads = {}
    for repo in sorted(config.repos):
        for record in records(store, config, repo):
            state, wi = phase(store, record)
            if state != "merged" or not wi.get("headSha") or not record.get("memoryId"):
                continue
            key = "fix:" + record["fingerprint"]
            identity = record_id(repo, "", key)
            if not store.knowledge.search(config, {"repo": repo, "id": identity})["memories"]:
                root = store.knowledge.search(config, {"repo": repo, "id": record["memoryId"]})["memories"]
                context = root[0]["text"][:300] if root else "the recorded blocker"
                store.knowledge.put(config, {"repo": repo, "key": key, "kind": "fix", "status": "approved",
                    "text": f"Proposed repair for {context}: issue #{record['issueNumber']} merged at PR head {wi['headSha']}. "
                            "Deployment and runtime effect remain unverified.",
                    "source": f"https://github.com/{repo}/pull/{wi['prNumber']}" if wi.get("prNumber")
                              else f"https://github.com/{repo}/issues/{record['issueNumber']}",
                    "links": [{"id": record["memoryId"], "relation": "addresses"}]}, at.isoformat())
            if record.get("fixMemoryId") != identity:
                record["fixMemoryId"] = identity
                save(store, record["fingerprint"], repo, {"fixMemoryId": identity})
            measurement = record.get("validation")
            if not measurement or measurement["direction"] == "needs_revalidation":
                continue
            expected_version = measurement["version"]
            try:
                if repo not in heads:
                    branch = foreman.github.repository(repo)["default_branch"]
                    heads[repo] = foreman.github.branch_head(repo, branch)
                head = heads[repo]
                if not head or head == record.get("checkedHead"):
                    continue
                files = foreman.github.changed_files(repo, measurement["revision"], head) if head != measurement["revision"] else []
                names = {name for file in files for name in (file["filename"], file.get("previous_filename"))}
                stale = not record.get("paths") or len(files) >= 300 or bool(names & set(record["paths"]))
            except GitHubError:
                stale, head = True, None
            if stale:
                current = store.knowledge.search(config, {"repo": repo, "id": measurement["memoryId"]})["memories"]
                if current and current[0]["status"] == "approved":
                    node = current[0]
                    # A concurrent validation must win before this versioned graph update.
                    updated = store.knowledge.put(config, {key: node[key] for key in (
                        "repo", "projectId", "key", "kind", "text", "source", "version", "links")} | {
                            "status": "candidate", "version": expected_version}, at.isoformat())
                    measurement["version"] = updated["version"]
                measurement["direction"] = "needs_revalidation"
            save(store, record["fingerprint"], repo, {"checkedHead": head, "validation": measurement},
                 expected_validation=expected_version)


def report(store, config, args):
    if set(args) != {"repo"}:
        raise ToolError("improvement status requires only repo")
    rows = records(store, config, args["repo"])
    exposures = {}
    orders = {order["id"]: order for order in store.all_workorders()
              if order["repo"].lower() == args["repo"].lower()}
    histories = {}
    for wi in store.all():
        if wi["repo"].lower() != args["repo"].lower():
            continue
        wid, current = wi["id"], None
        histories[wid] = store.history(wid)
        for position, event in enumerate(histories[wid]):
            detail = event["detail"]
            if event["action"] == "dispatch":
                current = orders.get(detail.get("workOrder"))
            received = (current.get("memory", []) if current and event["action"] == "submitted"
                        else detail.get("memories", []) if event["action"] == "memory_read" else [])
            for item in received:
                exposures.setdefault(item["id"], {}).setdefault(wid, position)
    for row in rows:
        state, wi = phase(store, row)
        row["implementation"] = state
        row["workerPasses"] = len(store.workorders(wi["id"])) if wi else 0
        row["failedPasses"] = sum(h["action"] == "attempt_failed" for h in store.history(wi["id"])) if wi else 0
        seen = {}
        for id_ in (row.get("fixMemoryId"), (row.get("validation") or {}).get("memoryId")):
            for wid, first in exposures.get(id_, {}).items():
                seen[wid] = min(seen.get(wid, first), first)
        row["exposedWorkItems"] = len(seen)
        row["failuresAfterExposure"] = sum(any(h["action"] == "attempt_failed"
            for h in histories[wid][first + 1:]) for wid, first in seen.items())
        row["effect"] = (row.get("validation") or {}).get("direction", "unknown")
    return {"improvements": rows, "limit": 100,
            "note": "Failures after exposure are not proof of same-cause recurrence; zero exposure leaves improvement unknown."}


def validate(foreman, args, at):
    allowed = {"repo", "fingerprint", "revision", "source", "baselineOpportunities",
               "baselineRecurrences", "opportunities", "recurrences", "version"}
    if set(args) - allowed or not allowed - {"version"} <= set(args):
        raise ToolError("validation requires repo, fingerprint, revision, source and before/after counts")
    rows = records(foreman.store, foreman.config, args["repo"])
    row = next((row for row in rows if row.get("fingerprint") == args["fingerprint"]), None)
    if row is None or phase(foreman.store, row)[0] != "merged":
        raise ToolError("only a known merged improvement can receive deployment evidence")
    expected_version = (row.get("validation") or {}).get("version", 0)
    revision = args["revision"]
    if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise ToolError("revision must be a full deployed commit SHA")
    source = text(args["source"], "source", 500)
    for opportunities, recurrences in (("baselineOpportunities", "baselineRecurrences"), ("opportunities", "recurrences")):
        if type(args[opportunities]) is not int or not 1 <= args[opportunities] <= 1000000 or \
                type(args[recurrences]) is not int or not 0 <= args[recurrences] <= args[opportunities]:
            raise ToolError("measurement requires positive opportunity counts and bounded recurrence counts")
    before = args["baselineRecurrences"] / args["baselineOpportunities"]
    after = args["recurrences"] / args["opportunities"]
    direction = "observed_better" if after < before else "not_observed_better"
    key = "effect:" + row["fingerprint"]
    root = foreman.store.knowledge.search(foreman.config, {"repo": args["repo"], "id": row["memoryId"]})["memories"]
    context = root[0]["text"][:300] if root else "the recorded blocker"
    node = foreman.store.knowledge.put(foreman.config, {"repo": args["repo"], "key": key,
        "kind": "learning", "status": "approved" if after < before else "contradicted",
        "version": args.get("version", 0), "source": source,
        "text": f"Owner-reported measurement for {context}, improvement #{row['issueNumber']} at {revision}: "
                f"{args['recurrences']}/{args['opportunities']} recurrences after deployment versus "
                f"{args['baselineRecurrences']}/{args['baselineOpportunities']} before. "
                "This is an observation, not a guarantee for other tasks.",
        "links": [{"id": row["memoryId"], "relation": "evaluates"}]}, at.isoformat())
    from .improvements import save
    row["validation"] = {**args, "origin": "owner_reported", "direction": direction, "memoryId": node["id"],
                         "version": node["version"], "at": at.isoformat()}
    save(foreman.store, row["fingerprint"], row["repo"], {"validation": row["validation"], "checkedHead": None},
         expected_validation=expected_version)
    return row["validation"]


def tools(store, foreman, clock):
    integer = {"type": "integer", "minimum": 0, "maximum": 1000000}
    return [Tool("factory_improvement_status", "Read repair progress, memory exposure and reported "
                 "effects. Merge alone is not deployment or proof of improvement.",
                 {"type": "object", "required": ["repo"], "additionalProperties": False,
                  "properties": {"repo": {"type": "string"}}}, "read",
                 lambda args: report(store, foreman.config, args)),
            Tool("factory_improvement_validate", "Owner: attach before/after measurements and deployment "
                 "evidence to a merged repair. Counts are owner-reported, not executed by this tool.",
                 {"type": "object", "additionalProperties": False,
                  "required": ["repo", "fingerprint", "revision", "source", "baselineOpportunities",
                               "baselineRecurrences", "opportunities", "recurrences"],
                  "properties": {"repo": {"type": "string"}, "fingerprint": {"type": "string"},
                      "revision": {"type": "string"}, "source": {"type": "string", "maxLength": 500},
                      "version": integer, "baselineOpportunities": integer, "baselineRecurrences": integer,
                      "opportunities": integer, "recurrences": integer}}, "write",
                 lambda args: validate(foreman, args, clock()))]
