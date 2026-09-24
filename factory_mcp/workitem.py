"""WorkItems from GitHub facts: intake, observation, and the fact-driven half of the state machine.

`reconcile` is pure. It moves a WorkItem only on facts GitHub reports (labels, PR, merge, verdict).
Transitions that are the Foreman's own actions (dispatch, open PR) live in the tick, not here.
"""
import re

from .store import now
from .verdict import verdicts

READY, PROPOSED = "factory:ready", "factory:proposed"
TERMINAL = frozenset({"closed", "cancelled", "exhausted", "failed"})
# The Foreman owns these: a worker is running, or a human holds the WorkItem.
BUSY = frozenset({"dispatching", "implementing", "human_waiting", "blocked"})
OUTCOME_STATE = {"MERGE": "merge_pending", "CHANGES": "changes_requested", "HUMAN": "human_waiting"}


def workitem_id(repo, number):
    return f"{repo}#{number}"


def branch_for(number):
    return f"factory/{number}"


def labels(issue):
    return {label["name"] for label in issue.get("labels", [])}


def parse_acceptance(body):
    """`- <claim> — done when: <check>` bullets under an `Acceptance` heading.

    Returns (criteria, bullets_missing_a_check).
    """
    criteria, missing, inside = [], [], False
    for line in (body or "").splitlines():
        if re.match(r"\s*#+\s", line):
            inside = bool(re.match(r"\s*#+\s*acceptance\b", line, re.I))
            continue
        bullet = re.match(r"\s*[-*]\s+(?:\[[ xX]\]\s+)?(.+)", line)
        if not inside or not bullet:
            continue
        parts = re.split(r"done when:?", bullet.group(1), maxsplit=1, flags=re.I)
        claim = parts[0].strip().rstrip("—–-|;:(").strip()
        check = parts[1].strip().rstrip(")").strip() if len(parts) == 2 else ""
        if claim and check:
            criteria.append({"claim": claim, "doneWhen": check})
        else:
            missing.append(bullet.group(1).strip())
    return criteria, missing


def intake(repo, issue):
    """A new WorkItem (stateVersion 0) for an open issue, or a ValueError saying what is missing."""
    if "pull_request" in issue:
        raise ValueError(f"{repo}#{issue['number']} is a pull request, not an issue")
    if issue["state"] != "open":
        raise ValueError(f"{repo}#{issue['number']} is closed")
    criteria, missing = parse_acceptance(issue.get("body"))
    if missing or not criteria:
        raise ValueError("The issue needs an '## Acceptance' list of "
                         "'- <claim> — done when: <check>' bullets. Without a check: "
                         + ("; ".join(missing) or "no bullets found"))
    names = labels(issue)
    state = "ready" if READY in names else "planned" if PROPOSED in names else "issued"
    stamp = now()
    return {"schemaVersion": "factory.workitem/v1", "id": workitem_id(repo, issue["number"]),
            "repo": repo, "issueNumber": issue["number"], "goal": issue["title"],
            "acceptance": criteria, "branch": branch_for(issue["number"]), "prNumber": None,
            "headSha": None, "reviewedSha": None, "gateRound": 0, "attempts": [], "blockers": [],
            "state": state, "stateVersion": 0, "createdAt": stamp, "updatedAt": stamp}


def observe(github, wi):
    """The GitHub facts a WorkItem is reconciled against."""
    issue = github.issue(wi["repo"], wi["issueNumber"])
    facts = {"issue": {"state": issue["state"], "labels": sorted(labels(issue))},
             "pr": None, "verdicts": [], "gate": None}
    pulls = github.pulls_for_branch(wi["repo"], wi["branch"])
    if pulls:
        pr = max(pulls, key=lambda p: p["number"])
        facts["pr"] = {"number": pr["number"], "state": pr["state"], "merged": bool(pr.get("merged_at")),
                       "headSha": pr["head"]["sha"], "baseSha": pr["base"]["sha"]}
        facts["verdicts"] = verdicts(github.comments(wi["repo"], pr["number"]), wi["repo"], pr["number"])
        runs = github.check_runs(wi["repo"], pr["head"]["sha"])
        if runs:
            facts["gate"] = {"status": runs[0]["status"], "conclusion": runs[0].get("conclusion")}
    return facts


def current_verdict(wi, facts):
    """The latest verdict if it is for the PR's head and newer than the one already accepted."""
    if not facts["verdicts"] or not facts["pr"]:
        return None
    latest = facts["verdicts"][-1]
    if latest["headSha"] != facts["pr"]["headSha"] or latest["round"] < wi.get("gateRound", 0):
        return None  # stale: reviewed an older commit, or an older round
    if latest["headSha"] == wi.get("reviewedSha") and latest["round"] == wi.get("gateRound"):
        return None  # already accepted
    return latest


def reconcile(wi, facts):
    """The WorkItem moved to what GitHub says is true. Returns a new dict; never mutates."""
    wi = dict(wi)
    if wi["state"] in TERMINAL:
        return wi
    issue, pr = facts["issue"], facts["pr"]
    if pr:
        wi.update(prNumber=pr["number"], headSha=pr["headSha"], baseSha=pr["baseSha"])
        if pr["merged"]:
            wi["state"] = "closed" if issue["state"] == "closed" else "merged"
            return wi
    if issue["state"] == "closed":
        return dict(wi, state="cancelled", terminalReason="issue closed without a merged PR")
    if wi["state"] == "merged":
        return wi
    if pr is None:
        if wi["state"] in ("issued", "planned"):
            names = set(issue["labels"])
            wi["state"] = "ready" if READY in names else "planned" if PROPOSED in names else "issued"
        return wi
    if wi["state"] in BUSY:
        return wi
    if pr["state"] == "closed":
        return dict(wi, state="human_waiting", blockedOn="PR closed without merge")
    verdict = current_verdict(wi, facts)
    if verdict:
        wi.update(gateRound=verdict["round"], reviewedSha=verdict["headSha"],
                  state=OUTCOME_STATE[verdict["outcome"]])
    elif wi["state"] in ("issued", "planned", "ready"):
        wi["state"] = "pr_open"  # rebuilt from GitHub: the PR exists already
    if wi["state"] == "pr_open" and facts["gate"]:
        wi["state"] = "reviewing"
    if wi["state"] == "reviewing" and not verdict and facts["gate"] \
            and facts["gate"]["status"] == "completed" and wi.get("reviewedSha") != pr["headSha"]:
        wi.update(state="human_waiting", blockedOn="gate finished on the head without a verdict")
    return wi
