"""The factory tools that Day Desk (Hermes) and the factory CLI call. Each one declares its effect."""
from datetime import datetime, timezone

from . import docker, lessons, metrics, repos, runtime, worker_ops
from .foreman import MAX_ALLOWANCE, MAX_PLAN_ISSUES, RECOVERY_ACTIONS, Foreman, agent_may_resolve
from .github import GitHubError
from .mcp import Tool, ToolError

# What Day Desk may call when it drives the factory with FACTORY_AGENT_TOKEN (ADR-0030). Setting up
# a repo, its gate secrets, and the lessons workers read stay with the owner's token.
AGENT_TOOLS = frozenset({"factory_runtime_status", "factory_status", "factory_repo_check", "factory_lessons",
                         "factory_report", "factory_check_worker", "factory_reload_workers",
                         "factory_approve_issue", "factory_propose_issue", "factory_start_plan",
                         "factory_set_dependencies", "factory_replace_issue", "factory_resolve_human",
                         "factory_answer_question",
                         "factory_use_allowance", "factory_queue_retry", "factory_merge_setup_pr",
                         "factory_docker_status", "factory_docker_control",
                         "factory_set_worker", "factory_register_worker", "factory_set_timeout"})
WAITING = ("human_waiting", "blocked")


def last_allowance_use(history):
    """The WorkItem's latest plan-allowance spend from the audit: who authorized it (the approval is
    a store fact), what evidence the decision carried and what it cost, with no secret data."""
    spends = [h for h in history if h["action"] == "allowance_spent"]
    if not spends:
        return None
    at, detail = spends[-1]["at"], spends[-1]["detail"]
    return {"at": at, "planId": detail.get("planId"), "action": detail.get("action"),
            "evidence": detail.get("evidence"), "remaining": detail.get("remaining")}


def _gate_pins(foreman):
    """Each repo's pr-gatekeeper revision pin (its gatekeeper_ref, ADR-0080), as the weekly report
    stamps it on every gauge (ADR-0083), so two windows under different pins never read as one
    trend. Best effort: a repo the factory cannot read yields "unknown", never a failed report."""
    pins = {}
    for repo in sorted(foreman.config.repos):
        try:
            text = foreman.github.file(repo, ".github/workflows/pr-gatekeeper.yml")
            pins[repo] = (repos._gate_pins(text) or {}).get("refSha") or "unknown"
        except GitHubError:
            pins[repo] = "unknown"
    return pins


def build(store, foreman, agent=False):
    runtime_schemas = []

    def summary_of(wi):
        row = {key: wi.get(key) for key in ("id", "state", "prNumber", "blockedOn", "waitingOn",
                                            "pendingRetry", "headSha", "gateRound", "worker",
                                            "updatedAt")}
        if wi["state"] in WAITING:
            row["agentMayResolve"] = agent_may_resolve(wi, store.history(wi["id"]))
        elif wi["state"] == "exhausted":
            row["blockedOn"] = Foreman._reason(wi)
        question = wi.get("question") or {}
        if (wi["state"] in ("dispatching", "implementing") and question.get("text")
                and not question.get("answer")):
            row["question"] = question
        return row

    def status(args):
        workitem_id = args.get("id")
        if workitem_id is None:
            workitems = store.all()
            summary = {"workItems": [summary_of(wi) for wi in workitems],
                       "autonomy": metrics.autonomy(workitems, store.history),
                       "repos": sorted(foreman.config.repos),
                       "planAllowances": store.allowances()  # identity, scope and remaining, no secrets
                       }
            if foreman.config.lessons_file:
                try:
                    summary["lessonEffect"] = metrics.lesson_effect(
                        lessons.load(foreman.config.lessons_file), store.all_workorders)
                except (ValueError, OSError) as exc:  # a hand-edited or unreadable file: say why
                    summary["lessonsError"] = str(exc)
            return summary
        wi = store.get(workitem_id)
        if wi is None:
            raise ToolError(f"No WorkItem {workitem_id}")
        history = store.history(workitem_id)
        return {"workItem": wi, "escalation": metrics.escalation(wi, history),
                "passes": len(store.workorders(workitem_id)), "history": history,
                "allowanceUse": last_allowance_use(history)}

    def set_timeout(args):
        if set(args) != {"id", "wallMs", "by"}:
            raise ToolError("factory_set_timeout requires exactly id, wallMs and by")
        return {"workItem": foreman.set_timeout(args["id"], args["wallMs"], args["by"], agent=agent)}

    def repo_check(args):
        repo = args["repo"]
        if not repos.NAME.match(repo):
            raise ToolError(f"{repo!r} is not a repo name in the form owner/repo")
        checks = repos.check(foreman.github, repo)
        return {"repo": repo, "enabled": foreman.config.listed(repo) is not None,
                "ready": all(c["ok"] for c in checks), "checks": checks,
                "setupMergeAuthorized": foreman.store.setup_merge(repo) is not None}

    def show_lessons(args):
        repo = args["repo"]
        if foreman.config.listed(repo) is None:
            raise ToolError(f"{repo} is not in FACTORY_REPOS or repos.json")
        repo = foreman.config.listed(repo)
        if not foreman.config.lessons_file:
            raise ToolError("FACTORY_LESSONS_FILE is not set, so the factory keeps no lessons")
        try:
            approved = lessons.for_repo(lessons.load(foreman.config.lessons_file), repo)
        except (ValueError, OSError) as exc:  # OSError: a file that exists but cannot be read
            raise ToolError(str(exc)) from None
        return {"repo": repo, "candidates": lessons.candidates(store, repo), "lessons": approved}

    def report(_args):
        try:
            approved = lessons.load(foreman.config.lessons_file)
        except (ValueError, OSError) as exc:  # a broken or unreadable lessons.json: say so and fail
            raise ToolError(str(exc)) from None
        candidates = [dict(candidate, repo=repo) for repo in sorted(foreman.config.repos)
                      for candidate in lessons.candidates(store, repo)]
        return metrics.weekly(store.all(), store.history, candidates,
                              metrics.lesson_effect(approved, store.all_workorders),
                              datetime.now(timezone.utc), store.all_workorders, _gate_pins(foreman))

    tools = [
        Tool("factory_docker_status", "Check whether the host Docker engine is running.",
             {"type": "object", "additionalProperties": False}, "read", lambda args: docker.status()),
        Tool("factory_docker_control",
             "Start host Docker (OrbStack) and verify readiness, or stop it only when no factory work "
             "is queued/running and no containers are running. Does not retry issues.",
             {"type": "object", "required": ["action"], "additionalProperties": False,
              "properties": {"action": {"type": "string", "enum": ["start", "stop"]}}}, "write",
             lambda args: docker.control(args["action"], store, foreman.runtime_lock)),
        Tool("factory_runtime_status",
             "Read sanitized live runtime facts: revision, workers/model/image, credential-file "
             "availability, service health, effective timeout and schema-cache differences. With id, "
             "probe exact-head GitHub read permissions through the factory identity. Never tests a "
             "provider, starts work, or reveals credentials/environment values.",
             {"type": "object", "properties": {"id": {"type": "string", "minLength": 1}},
              "additionalProperties": False}, "read", lambda args: runtime.report(
                  foreman, runtime_schemas, args.get("id"))),
        Tool("factory_status",
             "Show one WorkItem with its history, or, when no id is given, a summary of all "
             "WorkItems, their current PR/head/gate round/worker, and the repos the factory works on.",
             {"type": "object", "properties": {"id": {"type": "string", "description": "owner/repo#issue"}},
              "additionalProperties": False},
             "read", status),
        Tool("factory_repo_check",
             "Check what a GitHub repo needs before the factory can work on it: the factory's token "
             "can push to it, pr-gatekeeper is installed with a verify command, and its AGENTS.md "
             "carries the factory standard. Each failed check says how to fix it. Changes nothing.",
             {"type": "object", "required": ["repo"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"}}},
             "read", repo_check),
        Tool("factory_add_repo",
             "Enable a repo for the factory once a person agrees. It must pass factory_repo_check. "
             "Clones the worker checkout, creates the factory labels and adds the repo to repos.json; "
             "the factory works on it at once. Idempotent.",
             {"type": "object", "required": ["repo", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "by": {"type": "string", "minLength": 1, "description": "who agreed"}}},
             "write", lambda args: foreman.add_repo(args["repo"], args["by"])),
        Tool("factory_bootstrap_repo",
             "Prepare an owner-reviewed setup PR containing only missing gate files and the "
             "factory-standard block. An installed pr-gatekeeper caller whose gate revision pins "
             "are older than the approved revision is upgraded on the same PR, changing only those "
             "two pins and leaving its verify command, permissions, secrets and unrelated files as "
             "they are. Never merges or enables the repo. Requires a verify command "
             "when the gate config is missing. An empty repo first gets one commit with only a "
             "README.md on its default branch; the result names it as initialCommit. An open setup "
             "PR is adopted; when its files differ from the plan, a commit is added to it (updatedSha), "
             "and whenever the files or secret statuses it names change, its body is rewritten. "
             "Also sets the "
             "repo's missing gate Actions secrets from the owner's Keychain and never overwrites "
             "one; `secrets` gives each name and status (set, already set, missing, failed), "
             "never a value.",
             {"type": "object", "required": ["repo", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "verify": {"type": "string", "description": "repo test command, if gate config is missing"},
                             "by": {"type": "string", "minLength": 1, "description": "who approved the setup PR"}}},
             "write", lambda args: foreman.bootstrap_repo(args["repo"], args.get("verify"), args["by"])),
        Tool("factory_authorize_setup_merge",
             "An owner's explicit grant that this repo's one canonical setup PR may merge without a "
             "person (ADR-0074): it pins a digest of exactly what a person would review — the "
             "missing gate files and factory-standard block, plus the verify command when the gate "
             "config is missing. One authorization per repo; a re-approval for a changed plan "
             "replaces it, every change lands in the audit, and factory_revoke_setup_merge takes it "
             "back. It merges nothing. Repos without it still merge their setup PR by hand.",
             {"type": "object", "required": ["repo", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "by": {"type": "string", "minLength": 1, "description": "who grants it"},
                             "verify": {"type": "string", "minLength": 1,
                                        "description": "repo test command, when the repo lacks .github/pr-gatekeeper.json"}}},
             "write", lambda args: foreman.authorize_setup_merge(args["repo"], args.get("verify"), args["by"])),
        Tool("factory_revoke_setup_merge",
             "An owner's no: takes the setup-merge authorization for a repo away, so Day Desk can "
             "no longer merge its setup PR. Auditable, and idempotent only in that revoking an "
             "absent authorization is refused.",
             {"type": "object", "required": ["repo", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "by": {"type": "string", "minLength": 1, "description": "who revokes it"}}},
             "write", lambda args: foreman.revoke_setup_merge(args["repo"], args["by"])),
        Tool("factory_merge_setup_pr",
             "Day Desk's guarded merge of a repo's owner-authorized canonical setup PR (ADR-0074). "
             "Refuses unless the owner granted it, the open setup PR matches the approved setup "
             "exactly (paths, per-file content, default-branch base, live head), and the gate's "
             "pr-gatekeeper check on the head succeeded with no blocking verdict; GitHub's own "
             "branch protection still applies at the merge. Journalled by requestId: an "
             "interrupted or ambiguous merge is re-run and reconciles the same PR, reads the "
             "merged commit back and reports whether the repo's checks pass now, so nothing is "
             "dispatched against an incomplete setup. Merges only that one PR.",
             {"type": "object", "required": ["repo", "by", "requestId"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "by": {"type": "string", "minLength": 1,
                                    "description": "who answers for this merge in the audit"},
                             "requestId": {"type": "string", "minLength": 1,
                                           "description": "stable ID for the request; every retry carries it"}}},
             "write", lambda args: foreman.merge_setup_pr(args["repo"], args["by"], args["requestId"])),
        Tool("factory_intake",
             "Create a WorkItem from an open GitHub issue in an allowlisted repo. The issue needs an "
             "'## Acceptance' list of '- <claim> — done when: <check>' bullets. Idempotent.",
             {"type": "object", "required": ["repo", "issue"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "issue": {"type": "integer", "minimum": 1}}},
             "write", lambda args: {"workItem": foreman.intake(args["repo"], args["issue"])}),
        Tool("factory_approve_issue",
             "A person's go on an issue: label it factory:ready and take it in, so the factory starts "
             "work on it. The issue must be open, opened by a trusted author, and have an "
             "'## Acceptance' list of '- <claim> — done when: <check>' bullets. Idempotent.",
             {"type": "object", "required": ["repo", "issue", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "issue": {"type": "integer", "minimum": 1},
                             "by": {"type": "string", "minLength": 1, "description": "who approved"}}},
             "write", lambda args: {"workItem": foreman.approve_issue(args["repo"], args["issue"], args["by"])}),
        Tool("factory_tick",
             "Reconcile one WorkItem against GitHub and take the one legal next action. Without an "
             "id, first adopt open 'factory:ready' issues, then tick every live WorkItem. Idempotent.",
             {"type": "object", "properties": {"id": {"type": "string", "description": "owner/repo#issue"}},
              "additionalProperties": False},
             "write", lambda args: foreman.tick(args.get("id"))),
        Tool("factory_resolve_human",
             "Record a person's decision on a WorkItem in human_waiting, blocked or exhausted: retry (the "
             "factory tries again, with the note as a finding; the owner's retry buys a fresh budget of "
             "passes, Day Desk's does not), regate (back to the gate: a fix the person pushed, or the same "
             "head when the gate failed itself) or close. ownerAsked says the owner asked for this retry, "
             "regate or close in this chat: then Day Desk may answer a WorkItem it already answered once, "
             "and a retry may be of an exhausted or spent one and buys a fresh budget.",
             {"type": "object", "required": ["id", "resolution", "note", "by"],
              "additionalProperties": False,
              "properties": {"id": {"type": "string"},
                             "resolution": {"enum": ["retry", "regate", "close"]},
                             "note": {"type": "string", "minLength": 1},
                             "by": {"type": "string", "minLength": 1, "description": "who decided"},
                             "ownerAsked": {"type": "boolean", "description":
                                            "Day Desk only: the owner asked for this retry, regate or close "
                                            "in this chat, and the note quotes the owner's words"}}},
             "write", lambda args: {"workItem": foreman.resolve_human(
                 args["id"], args["resolution"], args["note"], args["by"], agent=agent,
                 owner_asked=args.get("ownerAsked", False))}),
        Tool("factory_answer_question",
             "Answer the question a running worker pass asked (ADR-0098). The worker is waiting on "
             "the answer; it reaches the pass and stays on the WorkItem. Answer from the issue, the "
             "plan and the owner's words. When the question is a real boundary (a scope change, a "
             "credential, a new pass budget), do not decide it: answer that the worker must leave it "
             "and report.",
             {"type": "object", "required": ["id", "questionId", "answer", "by"],
              "additionalProperties": False,
              "properties": {"id": {"type": "string"},
                             "questionId": {"type": "string", "minLength": 1,
                                            "description": "the question's id, from factory_status"},
                             "answer": {"type": "string", "minLength": 1,
                                        "description": "what the worker should do, in its terms"},
                             "by": {"type": "string", "minLength": 1,
                                    "description": "who answered: day-desk"}}},
             "write", lambda args: {"workItem": foreman.answer_question(
                 args["id"], args["questionId"], args["answer"], args["by"])}),
        Tool("factory_propose_issue",
             "Planner only: open an issue labelled factory:proposed. The body needs an "
             "'## Acceptance' list of '- <claim> — done when: <check>' bullets. A person adds "
             "factory:ready to start it.",
             {"type": "object", "required": ["repo", "title", "body"], "additionalProperties": False,
              "properties": {"repo": {"type": "string"}, "title": {"type": "string", "minLength": 1},
                             "body": {"type": "string", "minLength": 1}}},
             "write", lambda args: foreman.propose_issue(args["repo"], args["title"], args["body"])),
        Tool("factory_start_plan",
             "A person's go on a whole plan. Opens the issues in the given order, each labelled "
             "factory:ready. Each issue after the first gets the line 'Depends on #<previous>', and "
             "the factory starts it only after the previous issue merges. An issue may carry "
             "`after`: the 1-based positions of the earlier issues it waits for instead, each a "
             "whole number before it and not repeated; omitted keeps the chain, and `after: []` "
             "lets it start at once. First checks every issue: "
             f"1 to {MAX_PLAN_ISSUES} issues, each body with an '## Acceptance' list of "
             "'- <claim> — done when: <check>' bullets, and no title or body that names .github/ or "
             "AGENTS.md. If one check fails, opens no issue. A second call opens the issues again.",
             {"type": "object", "required": ["repo", "issues", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "issues": {"type": "array", "minItems": 1, "maxItems": MAX_PLAN_ISSUES,
                                        "description": "in the order they run",
                                        "items": {"type": "object", "required": ["title", "body"],
                                                  "additionalProperties": False,
                                                  "properties": {"title": {"type": "string", "minLength": 1},
                                                                 "body": {"type": "string", "minLength": 1},
                                                                 "after": {"type": "array",
                                                                           "description":
                                                                               "1-based positions of the earlier "
                                                                               "issues this one waits for; "
                                                                               "omitted keeps the chain, [] "
                                                                               "starts at once",
                                                                           "items": {"type": "integer",
                                                                                     "minimum": 1}}}}},
                             "by": {"type": "string", "minLength": 1, "description": "who approved the plan"}}},
             "write", lambda args: foreman.start_plan(args["repo"], args["issues"], args["by"])),
        Tool("factory_set_dependencies",
             "Change which issues a WorkItem waits for, before any worker has touched it: replaces the "
             "'Depends on #n' lines of its issue with the issue numbers given, and [] lets it start at "
             "once. Use it so independent issues of a plan run side by side. A WorkItem in human_waiting "
             "because a dependency ended has no pass, so its order may change too: fix it, then answer it "
             "with factory_resolve_human retry. Refused when the WorkItem has had a worker pass, when a "
             "number is itself, a pull request, missing, closed without a merged PR, cancelled or "
             "exhausted, or when the change would make a cycle.",
             {"type": "object", "required": ["id", "dependsOn", "by"], "additionalProperties": False,
              "properties": {"id": {"type": "string", "description": "owner/repo#issue"},
                             "dependsOn": {"type": "array", "maxItems": MAX_PLAN_ISSUES,
                                           "description": "issue numbers in the same repo; [] for none",
                                           "items": {"type": "integer", "minimum": 1}},
                             "by": {"type": "string", "minLength": 1, "description": "who asked"}}},
             "write", lambda args: {"workItem": foreman.set_dependencies(
                 args["id"], args["dependsOn"], args["by"], agent=agent)}),
        Tool("factory_set_timeout",
             "Set a WorkItem's wall timeout between worker passes, on request. wallMs is 60000 to "
             "3600000 milliseconds and at least FACTORY_WALL_MS, or null to restore that default. "
             "Checks the live relay cap. "
             "Refuses live workers and terminal items. Never retries, dispatches, resets passes or "
             "grants a recovery allowance. Read back factory_status before any separately approved retry.",
             {"type": "object", "required": ["id", "wallMs", "by"], "additionalProperties": False,
              "properties": {"id": {"type": "string", "minLength": 1},
                             "wallMs": {"type": ["integer", "null"], "minimum": 60000,
                                        "maximum": 3600000},
                             "by": {"type": "string", "minLength": 1}}},
             "write", set_timeout),
        Tool("factory_set_worker",
             "Choose which sandboxed worker (and so which pinned model) runs a WorkItem: replaces its "
             "issue's 'Worker: <id>' lines with the agent ids given, and [] falls back to "
             "FACTORY_WORKERS order. Only agents in FACTORY_WORKERS may be named; an unknown id is "
             "refused before the edit. Use it to steer work to a model on request. Refused while a "
             "worker is running or on a closed, cancelled or exhausted WorkItem.",
             {"type": "object", "required": ["id", "workers", "by"], "additionalProperties": False,
              "properties": {"id": {"type": "string", "description": "owner/repo#issue"},
                             "workers": {"type": "array",
                                         "description": "relay agent ids from FACTORY_WORKERS, in "
                                                        "order; [] for the default order",
                                         "items": {"type": "string"}},
                             "by": {"type": "string", "minLength": 1, "description": "who asked"}}},
             "write", lambda args: {"workItem": foreman.set_worker(
                 args["id"], args["workers"], args["by"], agent=agent)}),
        Tool("factory_register_worker",
             "Register a new sandboxed factory worker in the relay registry: it copies an approved "
             "sandboxed template's command, credential mount, env, cwd, allowedRoots, image and "
             "timeout, and pins only the model given, under the template's own provider. "
             "`template` picks which approved sandboxed template to copy (default "
             "FACTORY_WORKER_TEMPLATE); an unsandboxed, unknown or cross-provider template is "
             "refused before anything is written. It is NOT added to FACTORY_WORKERS, so it runs "
             "nothing until the owner lists it there and its provider-health pilot is green. The "
             "registry is the relay's agents.json; the relay is reloaded when it answers. Returns "
             "the new agent and whether the running relay reloaded it.",
             {"type": "object", "required": ["worker", "model", "by"], "additionalProperties": False,
              "properties": {"worker": {"type": "string", "description": "new relay agent id"},
                             "model": {"type": "string", "minLength": 1,
                                       "description": "the model this worker pins, bare or as "
                                                      "provider/model under the template's provider"},
                             "template": {"type": "string", "minLength": 1,
                                          "description": "approved sandboxed template agent id to "
                                                         "copy; defaults to FACTORY_WORKER_TEMPLATE"},
                             "by": {"type": "string", "minLength": 1, "description": "who asked"}}},
             "write", lambda args: foreman.register_worker(
                 args["worker"], args["model"], args["by"], template=args.get("template"),
                 agent=agent)),
        Tool("factory_check_worker",
             "Run one bounded synthetic provider-health check for a registered sandboxed worker. "
             "Uses only the worker's existing pinned model, credential mounts, safe environment, "
             "image and supported harness. Stores sanitized health or quarantine status for 15 minutes. "
             "Never starts a WorkOrder or activates the worker.",
             {"type": "object", "required": ["worker", "by"], "additionalProperties": False,
              "properties": {"worker": {"type": "string", "minLength": 1},
                             "by": {"type": "string", "minLength": 1}}}, "write",
             lambda args: worker_ops.check(store, foreman.config,
                                           args["worker"], args["by"], agent=agent)),
        Tool("factory_reload_workers",
             "Ask the running relay to reload its existing worker registry through its fixed local "
             "reload endpoint. Does not edit the registry, change FACTORY_WORKERS, start a task or "
             "retry work.",
             {"type": "object", "required": ["by"], "additionalProperties": False,
              "properties": {"by": {"type": "string", "minLength": 1}}}, "write",
             lambda args: worker_ops.reload(store, foreman.relay, args["by"], agent=agent)),
        Tool("factory_replace_issue",
             "The owner's replacement of a WorkItem that is waiting on recovery (human_waiting or "
             "blocked) with ordered "
             "replacement issues of the same repo: the first inherits the original's "
             "upstream dependencies, each next one waits for the one before it, and every direct "
             "dependent of the original now waits for the last replacement, keeping its other "
             "dependencies. The whole graph is planned and validated before anything is written: a "
             "replacement or dependent that had a worker pass, an original whose work is still live "
             "(an active WorkOrder or relay task; failed attempts with nothing running on it are fine), "
             "or a plan that would make a cycle, "
             "refuses with nothing "
             "changed. A planned replacement is one the factory opens itself: a factory:proposed issue "
             "(never factory:ready), carrying its own identity marker; a repeat of the same call "
             "verifies the marker and the wording and adopts the issue already created, so an "
             "interrupted create never opens a duplicate. Otherwise the changed issues are edited, "
             "the original is cancelled and closed "
             "keeping what it tried, "
             "and the replacement is journalled so a retry resumes where it stopped.",
             {"type": "object", "required": ["id", "replacements", "by", "note", "requestId"],
              "additionalProperties": False,
              "properties": {"id": {"type": "string", "description": "owner/repo#issue"},
                             "replacements": {"type": "array", "minItems": 1, "maxItems": MAX_PLAN_ISSUES,
                                              "description": "issue numbers in the same repo, in the order they "
                                                             "run, or title-and-body plan items the factory opens "
                                                             "as factory:proposed",
                                              "items": {"anyOf": [
                                                  {"type": "integer", "minimum": 1},
                                                  {"type": "object", "required": ["title", "body"],
                                                   "additionalProperties": False,
                                                   "properties": {"title": {"type": "string", "minLength": 1},
                                                                  "body": {"type": "string", "minLength": 1}}}]}},
                             "by": {"type": "string", "minLength": 1, "description": "who asked"},
                             "note": {"type": "string", "minLength": 1, "description":
                                      "the owner's approval: Day Desk quotes the owner's words in chat "
                                      "approving this replacement, and the owner's own call says what "
                                      "they approved"},
                             "requestId": {"type": "string", "minLength": 1, "description":
                                           "a stable ID for this replacement request: every repeat of "
                                           "the same call, retries included, carries it, so the journal "
                                           "replays; a different requestId for the same WorkItem "
                                           "conflicts and refuses"},
                             "ownerAsked": {"type": "boolean", "description":
                                             "Day Desk only: the owner approved this replacement in chat "
                                             "and the note quotes their words; the owner's own call runs "
                                             "without it"},
                             "allowancePlanId": {"type": "string", "minLength": 1, "description":
                                                 "Day Desk only: replace inside the owner-approved plan "
                                                 "allowance (ADR-0069), spending one use; the scope is "
                                                 "open issues already in the repo"}}},
             "write", lambda args: {"workItem": foreman.replace_issue(
                 args["id"], args["replacements"], args["by"], args["note"], args["requestId"],
                 agent=agent, owner_asked=args.get("ownerAsked", False),
                 allowance_plan_id=args.get("allowancePlanId"))}),
        Tool("factory_approve_plan_allowance",
             "The owner's one approval of bounded recovery for a plan (ADR-0069): Day Desk may then "
             "spend up to `remaining` uses on the recovery actions named, without the owner answering "
             "each one. One approval per plan id; nothing unlimited. Safety stops (a HUMAN verdict, "
             "protected paths, access) and confirmed environment faults still need a person, and the "
             "per-item pass budget stays as it is.",
             {"type": "object", "required": ["planId", "actions", "remaining", "by", "repo"],
              "additionalProperties": False,
              "properties": {"planId": {"type": "string", "minLength": 1,
                                        "description": "the plan this allowance is bound to"},
                             "actions": {"type": "array", "minItems": 1, "uniqueItems": True,
                                         "items": {"enum": list(RECOVERY_ACTIONS)}},
                             "remaining": {"type": "integer", "minimum": 1, "maximum": MAX_ALLOWANCE},
                             "by": {"type": "string", "minLength": 1, "description": "who approved"},
                             "repo": {"type": "string", "minLength": 1, "description":
                                      "the repo the plan covers; no use spends outside it"},
                             "items": {"type": "array", "minItems": 1, "uniqueItems": True,
                                       "items": {"type": "string", "minLength": 1},
                                       "description": "the exact WorkItem members of the plan, like "
                                                      "[\"o/r#1\"]; omit for every WorkItem of repo"}}},
             "write", lambda args: foreman.approve_plan_allowance(
                 args["planId"], args["actions"], args["remaining"], args["by"], args["repo"],
                 args.get("items"))),
        Tool("factory_queue_retry",
             "Queue one approved Day Desk retry and read it back. This phase records a durable intent "
             "without spending the allowance or starting a worker; automatic dispatch arrives in a "
             "follow-up release. Repeating the same requestId is idempotent. A lease collision returns "
             "status retryable with reason workitem_busy; retry that same requestId.",
             {"type": "object", "required": ["id", "planId", "requestId", "by", "evidence"],
              "additionalProperties": False,
              "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 200},
                             "planId": {"type": "string", "minLength": 1, "maxLength": 128},
                             "requestId": {"type": "string", "minLength": 1, "maxLength": 128,
                                           "description": "stable id for this decision; reuse it on a retry"},
                             "by": {"type": "string", "minLength": 1, "maxLength": 100,
                                    "description": "Day Desk identifies itself as day-desk"},
                             "evidence": {"type": "string", "minLength": 1, "maxLength": 5000,
                                          "description": "bounded evidence, stored after sanitization"}}},
             "write", lambda args: foreman.queue_retry(args["id"], args["planId"], args["requestId"],
                                                       args["by"], args["evidence"], agent=agent)),
        Tool("factory_use_allowance",
             "Recover a waiting WorkItem immediately inside an owner-approved plan allowance "
             "(ADR-0069), without ownerAsked and without buying fresh passes. The owner's direct "
             "decision; Day Desk may also queue through factory_queue_retry, which stays durable "
             "across an interrupted chat. Refused when the escalation is a safety stop, when a "
             "confirmed environment fault is not repaired, when the action is outside the "
             "allowance's scope, or when the allowance is exhausted: then the owner decides.",
             {"type": "object", "required": ["id", "planId", "action", "note", "by"],
              "additionalProperties": False,
              "properties": {"id": {"type": "string"},
                             "planId": {"type": "string", "minLength": 1},
                             "action": {"enum": list(RECOVERY_ACTIONS)},
                             "note": {"type": "string", "minLength": 1, "description":
                                      "the decision's evidence: what the attempt showed and why this "
                                      "recovery is sound; it rides in the audit, sanitized"},
                             "by": {"type": "string", "minLength": 1}}},
             "write", lambda args: {"workItem": foreman.use_allowance(
                 args["id"], args["planId"], args["action"], args["note"], args["by"])}),
        Tool("factory_lessons",
             "Show what the factory learned about one repo: the blocking gate findings that "
             "recurred on two or more WorkItems (candidates), and the lessons the owner approved "
             "for its WorkOrders. Changes nothing.",
             {"type": "object", "required": ["repo"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"}}},
             "read", show_lessons),
        Tool("factory_report",
             "The weekly report's numbers for the 7 days to now: the autonomy rate of the "
             "WorkItems that ended in them and in the 7 days before, how many WorkItems went to a "
             "person or ran out of passes and the 3 most frequent reasons why, the lesson "
             "candidates that are new, and the lessons flagged as not working. First-pass gauges "
             "for the window: first-verdict MERGE rate, clean one-shot rate, rounds to merge, "
             "accepted Results by pass kind and worker family, the timeout share of failed passes "
             "and dependency-wait hours — each with its n, its IST window and the gate pin "
             "(ADR-0083). Changes nothing.",
             {"type": "object", "properties": {}, "additionalProperties": False},
             "read", report),
        Tool("factory_approve_lesson",
             "A person's go on one lesson for a repo: the factory passes it to this repo's future "
             "workers as a past gate finding to avoid. The fingerprint must be a candidate from "
             f"factory_lessons; the text is what the worker should read (capped at {lessons.MAX_TEXT} "
             "characters). Approving the same fingerprint again for the repo is refused.",
             {"type": "object", "required": ["repo", "fingerprint", "text", "by"],
              "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "fingerprint": {"type": "string", "minLength": 1},
                             "text": {"type": "string", "minLength": 1},
                             "by": {"type": "string", "minLength": 1, "description": "who approved"}}},
             "write", lambda args: foreman.approve_lesson(
                 args["repo"], args["fingerprint"], args["text"], args["by"])),
        Tool("factory_retire_lesson",
             "A person's no on one lesson: stop passing it to this repo's workers, for instance "
             "because it recurred again (factory_status shows it) or because the repo's own "
             "AGENTS.md now carries it. Refused when the fingerprint is not a lesson in the repo.",
             {"type": "object", "required": ["repo", "fingerprint", "by"], "additionalProperties": False,
              "properties": {"repo": {"type": "string", "description": "owner/repo"},
                             "fingerprint": {"type": "string", "minLength": 1},
                             "by": {"type": "string", "minLength": 1, "description": "who retired it"}}},
             "write", lambda args: foreman.retire_lesson(args["repo"], args["fingerprint"], args["by"])),
    ]
    runtime_schemas.extend(tool.listing() for tool in tools if not agent or tool.name in AGENT_TOOLS)
    return {tool.name: tool for tool in tools if not agent or tool.name in AGENT_TOOLS}
