"""Owner approval of original gate follow-ups. GitHub records supply provenance."""
import hashlib
import json
import re
from datetime import datetime

from . import repos
from .mcp import ToolError
from .verdict import GATE_LOGIN, parse_marker

WRAPPER = "Ami-Khokhar/pr-gatekeeper-workflow"


def digest(issue):
    """Bind identity, title and complete body, including acceptance and worker directives."""
    content = [issue.get("id"), issue["number"], issue.get("title"), issue.get("body"),
               (issue.get("user") or {}).get("id")]
    return hashlib.sha256(json.dumps(content, ensure_ascii=True).encode()).hexdigest()


def validate(github, repo, issue, source_pr):
    """The gate identity is the same GitHub Actions bot used for trusted Verdicts.

    A body claiming a source is insufficient: require GitHub's creation cross-reference,
    the merged source PR, and one real reviewed MERGE comment on its exact head.
    """
    def refuse():
        raise ToolError("gate follow-up provenance is missing, forged or ambiguous; "
                        "need the original bot creation link and one merged gate-reviewed source PR")

    bot = {"login": GATE_LOGIN, "id": 41898282}
    author = issue.get("user") or {}
    if any(author.get(k) != v for k, v in bot.items()) or not issue.get("id"):
        refuse()
    if not isinstance(source_pr, int) or isinstance(source_pr, bool) or source_pr <= 0:
        refuse()
    pr = github.pull(repo, source_pr)
    if (not pr.get("merged_at")
            or any((pr.get("merged_by") or {}).get(k) != v for k, v in bot.items())
            # head/base `repo` is null on GitHub when the repository was deleted
            or ((pr.get("base") or {}).get("repo") or {}).get("full_name") != repo
            or ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo):
        refuse()
    created = issue.get("created_at")
    if not created or created < pr["merged_at"]:
        refuse()
    try:
        created_time = datetime.fromisoformat(created.replace("Z", "+00:00"))
    except ValueError:
        refuse()
    links = []
    for event in github.issue_timeline(repo, source_pr):
        source = (event.get("source") or {}).get("issue") or {}
        actor = event.get("actor") or {}
        if (event.get("event") == "cross-referenced" and source.get("id") == issue["id"]
                and source.get("number") == issue["number"]
                and source.get("repository_url") == f"https://api.github.com/repos/{repo}"
                and all(actor.get(k) == v for k, v in bot.items())
                and event.get("created_at")):
            try:
                linked_time = datetime.fromisoformat(event["created_at"].replace("Z", "+00:00"))
                if not 0 <= (linked_time - created_time).total_seconds() <= 5:
                    continue
            except (ValueError, TypeError):
                continue
            links.append(event)
    if len(links) != 1:
        refuse()
    reviews = []
    for comment in github.comments(repo, source_pr):
        user = comment.get("user") or {}
        marker = parse_marker(comment.get("body"))
        if (all(user.get(k) == v for k, v in bot.items()) and marker
                and marker.get("outcome") == "MERGE" and marker.get("reviewed") is True
                and marker.get("head_sha") == pr["head"]["sha"]
                and comment.get("created_at", "~") <= pr["merged_at"]):
            reviews.append((comment, marker))
    if len(reviews) != 1:
        refuse()
    comment, marker = reviews[0]
    # Trust the caller pinned on the source PR's base, never a pin supplied by the issue/PR head.
    # The pin itself is parsed by the repo helper, so the two parsers of one file cannot drift.
    caller = github.file(repo, ".github/workflows/pr-gatekeeper.yml", pr["base"]["sha"]) or ""
    # `GATE_USES` matches only real `uses:` lines, so a comment kept by an upgrade is never a pin;
    # two jobs or an old live pin would still be ambiguous, so count the live pins and refuse any
    # caller that does not pin exactly one gate revision.
    pins = repos._gate_pins(caller)
    if (pins is None or len(repos.GATE_USES.findall(caller)) != 1
            or pins["uses"] != f"uses: {WRAPPER}/.github/workflows/gatekeeper.yml@"):
        refuse()
    pin = pins["usesSha"]
    runs = []
    for run in github.workflow_runs(repo, pr["head"]["sha"]):
        refs = run.get("referenced_workflows") or []
        trusted = [ref for ref in refs if re.fullmatch(
            r"Ami-Khokhar/pr-gatekeeper-workflow/\.github/workflows/gatekeeper\.yml@[0-9a-f]{40}",
            ref.get("path", "")) and ref["path"].endswith("@" + ref.get("sha", "missing")) and ref.get("sha") == pin]
        if (run.get("path") == ".github/workflows/pr-gatekeeper.yml"
                and run.get("head_sha") == pr["head"]["sha"]
                and run.get("event") == "pull_request" and run.get("conclusion") == "success"
                and run.get("created_at", "~") <= comment["created_at"] <= created
                and created <= run.get("updated_at", "") and len(trusted) == 1):
            runs.append((run, trusted[0]["sha"]))
    if len(runs) != 1:
        refuse()
    run, workflow_sha = runs[0]
    findings = marker.get("findings") or []
    # Require actual reviewed residual work, not just a copied source number or marker.
    residuals = [f for f in findings if f.get("severity") in ("minor", "nit")]
    body = issue.get("body") or ""
    if (any(f.get("severity") == "blocking" for f in findings) or not residuals
            or issue.get("title") != f"Follow-up to #{source_pr}: {pr.get('title')}"
            or not body.startswith(f"pr-gatekeeper merged #{source_pr} with nothing blocking.")
            or not all(f.get("problem") and f["problem"] in body
                       and f.get("fix") and f["fix"] in body for f in residuals)):
        refuse()
    return {"runId": run["id"], "workflowSha": workflow_sha, "sourcePr": source_pr,
            "reviewId": comment["id"], "reviewDigest": hashlib.sha256(comment["body"].encode()).hexdigest(),
            "headSha": pr["head"]["sha"],
            "issueId": issue["id"], "digest": digest(issue), "validation": "verified"}
