"""WorkItems from GitHub facts: intake, observation, and the fact-driven half of the state machine.

`reconcile` is pure. It moves a WorkItem only on facts GitHub reports (labels, PR, merge, verdict).
Transitions that are the Foreman's own actions (dispatch, open PR) live in the tick, not here.
"""
import re

from .github import GitHubError
from .store import now
from .verdict import blockers, verdicts

READY, PROPOSED = "factory:ready", "factory:proposed"
TERMINAL = frozenset({"closed", "cancelled", "exhausted"})
# The Foreman owns these: a worker is running, or a human holds the WorkItem.
BUSY = frozenset({"dispatching", "implementing", "human_waiting", "blocked"})
OUTCOME_STATE = {"MERGE": "merge_pending", "CHANGES": "changes_requested", "HUMAN": "human_waiting"}
# Gate rounds one problem may be reported in before a human takes it (ADR-0005).
ROUNDS_PER_PROBLEM = 2
# terminalReason of a PR merged without a MERGE verdict on its head (contracts, obligation 5).
UNGATED = "merged without a pr-gatekeeper MERGE verdict on"
DEPENDS_ON = re.compile(r"^\s*depends on #(\d+)\b", re.I | re.M)
HEADING = re.compile(r"\s*#+\s")
ACCEPTANCE = re.compile(r"\s*#+\s*acceptance\b", re.I)
WORKER = re.compile(r"^\s*worker:\s*(\S+)\s*$", re.I | re.M)
PATHS = re.compile(r"^\s*paths:\s*(.+?)\s*$", re.I | re.M)


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
        if HEADING.match(line):
            inside = bool(ACCEPTANCE.match(line))
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


def description(body):
    """The issue's text outside its Acceptance section: what the worker needs to know besides the
    checks (ADR-0038)."""
    kept, inside = [], False
    for line in (body or "").splitlines():
        if HEADING.match(line):
            inside = bool(ACCEPTANCE.match(line))
        if not inside:
            kept.append(line)
    return "\n".join(kept).strip()


def depends_on(body):
    """The issue numbers that `Depends on #n` lines name, in order (ADR-0024)."""
    return [int(number) for number in DEPENDS_ON.findall(body or "")]


def with_dependencies(body, numbers):
    """`body` with its `Depends on #n` lines replaced by one for each of `numbers`, as factory_start_plan
    writes them: at the top, then a blank line (ADR-0042). Each old line goes whole, with any text after
    the number."""
    rest = re.sub(DEPENDS_ON.pattern + r".*\n?", "", body or "", flags=re.I | re.M).lstrip("\n")
    lines = [f"Depends on #{number}" for number in numbers]
    return "\n".join(lines) + ("\n\n" if lines and rest else "\n" if lines else "") + rest


def with_workers(body, agents):
    """`body` with its `Worker: <id>` lines replaced by one for each of `agents`, as
    factory_set_worker writes them: at the top, then a blank line (ADR-0029). Each old line goes
    whole, with any text after the id. An empty `agents` leaves no Worker line, so routing falls
    back to FACTORY_WORKERS order."""
    rest = re.sub(WORKER.pattern + r".*\n?", "", body or "", flags=re.I | re.M).lstrip("\n")
    lines = [f"Worker: {agent}" for agent in agents]
    return "\n".join(lines) + ("\n\n" if lines and rest else "\n" if lines else "") + rest


def replacement_plan(graph, original, replacements):
    """The dependency changes that replace `original` with the ordered `replacements` (ADR-0059): the
    first replacement inherits the original's upstream dependencies, each next one depends on the one
    before it, and every direct dependent of the original moves its edge to the last replacement and
    keeps its other dependencies. Pure: `graph` maps each issue number to the numbers its 'Depends on'
    lines name; the result maps each issue number to its new list."""
    plan = {}
    for index, number in enumerate(replacements):
        plan[number] = list(graph.get(original) or ()) if index == 0 else [replacements[index - 1]]
    last = replacements[-1]
    for number, deps in graph.items():
        if original in deps:
            plan[number] = [last if dep == original else dep for dep in deps]
    return plan


def decomposition_plan(order):
    """An acceptance-preserving, ordered decomposition of an oversized WorkOrder (ADR-0079).

    Pure and deterministic: one slice per acceptance criterion, in order, each depending on the one
    before it, so running the slices in sequence delivers every check the original named. A
    WorkOrder with no criteria keeps a single slice for its goal. The slices are a proposal only;
    nothing here dispatches, replaces or spends budget."""
    slices = []
    for index, criterion in enumerate(order.get("acceptance") or [], 1):
        slices.append({"position": index, "claim": criterion["claim"], "doneWhen": criterion["doneWhen"],
                       "dependsOn": [index - 1] if index > 1 else []})
    if not slices:
        slices.append({"position": 1, "claim": order.get("goal") or "the goal",
                       "doneWhen": "the goal's checks pass", "dependsOn": []})
    return slices


def cycle_in(graph):
    """A cycle in a dependency graph, as the path of issue numbers that closes on itself, or None.
    Pure: `graph` maps each issue number to the numbers its 'Depends on' lines name."""
    state = {}  # number -> "open" while its upstream deps are walked, "done" after

    def walk(start, path):
        state[start] = "open"
        path.append(start)
        for dep in graph.get(start) or ():
            if state.get(dep) == "open":
                return path[path.index(dep):] + [dep]
            if dep not in state:
                cycle = walk(dep, path)
                if cycle:
                    return cycle
        path.pop()
        state[start] = "done"
        return None

    for number in graph:
        cycle = walk(number, [])
        if cycle:
            return cycle
    return None


def workers_named(body):
    """The relay agents that `Worker: <id>` lines choose, in order (#100)."""
    return WORKER.findall(body or "")


def paths_listed(body):
    """The globs that `Paths: <glob>, <glob>` lines list, in order (ADR-0087): the source files a
    node's pass may change. No line means no fence."""
    return [glob for line in PATHS.findall(body or "")
            for glob in (part.strip() for part in line.split(",")) if glob]


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
            "description": description(issue.get("body")), "acceptance": criteria,
            "paths": paths_listed(issue.get("body")) or None, "wallMsOverride": None,
            "branch": branch_for(issue["number"]), "prNumber": None,
            "headSha": None, "reviewedSha": None, "gateRound": 0, "attempts": [], "blockers": [],
            "pendingRetry": None, "state": state, "stateVersion": 0,
            "createdAt": stamp, "updatedAt": stamp}


def observe(github, wi):
    """The GitHub facts a WorkItem is reconciled against."""
    issue = github.issue(wi["repo"], wi["issueNumber"])
    facts = {"issue": {"state": issue["state"], "labels": sorted(labels(issue)),
                       "dependsOn": depends_on(issue.get("body")), "workers": workers_named(issue.get("body"))},
             "pr": None, "verdicts": [], "gate": None}
    pulls = github.pulls_for_branch(wi["repo"], wi["branch"])
    if pulls:
        pr = max(pulls, key=lambda p: p["number"])
        facts["pr"] = {"number": pr["number"], "state": pr["state"], "merged": bool(pr.get("merged_at")),
                       "headSha": pr["head"]["sha"], "mergeable": None}
        if pr["state"] == "open":
            try:
                facts["pr"]["mergeable"] = github.pull(wi["repo"], pr["number"]).get("mergeable")
            except GitHubError as exc:  # a PR gone or unreadable: not known, as while GitHub computes it
                if exc.status not in (403, 404):
                    raise
        facts["verdicts"] = verdicts(github.comments(wi["repo"], pr["number"]), wi["repo"], pr["number"])
        facts["gate"] = gate_run(github, wi["repo"], pr["head"]["sha"])
    return facts


def gate_run(github, repo, sha):
    """The gate's run on `sha` as {status, conclusion}, or None. A fine-grained token cannot read
    check runs on a private repo (403), so the workflow run stands in for it; when the token cannot
    read that either, the verdict comments and FACTORY_GATE_WAIT_SECONDS still move the WorkItem."""
    try:
        runs = github.check_runs(repo, sha)
    except GitHubError as exc:
        if exc.status != 403:
            raise
        try:
            runs = github.workflow_runs(repo, sha)
        except GitHubError as again:
            if again.status != 403:
                raise
            return None
    runs = [run for run in runs if run.get("conclusion") != "cancelled"]  # a newer push or edit cancels a run
    return {"status": runs[0]["status"], "conclusion": runs[0].get("conclusion"),
            "startedAt": runs[0].get("started_at") or runs[0].get("run_started_at")} if runs else None


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


def gate_merged_base(commit, previous_head):
    """Whether a GitHub commit is the gate's merge of the base branch into a PR whose head was
    `previous_head` (ADR-0039): a merge on that head that a workflow's token made through GitHub's
    API, so GitHub signed it. No person's push or click on GitHub makes that commit."""
    parents = [p["sha"] for p in commit["parents"]]
    return ((commit.get("author") or {}).get("login") == "github-actions[bot]"
            and (commit.get("committer") or {}).get("login") == "web-flow"
            and commit["commit"]["verification"]["verified"]
            and len(parents) == 2 and parents[0] == previous_head)


def gated(facts):
    """True if pr-gatekeeper decided MERGE on the head that GitHub merged."""
    head = facts["pr"]["headSha"]
    return any(v["outcome"] == "MERGE" and v["headSha"] == head for v in facts["verdicts"])


def reconcile(wi, facts):
    """The WorkItem moved to what GitHub says is true. Returns a new dict; never mutates."""
    wi = dict(wi)
    if wi["state"] in TERMINAL:
        return wi
    issue, pr = facts["issue"], facts["pr"]
    if pr:
        wi.update(prNumber=pr["number"], headSha=pr["headSha"])
        if pr["merged"]:
            wi["state"] = "closed" if issue["state"] == "closed" else "merged"
            wi["blockedOn"] = None  # merged: nobody waits on a person any more
            if not gated(facts):  # a person merged it: a fact, but not the gate's decision (ADR-0004)
                wi["terminalReason"] = f"{UNGATED} {pr['headSha']}"
            return wi
    if issue["state"] == "closed":
        return dict(wi, state="cancelled", terminalReason="issue closed without a merged PR", blockedOn=None)
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
                  state=OUTCOME_STATE[verdict["outcome"]], blockers=blockers(facts["verdicts"]))
        if verdict["outcome"] == "HUMAN":
            wi["blockedOn"] = "pr-gatekeeper decided HUMAN; read the latest gate comment"
        stuck = [b["fingerprint"] for b in wi["blockers"] if not b["resolved"]
                 and verdict["round"] - b["firstSeenRound"] + 1 >= ROUNDS_PER_PROBLEM]
        if verdict["outcome"] == "CHANGES" and stuck:
            wi.update(state="human_waiting", blockedOn=(
                f"the same problem was reported in {ROUNDS_PER_PROBLEM} gate rounds: "
                + "; ".join(f["problem"] for f in verdict["findings"] if f["fingerprint"] in stuck)))
        elif verdict["outcome"] == "CHANGES" and not verdict["findings"]:  # e.g. only its PR lint failed
            wi.update(state="human_waiting", blockedOn=(
                f"pr-gatekeeper asked for changes on PR #{pr['number']} with no finding a worker could "
                "fix; read its latest comment, then retry with a note, regate or close"))
    elif wi["state"] == "changes_requested" and pr["headSha"] != wi.get("reviewedSha"):
        wi["state"] = "reviewing"  # someone pushed after the verdict; the gate re-runs on it
    elif wi["state"] in ("issued", "planned", "ready"):
        wi["state"] = "pr_open"  # rebuilt from GitHub: the PR exists already
    if wi["state"] == "pr_open" and facts["gate"]:
        wi["state"] = "reviewing"
    # a run that started before the last regate is the one the regate asked to repeat, not its answer;
    # a run with no start time is taken as the answer, so the WorkItem cannot wait on it forever
    started = (facts["gate"] or {}).get("startedAt")
    stale = bool(wi.get("regatedAt") and started and started < wi["regatedAt"])
    if wi["state"] == "reviewing" and not verdict and facts["gate"] and not stale \
            and facts["gate"]["status"] == "completed" and wi.get("reviewedSha") != pr["headSha"]:
        wi.update(state="human_waiting", blockedOn="gate finished on the head without a verdict")
    return wi
