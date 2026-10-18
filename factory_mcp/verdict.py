"""Read pr-gatekeeper's verdict: the last PR comment carrying `<!-- pr-gatekeeper {json} -->`."""
import hashlib
import json
import re

MARKER = "<!-- pr-gatekeeper "
GATE_LOGIN = "github-actions[bot]"  # the gate posts with its workflow's GITHUB_TOKEN


def fingerprint(path, problem):
    """Identity of "the same problem" (ADR-0005): path + normalized text, never the line number."""
    text = " ".join(re.sub(r"[^a-z0-9]+", " ", (problem or "").lower()).split())
    return hashlib.sha256(f"{path or ''}\n{text}".encode()).hexdigest()[:16]


def parse_marker(body):
    """The marker JSON in a comment body, or None. `\\u003e` escapes decode to `>` in json.loads."""
    start = (body or "").rfind(MARKER)
    if start < 0:
        return None
    end = body.find(" -->", start + len(MARKER))
    if end < 0:
        return None
    try:
        value = json.loads(body[start + len(MARKER):end])
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def to_verdict(marker, repo, pr_number, published_at):
    """A factory.verdict/v1 document, or None if the marker lacks what the contract requires."""
    if marker.get("outcome") not in ("MERGE", "CHANGES", "HUMAN") \
            or not isinstance(marker.get("round"), int) or not marker.get("head_sha"):
        return None
    findings = []
    for f in marker.get("findings") or []:
        if not isinstance(f, dict) or not f.get("problem"):
            continue
        findings.append({
            "fingerprint": fingerprint(f.get("path"), f["problem"]),
            "severity": "blocking" if f.get("severity") == "blocking" else "minor",
            "path": f.get("path"), "line": f.get("line") if isinstance(f.get("line"), int) else None,
            "problem": f["problem"], "fix": f.get("fix"), "test": f.get("test"),
        })
    if marker.get("outcome") == "CHANGES":  # the pipeline's problems are blocking findings (ADR-0084)
        for p in marker.get("problems") or []:
            if not isinstance(p, dict) or not p.get("problem"):
                continue
            findings.append({
                "fingerprint": fingerprint(p.get("path"), p["problem"]),
                "severity": "blocking", "path": p.get("path"), "line": None,
                "problem": p["problem"], "fix": None, "test": None,
            })
    return {"schemaVersion": "factory.verdict/v1", "repo": repo, "prNumber": pr_number,
            "round": marker["round"], "headSha": marker["head_sha"], "outcome": marker["outcome"],
            "findings": findings, "publishedAt": published_at}


def blocking(verdict):
    """The findings a fix must address: the blocking ones, or all of them if none is blocking."""
    found = verdict.get("findings") or []
    return [f for f in found if f["severity"] == "blocking"] or found


def blockers(history, latest=None):
    """WorkItem.blockers from every verdict on the PR, oldest first (ADR-0005). A problem keeps the
    round it was first reported in; it is resolved while the latest verdict does not report it."""
    first_seen = {}
    for verdict in history:
        for finding in blocking(verdict):
            first_seen.setdefault(finding["fingerprint"], verdict["round"])
    latest_findings = latest if latest is not None else (history[-1] if history else None)
    unresolved = {f["fingerprint"] for f in blocking(latest_findings)} if latest_findings else set()
    return [{"fingerprint": fp, "firstSeenRound": r, "resolved": fp not in unresolved}
            for fp, r in first_seen.items()]


def verdicts(comments, repo, pr_number):
    """Every verdict on the PR, oldest first. Only the gate's own comments count: anyone who can
    comment can also write a marker."""
    found = []
    for comment in comments:
        if (comment.get("user") or {}).get("login") != GATE_LOGIN:
            continue
        marker = parse_marker(comment.get("body"))
        verdict = marker and to_verdict(marker, repo, pr_number, comment.get("created_at"))
        if verdict:
            found.append(verdict)
    return found
