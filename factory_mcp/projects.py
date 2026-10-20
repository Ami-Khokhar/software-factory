"""The Project contract's checks (ADR-0099): what the Lead's fields must hold to store one.

A Project is a named goal with one shared budget of worker passes. `problem` names the first limit
the Lead's fields break, in the order of schemas/project.schema.json, so a refusal says what to
fix; `new` builds the stored document from fields that pass.
"""
import re

MAX_PASSES = 60          # one Project's budget ceiling; the owner extends past it (WO-03)
MAX_MEMBER_PASSES = 8  # one member's share of a Project's budget
MAX_BLUEPRINT = 20000    # characters
MAX_DONE_WHEN = 5
PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{2,40}$")
STATES = ("active", "paused", "done", "stopped")

REPO = re.compile(r"^[^/]+/[^/]+$")


def _text(key, value, high):
    """What `value` as `key` breaks about its 1..high length, or None."""
    if not isinstance(value, str):
        return f"{key} must be text of 1 to {high} characters"
    if len(value) > high:
        return f"{key} is {len(value)} characters; the limit is {high}"
    if not value:
        return f"{key} must be 1 to {high} characters"
    return None


def _object(value, keys, name):
    if not isinstance(value, dict):
        return f"{name} must be {', '.join(keys)}"
    unknown = sorted(set(value) - set(keys))
    if unknown:
        return f"{name} carries keys other than {', '.join(keys)}: {', '.join(unknown)}"
    return None


def _done_when(field):
    """What `doneWhen` breaks about its checks, or None."""
    checks = field.get("doneWhen")
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_DONE_WHEN:
        return f"doneWhen must be 1 to {MAX_DONE_WHEN} checks"
    for n, check in enumerate(checks, 1):
        outer = _object(check, ("claim", "command"), f"doneWhen[{n}]")
        if outer:
            return outer
        for key, high in ("claim", 300), ("command", 500):
            reason = _text(f"doneWhen[{n}] {key}", check.get(key), high)
            if reason:
                return reason
    return None


def problem(fields):
    """The first reason `fields` cannot become a Project, as one sentence that says what to fix,
    or None. `fields` has the keys id, repo, title, goal, blueprint, doneWhen, budget, approval."""
    id_ = fields.get("id")
    if not isinstance(id_, str) or not PROJECT_ID.fullmatch(id_):
        return ("id must be 3 to 41 characters of lowercase letters, digits and dashes, "
                "starting with a letter or digit")
    if not isinstance(fields.get("repo"), str) or not REPO.match(fields["repo"]):
        return "repo must be in the form owner/repo"
    for key, high in ("title", 100), ("goal", 2000), ("blueprint", MAX_BLUEPRINT):
        reason = _text(key, fields.get(key), high)
        if reason:
            return reason
    reason = _done_when(fields)
    if reason:
        return reason
    budget = fields.get("budget")
    reason = _object(budget, ("passes", "deadline"), "budget")
    if reason:
        return reason
    passes = budget.get("passes")
    if isinstance(passes, bool) or not isinstance(passes, int) or not 1 <= passes <= MAX_PASSES:
        return f"budget passes must be 1 to {MAX_PASSES}"
    deadline = budget.get("deadline")
    if not isinstance(deadline, str) or not deadline:
        return "budget deadline must be a date-time"
    approval = fields.get("approval")
    reason = _object(approval, ("by", "words", "at"), "approval")
    if reason:
        return reason
    reason = _text("approval by", approval.get("by"), 100)
    if reason:
        return reason
    reason = _text("approval words", approval.get("words"), 500)
    if reason:
        return reason
    at = approval.get("at")
    if not isinstance(at, str) or not at:
        return "approval at must be a date-time"
    return None


def new(fields, at):
    """A Project document from checked `fields`: state active, passesUsed 0, members [],
    pausedReason None, acceptanceItem None, stateVersion 0, createdAt and updatedAt `at`."""
    reason = problem(fields)
    if reason:
        raise ValueError(f"a Project's fields cannot store one: {reason}")
    return {
        "schemaVersion": "factory.project/v1",
        "id": fields["id"],
        "repo": fields["repo"],
        "title": fields["title"],
        "goal": fields["goal"],
        "blueprint": fields["blueprint"],
        "doneWhen": [dict(check) for check in fields["doneWhen"]],
        "budget": dict(fields["budget"]),
        "passesUsed": 0,
        "state": "active",
        "pausedReason": None,
        "members": [],
        "acceptanceItem": None,
        "approval": dict(fields["approval"]),
        "stateVersion": 0,
        "createdAt": at,
        "updatedAt": at,
    }


def acceptance_issue(project_fields, count):
    """The last issue of a Project: run its own checks and commit the evidence (ADR-0105)."""
    path = f"docs/acceptance/{project_fields['id']}.md"
    body = (
        "Run the Project's end-to-end checks on the default branch and record the result.\n\n"
        "For each check below: run the command from the repository root. Write the command, "
        f"its exit code and the last 40 lines of its output to {path}. When a command fails, "
        "find the cause in the product code, fix it with a test that fails without the fix, "
        "and run the command again.\n\n"
        "Do not change a check to make it pass. Do not mock what the command runs.\n\n"
        "Paths: *\n\n"
        "## Acceptance\n"
    )
    for check in project_fields["doneWhen"]:
        body += (f"- {check['claim']} — done when: `{check['command']}` exits 0 "
                 f"and {path} shows its output\n")
    body += ("- The other checks of the repository still pass — done when: "
             "the repository's verify command passes\n")
    return {"title": f"Acceptance: {project_fields['title']}",
            "after": list(range(1, count + 1)), "body": body}
