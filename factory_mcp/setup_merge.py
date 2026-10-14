"""Reviewed, owner-authorized, bootstrap-only merge of a repo's setup PR (ADR-0074).

The owner grants one authorization per repo (factory_authorize_setup_merge), pinned to a digest of
the canonical setup plan the moment they approve it. factory_merge_setup_pr then merges only the
one open setup PR that still exactly matches that digest: the same paths, per-file content, base
and head, and a green pr-gatekeeper run on that head. Everything else fails closed, before any
merge call: no grant, a plan that no longer matches (a moved base, a changed verify command, a
differing control file), an unexpected path or noncanonical content, a stale head, several open
setup PRs, and unknown, pending or failed checks. The tool calls GitHub's merge API with the
verified head (so branch protection still applies at GitHub), reads the merged PR back with its
merge commit, and reports whether the repo's checks would now pass so readiness is a fact, not
a hope.

Absent a grant, nothing changes: factory_bootstrap_repo still opens a setup PR for the owner's
manual merge, and the tool cannot merge a normal factory PR or touch any other ref.
"""
from . import repos, verdict
from .github import GitHubError
from .mcp import ToolError

BOOTSTRAP_PREFIX = "factory-bootstrap/"  # the setup branch name prefix, shared with the Foreman


def authorized_plan(store, github, repo):
    """(the grant row, the canonical plan) while `repo`'s authorization still matches its setup,
    or a refusal otherwise."""
    row = store.setup_merge(repo)
    if not row:
        raise ToolError(f"{repo} carries no setup-merge authorization: a person merges its setup "
                        "PR, or the owner grants one with factory_authorize_setup_merge")
    try:
        plan = repos.bootstrap_plan(github, repo, row["verify"] or None)
    except ValueError as exc:
        raise ToolError(f"{repo}'s setup no longer matches the approved plan: {exc}; the owner "
                        "reviews the setup PR and re-authorizes if it is right") from None
    if plan["fingerprint"] != row["fingerprint"]:
        raise ToolError(f"{repo}'s canonical setup changed since the owner's grant (a moved "
                        "default branch, a changed verify command or control file); the owner "
                        "re-authorizes it or merges the setup PR in person")
    return row, plan


def _setup_repo_head(pr):
    return ((pr["head"].get("repo") or {}).get("full_name") or "").lower()


def _setups_of(github, repo, base, branch=None):
    """A repo's open setup PRs into `base`, or, with `branch`, every PR of `repo` on that exact
    branch in any state."""
    if branch is None:
        prs = [pr for pr in github.open_pulls(repo, base)
               if pr["head"]["ref"].startswith(BOOTSTRAP_PREFIX)]
    else:
        prs = [pr for pr in github.pulls_for_branch(repo, branch) if pr["base"]["ref"] == base]
    return [pr for pr in prs if _setup_repo_head(pr).lower() == repo.lower()]


def _exact_setup_pr(github, repo, base, branch):
    """The one setup PR that may merge, or a refusal that keeps a duplicate or deputy from ever
    being merged instead (never duplicate PRs or dispatch prematurely)."""
    merged = [pr for pr in _setups_of(github, repo, base, branch) if pr.get("merged_at") or pr.get("merged")]
    open_ = [pr for pr in _setups_of(github, repo, base) if pr["state"] == "open"]
    if merged:
        raise ToolError(f"{repo}'s setup PR #{merged[0]['number']} is already merged")
    if len(open_) > 1:
        numbers = ", ".join(f"#{pr['number']}" for pr in open_)
        raise ToolError(f"{repo} has several open setup PRs ({numbers}); close all but one")
    if not open_:
        raise ToolError(f"{repo} has no open setup PR into {base}: call factory_bootstrap_repo, "
                        "then ask again once it is open")
    if open_[0]["head"]["ref"] != branch:
        raise ToolError(f"{repo}'s open setup PR #{open_[0]['number']} is on branch "
                        f"{open_[0]['head']['ref']}, not the authorized setup branch {branch}")
    return open_[0]


def _at_head(github, repo, plan, pr):
    """Why `pr` is not the exact canonical setup the plan names, or None. It only reads."""
    base, head = plan["base_sha"], pr["head"]["sha"]
    if pr["base"]["ref"] != plan["default_branch"]:
        return f"the setup PR targets {pr['base']['ref']}, not the default branch {plan['default_branch']}"
    if github.branch_head(repo, plan["default_branch"]) != base:
        return f"the default branch moved off the approved base {base}; the owner re-authorizes"
    if github.branch_head(repo, pr["head"]["ref"]) != head:
        return f"the setup PR's head moved off {head} since this call read it; try again"
    changed = github.changed_files(repo, base, head)
    names = {entry["filename"] for entry in changed}
    if names != set(plan["files"]):
        return f"the setup PR changes {sorted(names)}, not exactly the approved {sorted(plan['files'])}"
    for name, text in plan["files"].items():
        if github.file(repo, name, ref=head) != text:
            return f"{name} on the setup PR's head differs from the approved canonical content"
    runs = github.check_runs(repo, head)
    if not runs:
        return f"the setup PR's head {head} shows no pr-gatekeeper check run at all"
    for run in runs:
        if run["status"] != "completed":
            return f"the setup PR's pr-gatekeeper check is still {run['status']}; wait and ask again"
        if run["conclusion"] != "success":
            return f"the setup PR's pr-gatekeeper check concluded {run['conclusion']}; a person decides"
    latest = verdict.verdicts(github.comments(repo, pr["number"]), repo, pr["number"])
    if latest and latest[-1]["headSha"] == head and latest[-1]["outcome"] != "MERGE":
        return f"the gate's latest verdict on this head is {latest[-1]['outcome']} with findings; a person decides"
    return None


def _merged_setup(store, github, repo):
    """The merged setup PRs on the branch the grant names, for resuming an ambiguous merge:
    the branch pins to the grant row, so this needs no fresh digest of a setup that just landed."""
    row = store.setup_merge(repo)
    if not row:
        return []
    branch = BOOTSTRAP_PREFIX + row["fingerprint"][:16]
    return [pr for pr in github.pulls_for_branch(repo, branch)
            if _setup_repo_head(pr) == repo.lower() and (pr.get("merged_at") or pr.get("merged"))]


def merge(store, github, repo, by, request_id):
    """Merge the authorized setup PR of `repo` once, exactly, and read the delivery back."""
    request = store.open_request(request_id, "merge_setup_pr", {"repo": repo})
    if request["phase"] == "done":
        return _adopt(store, github, repo, by, request_id)  # a retry of a finished merge
    merged = _merged_setup(store, github, repo)
    if len(merged) == 1:  # an interrupted or ambiguous merge already went through
        return _readback(store, github, repo, merged[0]["number"], by, request_id, already=True)
    if len(merged) > 1:
        raise ToolError(f"{repo} shows several merged setup PRs for its approved branch; a person "
                        "inspects GitHub and the store stays as it is")
    _, plan = authorized_plan(store, github, repo)
    pr = _exact_setup_pr(github, repo, plan["default_branch"], BOOTSTRAP_PREFIX + plan["fingerprint"][:16])
    if (reason := _at_head(github, repo, plan, pr)) is not None:
        raise ToolError(f"the setup PR #{pr['number']} of {repo} is refused: {reason}")
    if request["phase"] == "started":  # journalled once the plan, PR and head are verified
        store.advance_request(request_id, "pushing")
        store.log("setup_merge_journalled", {"repo": repo, "requestId": request_id,
                                             "prNumber": pr["number"], "headSha": pr["head"]["sha"]})
    try:
        github.merge_pull(repo, pr["number"], pr["head"]["sha"])
    except GitHubError as exc:
        raise ToolError(f"the setup PR #{pr['number']} of {repo} was not merged: {exc}; "
                        "GitHub refused the merge and it stays open") from None
    return _readback(store, github, repo, pr["number"], by, request_id)


def _adopt(store, github, repo, by, request_id):
    """The done phase replayed: read back the merged setup PR the grant names, and never merge again."""
    row = store.setup_merge(repo)
    branch = BOOTSTRAP_PREFIX + row["fingerprint"][:16] if row else None
    merged = [pr for pr in (github.pulls_for_branch(repo, branch) if branch else [])
              if _setup_repo_head(pr).lower() == repo.lower()
              and (pr.get("merged_at") or pr.get("merged"))]
    if len(merged) != 1:
        raise ToolError(f"the merge {request_id} of {repo} is journalled done, but the repo shows "
                        f"{len(merged)} merged setup PRs for its approved branch; a person inspects "
                        "GitHub and the store stays as it is")
    return _readback(store, github, repo, merged[0]["number"], by, request_id, already=True)


def _readback(store, github, repo, number, by, request_id, already=False):
    """Confirm the merged PR, record it with its merge commit, and say whether the repo checks pass."""
    pr = github.pull(repo, number)
    if not (pr.get("merged_at") or pr.get("merged")):
        raise ToolError(f"the setup PR #{number} of {repo} merged but reads back unmerged; a person "
                        "looks at GitHub before anything proceeds")
    sha = pr.get("merge_commit_sha")
    if not sha:
        raise ToolError(f"the merged setup PR #{number} of {repo} names no merge commit; the "
                        "merge is read back, never claimed without it")
    if store.request(request_id)["phase"] != "done":
        store.advance_request(request_id, "done")
    store.log("setup_pr_merged", {"repo": repo, "requestId": request_id, "by": by,
                                  "prNumber": number, "headSha": pr["head"]["sha"], "mergeSha": sha})
    checks = repos.check(github, repo)
    return {"repo": repo, "prNumber": number, "mergeSha": sha, "alreadyMerged": already,
            "ready": all(item["ok"] for item in checks), "checks": checks}
