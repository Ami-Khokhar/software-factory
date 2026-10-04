# Factory decisions

Each decision that constrains later work is a short ADR: context, decision,
consequence. A changed decision gets a new ADR that supersedes the old one.

---

## ADR-0001 — Agent registry has one home

**Context.** The repo checkout and the installed clone each carried a
machine-local `config/agents.json`, and `default_config_path()` silently fell
back to whichever ran. The same dispatch could resolve to a different worker
pool depending on cwd.

**Decision.** `~/.config/agent-relay/agents.json` is the single source of truth
for the agent registry. Checkout configs are fallbacks only, aligned to it.

**Consequence.** Dispatch is deterministic. Follow-up: make the missing-user-
registry case fail loud instead of silently falling back.

---

## ADR-0002 — One deliverable issue = one branch = one PR

**Context.** Gate limits are ≤600 changed lines and ≤20 files. Large goals must
be split, but arbitrary splits merge separately and may never form the feature.

**Decision.** The unit of work is one deliverable issue → one branch → one PR.
Larger goals are a parent goal (epic) with **child issues**, each independently
valid. Parent completion is checked only when all children are done.

**Consequence.** Retry, review, and completion identity stay unambiguous. An
epic is never one oversized PR.

---

## ADR-0003 — The controller opens the PR

**Superseded by ADR-0017** for what the Result carries: a patch, not a branch. The controller
still opens the PR.

**Context.** A worker may push a branch or open a PR and then time out before
returning its `Result`, so "worker failed" and "work exists" can both be true.

**Decision.** The worker returns a `Result` (branch + commit + evidence); the
**controller** validates it and opens the PR. If a PR already exists on the
branch, the controller reconciles instead of opening a duplicate.

**Consequence.** There is one acceptance point for the worker's artifact, and a
timed-out worker cannot be both failed and complete.

---

## ADR-0004 — Gatekeeper is the only routine merger

**Context.** Workers must not approve their own work; but HUMAN needs a route to
completion.

**Decision.** Only pr-gatekeeper performs routine merges. A human resolves
`HUMAN` through an explicit, audited action, after which the gate **re-runs** on
the resulting head. Any emergency manual merge is exceptional and logged.

**Consequence.** One normal merge policy, one explicit recovery path.

---

## ADR-0005 — Round counting is per unresolved problem

**Context.** On the real `agent-relay` repo every PR died at round 4 (`HUMAN`)
because a strict per-PR counter counted fresh minor findings as new rounds. The
budget capped spend but never produced convergence.

**Decision.** Count attempts at the **same unresolved problem** (identified by a
stable `blocker.fingerprint` = hash of path + normalized problem text), not
total gate rounds. A hard gate-round ceiling still exists as a safety net, but
duplicate re-reporting of one problem does not consume the attempt budget for
unrelated problems. Only **blocking** findings force another round; minor
findings are recorded and do not.

**Consequence.** A PR making genuine progress keeps going; a PR stuck on one
problem escalates to a human. This is the fix for the convergence failure.

---

## ADR-0006 — HUMAN is a queue, not a terminal state

**Context.** `HUMAN` was modeled as terminal, so escalated work had no owner and
no way back.

**Decision.** `human_waiting` is a durable queue entry with a **named owner**, a
reason, and allowed resolutions: request changes (→ `ready`), or close (→
`closed`). It is not terminal.

**Consequence.** Nothing strands. A label plus an `@mention` is a notification,
not ownership.

---

## ADR-0007 — Workers are least-privilege; untrusted input is data

**Superseded by ADR-0017** for push access: a worker holds no GitHub credential, and factory-mcp
pushes. Untrusted input is still data.

**Context.** A worker needs enough access to push a branch, but issue text, PR
comments, code, and model findings are all untrusted input into an agent with
repository access.

**Decision.** Grant workers only the repository permissions needed to produce
their assigned artifact — never merge or admin. Treat issue bodies, comments,
findings, and diffs as **data, not instructions**. Validate WorkOrder scope and
changed paths before opening the PR. Risk classes — **secrets, payments,
security, data deletion, or major design choices** — force `HUMAN` before merge,
independent of path-based protected lists.

**Consequence.** Prompt injection cannot escalate role or permission. Protected
categories are risk-based, not just path-based.

---

## ADR-0008 — Branch protection is the independent backstop

**Context.** pr-gatekeeper both judges and merges, so a failure inside it is a
single point of failure.

**Decision.** Enable branch protection requiring the gate checks to pass, so a
merge is blocked if the gate is bypassed, crashed, or fooled.

**Consequence.** Two independent mechanisms must both pass to merge.

---

## ADR-0009 — Reconcile over events

**Context.** `repository_dispatch` can be delayed, duplicated, or arrive after a
newer push. A design that treats events as commands acts on stale state.

**Decision.** GitHub is authoritative for facts. Events are wake-up hints; a
periodic reconcile (on startup and on a schedule) computes the true state. Every
transition is guarded by validating `repo + prNumber + headSha + round` against
the WorkItem.

**Consequence.** Duplicate and stale events are harmless. Recovery is eventual
and does not depend on delivery.

---

## ADR-0010 — OpenBot is the control plane; decisions stay deterministic

**Superseded by ADR-0013** for the front door. Decisions stay deterministic; ADR-0030 lets Day Desk make
the calls the owner made, within guards in code.

**Context.** The factory needs durable goals, governance, an audit trail, human-in-the-loop
channels and schedules. OpenBot has all of these (Jobs, CEL policy, audit, Telegram, routines).
It is also an LLM agent platform, and a model must not decide merges or state transitions.

**Decision.** OpenBot holds the goal (one Job per WorkItem), runs the Foreman and Planner Bots,
and carries escalations to a human. Every state transition and every side effect is decided by
deterministic code in `factory-mcp`. The Bots can only call `factory-mcp` tools, and the policy
denies everything else.

**Consequence.** An OpenBot or model failure can stall a job but cannot cause a wrong merge or
skip a guard.

---

## ADR-0011 — No Anthropic dependency; providers are swappable

**Context.** The Claude subscription ends 2026-09-28. The factory must keep running after that
date, and any single provider can fail.

**Decision.** Nothing in the factory depends on Anthropic. Models and workers come from OpenCode
Go, OpenAI and local providers. Workers are routed by locality and risk, and a provider that
fails is skipped on the next attempt.

**Consequence.** Any harness (Codex, pi, OpenCode) can continue the work, and one provider's
outage degrades throughput instead of stopping the line.

---

## ADR-0012 — The Foreman is `factory-mcp`

**Context.** OpenBot's MCP client speaks Streamable HTTP only; agent-relay's MCP server is stdio
only. Keeping WorkItem state in OpenBot's Postgres would mean forking an alpha codebase.

**Decision.** `factory-mcp` is a Streamable-HTTP MCP server in this repo, written with the
Python standard library. It owns the WorkItem state machine and its SQLite store, calls
agent-relay over HTTP and GitHub over REST. The store is rebuildable from GitHub (issue labels,
branch `factory/<issue>`, PR, verdict marker). There is no merge tool.

**Consequence.** OpenBot stays unmodified except for the tenant package. Losing the store loses
history, not correctness.

---

## ADR-0013 — Hermes is the front door

**Superseded by ADR-0021** for the shell, **by ADR-0030** for the owner's yes on each write, and **by ADR-0041** for the other toolsets.

**Context.** OpenBot gave the factory two Bots, a CEL policy and a 15-minute routine that ran a
model to tick WorkItems, which factory-mcp's own background tick already does. A person's yes was
a `confirm` flag the model set itself.

**Decision.** One Hermes agent, Day Desk, is the front door, in Telegram. It is the profile
distribution in `tenant/hermes/`. factory-mcp is its MCP server with `trust: untrusted`, so Hermes
asks the owner before each tool call not marked read-only, and no answer is a no. Day Desk sees
the tools a person uses, not `factory_tick` or `factory_intake`. A no-agent cron job,
`factory watch`, reports each new escalation once and runs no model. factory-mcp still decides
every transition and side effect (ADR-0010).

**Consequence.** The owner's yes is Hermes's approval, not the model's word. Day Desk has no
shell, file or subagent tools: they would run as the owner with no approval, and could read the
factory token and call any tool directly. The worker sandbox and the gate stay the boundaries
(ADR-0007, ADR-0004). OpenBot keeps running until the trial in docs/RUNBOOK.md
passes, and #37 then retires it.

---

## ADR-0014 — The allowlist is `FACTORY_REPOS` plus `repos.json`

**Superseded by ADR-0027** for how a repo joins. `repos.json` still records it.

**Context.** The allowlist lived only in `FACTORY_REPOS`, and `factory add-repo` edited the env
file and restarted factory-mcp. A tool that enables a repo from chat cannot restart the process
that serves it, and an allowlist the factory extends itself should be a file a person can read.

**Decision.** `repos.json` (`FACTORY_REPOS_FILE`; `factory serve` uses
`~/.config/factory-mcp/repos.json`) lists the repos a tool enabled, with who enabled each and when.
The allowlist is `FACTORY_REPOS` plus that file. factory-mcp reads it at start, and the tool that
adds a repo also adds it to the running process. JSON, because factory-mcp uses only the standard
library. Per-repo workers, trusted authors and protected paths stay out until something needs
them; a repo's protected paths already live in its own `.github/pr-gatekeeper.json`.

**Consequence.** Enabling a repo needs no restart and leaves a record a person can review. Repos
already in `FACTORY_REPOS` keep working. Taking a repo away is an edit and a restart, and its
WorkItems wait untouched until it comes back.

---

## ADR-0015 — One written standard for the factory and the repos it builds

**Context.** This repo's AGENTS.md described only the gate's pull request protocol. People and
coding agents working here, and the factory's workers in other repos, had no written rules for
planning, building, testing or documenting, so quality depended on each one's habits. The audit in
#25 found fields that nothing read, tests that could not fail, and finished plans kept as docs.

**Decision.** The standard is written once, in `templates/AGENTS.md`, between `factory-standard`
markers. Each factory repo's AGENTS.md carries the block unchanged, around the repo's own layout,
commands and rules. This repo's AGENTS.md carries it too, and a test keeps the two identical. The
factory applies it: it enables only repos whose AGENTS.md carries the block, and it tells its
workers to follow it (#34).

**Consequence.** One place improves how code is written, for people, coding agents and workers
alike. Changing the standard means changing the template, then copying the block into each repo;
AGENTS.md is a protected path, so a person merges each copy.

---

## ADR-0016 — Freebuff is the factory's coding worker

**Superseded by ADR-0026.**

**Context.** The owner wants GLM 5.3 Flash through Freebuff for worker passes. Freebuff 0.0.203
has a terminal UI but no noninteractive task/result command, while agent-relay needs a bounded
command and the factory needs a trustworthy Result.

**Decision.** The relay runs `worker/runner.py` with `worker/freebuff.py` as its only configured
coding harness. The runner gives a disposable container a fresh repository copy and a private
copy of Freebuff's login credential. The adapter selects GLM 5.3 Flash, sends the WorkOrder through
Freebuff's terminal UI, and accepts completion only when the new session log records its request
and `Main prompt finished`. The runner then collects the patch and runs the base commit's verify
command. A changed UI or missing completion marker fails the pass.

**Consequence.** The model provider and UI remain outside the deterministic Foreman and gate.
Freebuff's terminal UI and log are a version-specific integration boundary; the image pins its
version, tests cover the terminal protocol, and a live container pass must be checked after an
upgrade.

---

## ADR-0017 — The patch is the only handoff

**Context.** A Result could hand back a branch the worker pushed (`artifact`) or a patch that
factory-mcp pushes. Every worker runs behind `worker/runner.py`, which holds no GitHub credential
and returns a patch, and the rehearsal returns a patch too (#39), so nothing produced `artifact`.
A worker that pushes its own branch also pushes before any check: a pushed workflow file runs with
the repo's secrets before factory-mcp can refuse it (#52).

**Decision.** A completed Result carries a patch, and only factory-mcp pushes. It refuses a patch
that touches a protected path before the push, pushes the WorkItem's branch, then checks what
GitHub holds: the pushed commit, its protected paths and its size. The Result schema stays `v1`,
because its only producer ships in this repo.

**Consequence.** A worker needs no GitHub access, and one code path pushes every change. A hosted
worker that can only push branches cannot be used as is; supporting one takes a new ADR and a
check that runs before its push.

---

## ADR-0018 — Agent docs are guarded by a script

**Context.** `AGENTS.md` tells every agent how to work in a repo, and nothing checked that it
stayed true: a moved file left a dead backticked path, a renamed doc left a broken link, and a
file could grow past any bound. A wrong doc misdirects every agent that reads it.

**Decision.** `templates/repo_lint.py` is the guard. Run from a repo root, it prints one
`path:line: problem` line for a backticked path in `AGENTS.md` that does not exist, a relative
link in `AGENTS.md`, `README.md` or `docs/*.md` that does not resolve, and a text file git
does not ignore that is over 800 lines (`--limit` overrides the limit). It prints nothing and
exits 0 when the repo is clean.
A span with no `/` counts as a path only when its suffix is a file extension, so `patches.apply`
is a module reference, not a path.

**Consequence.** Every repo the factory touches can run the same check with one script. Files
over 800 lines fail it: split the file or raise the limit. Wiring it into a repo's `verify`
command changes `.github/`, so that step stays with the owner.

---

## ADR-0019 — Owner-reviewed bootstrap PRs for new repos

**Context.** A repo needs gate config, a workflow and the factory standard before it can join the
factory. Requiring the owner to write each control file by hand made repo setup needlessly hard;
letting an ordinary coding worker edit those paths would let it alter its own review rules.

**Decision.** With one Hermes write approval, factory-mcp prepares a setup PR containing only
missing controls from its reviewed templates. It preserves existing valid controls and refuses
conflicts. The owner reviews and merges the PR, then enables the repo through the existing check.
The bootstrap tool never merges, and ordinary worker patches still cannot touch `.github/` or
`AGENTS.md`. A PR needs a base commit, so a repo with no refs at all first gets one commit on its
default branch that holds only a `README.md` with the repo's name; the audit log and the tool
result record it. It is pushed only when the repo has no refs at all, and it is not forced: if the
default branch got a commit since that check, the push is rejected and nothing more is pushed. A
repo with refs but no commit on its default branch is refused, and `factory_repo_check` tells the
owner to push a first commit there by hand. The controls never go straight to the default branch.

**Consequence.** The owner no longer authors boilerplate, while the first change to a repo's
authority remains a human merge. ADR-0022 sets the gate's Actions secrets.

---

## ADR-0020 — Lessons are owner-approved data; code finds the candidates

**Context.** Each WorkItem starts from nothing. When the gate blocks two WorkItems in one repo on
the same problem (say, "the new endpoint has no test for its 401 path"), the audit log holds both
verdicts, but nothing reads it to help the next WorkItem. Gate findings are model text (ADR-0007),
so passing them on to later workers unreviewed would be a prompt-injection path into every one.

**Decision.** The reconcile audit entry keeps the blocking findings of the verdict it accepts.
`lessons.candidates` groups them by fingerprint (ADR-0005) and offers each one the gate reported
on two or more WorkItems of one repo. Only a person turns a candidate into a lesson:
`factory_approve_lesson`, with the owner's wording (one line, at most 300 characters) and name.
Approved lessons live in `lessons.json` (`FACTORY_LESSONS_FILE`; `factory serve` uses
`~/.config/factory-mcp/lessons.json`), next to `repos.json` and for the same reason (ADR-0014): a
file a person can read and edit. `factory_retire_lesson` removes one. A model never approves a
lesson, and a fingerprint that is not a candidate is refused. A WorkOrder carries at most 10 of the
repo's lessons, newest first, each cut to 300 characters, because the file is hand-editable
(`lessons.MAX_PER_WORKORDER` and `lessons.MAX_TEXT`; the WorkOrder schema holds the same caps).
`factory_status` measures each lesson from the stored WorkOrders: on how many WorkItems it rode
along (`sent`), and on how many the gate reported its fingerprint again in a later round
(`recurred`). Three recurrences flag it as not working, for the owner to retire or rewrite.

**Consequence.** The factory can remember across WorkItems with no new table and no dependency,
and every lesson has a named approver. A finding the gate saw on one WorkItem never becomes a
lesson. When WorkOrders carry lessons, they stay data under the worker rules, like
`findingsToAddress`. The measurement comes from the stored WorkOrders, so a lost store resets it;
`lessons.json` keeps the lessons.

---

## ADR-0021 — Day Desk's shell runs in a Docker sandbox

**Context.** ADR-0013 gave Day Desk no shell, because a host shell runs as the owner: it could
read the profile's `.env` and call factory tools with no approval. So the owner had to make each
repo's first commit by hand, and Day Desk could not read a repo to plan issues for it.
Hermes can run its terminal tool in Docker instead. That container gets no host directory it can
write when `terminal.container_persistent` is false, and it gets only the environment variables
that the config names: Hermes mounts the profile's skills and caches read-only under `/root`, and
never mounts the profile's `.env`, `~/.config` or `~/.ssh`. Hermes also never forwards `GH_TOKEN`
itself.

**Decision.** Day Desk's `terminal` toolset is on, with `terminal.backend: docker` and the image
in `tenant/hermes/sandbox/`: git, a pinned gh and python3, for a user with no sudo. One variable
enters the container, `DAYDESK_GITHUB_TOKEN`, and the image's gh wrapper hands it to gh as
`GH_TOKEN`. It is a fine-grained token that the owner makes for this job alone, for the factory's
repos only, with Contents read and write, Pull requests read only, and no Administration: neither
of the shell's jobs writes a PR. It is not the factory's token. Day Desk uses
the shell to make the first commit of an empty repo and to read a repo; the owner creates repos.
It never merges, never pushes to a `factory/*` branch or, after the first commit, to a default
branch, and never changes `.github/` or `AGENTS.md`. `code_execution`, `file`, `browser`,
`computer_use` and `delegation` stay off: they run on the host or call tools back on it.

**Consequence.** Hermes asks no approval for a command in a Docker sandbox that has no writable host
mount, so the owner's yes in Telegram now covers factory tools only. The shell's limits are the
skill, the token's permissions and branch protection. A token that can push can also push to a
default branch, and a prompt-injected Day Desk could try. Branch protection on `main` is the
backstop (ADR-0008), so each repo needs it before the factory works there. The token has no
Administration, so it cannot change that protection, delete a repo or create one. Without the
Workflows permission, GitHub refuses a push that changes `.github/workflows/`. A clone is gone
when Hermes removes the container at the end of the session. Services on the host's loopback may be reachable from the
container, so each one keeps its own bearer token.

---

## ADR-0022 — Bootstrap sets the gate's Actions secrets from the owner's Keychain

**Context.** A repo's gate needs the Actions secrets `OPENCODE_GO_API_KEY` and
`GATEKEEPER_READ_TOKEN`. After ADR-0019 the owner still added them by hand in each repo. The gate
workflow calls the public wrapper Ami-Khokhar/pr-gatekeeper-workflow, but that wrapper checks out
the private Ami-Khokhar/pr-gatekeeper with `GATEKEEPER_READ_TOKEN`, else the repo's own token,
which cannot read another private repo. So the gate needs both while pr-gatekeeper is private. A
secret value must never reach a model, a log or a PR.

**Decision.** The owner stores each secret once in the login Keychain as a generic password with
service `software-factory/<NAME>`. On each `factory_bootstrap_repo` call, factory-mcp, not a model,
lists the repo's secret names with `gh secret list`, leaves alone a secret the repo has, reads a
missing one with `security find-generic-password -w` and gives it to `gh secret set` on stdin,
never in argv, with the factory's token as `GH_TOKEN`. gh does GitHub's libsodium encryption, so
factory-mcp stays standard library only. It lists the names again to confirm. The result, the audit
log and the setup PR body name each secret with a status: `set`, `already set`, `missing: ...` or
`failed: <command> exited <code>`. Nothing reads stderr, where a failing command may echo a value.
A missing or failed secret does not stop the setup PR: the owner adds it and calls the tool again.

**Consequence.** Repo setup needs no manual secrets, only one `security add-generic-password` per
secret on the owner's Mac. factory-mcp needs `gh` on its PATH and runs in the owner's login session
(the launchd agent does) to read the Keychain. This supersedes the last sentence of ADR-0019's
consequence.

---

## ADR-0023 — One setup PR per repo, and why gh refused to list secrets

**Context.** On Ami-Khokhar/Morningbrief a retry of `factory_bootstrap_repo` opened a second setup
PR: the branch name comes from a fingerprint of the planned files, and the templates had changed
since the first PR. The same run reported `failed: gh secret list exited 1` for both secrets, and
the owner could not see why, because nothing read stderr (ADR-0022). The owner's own gh login
listed the secrets, so the factory's token was the likely cause.

**Decision.** Before it creates a branch, the bootstrap adopts the open PR into the default branch
from a `factory-bootstrap/` branch of the repo itself; more than one is refused, naming each. When
the plan's files differ from the adopted branch, factory-mcp adds one commit with them on top of
the PR's head and returns it as `updatedSha`. It rewrites the PR body from the current plan and
secret statuses whenever that body differs, also on a retry that only set a secret. It first
checks that the default branch has not moved since it read the plan, as it does before it creates
a branch. That push is not forced: a branch that moved during the update fails
with an error that names the PR. For `gh secret list` only, which is given no
value, the failed status holds gh's first stderr line, at most 200 characters, with the factory's
token and anything shaped like a GitHub token (`ghp_`, `gho_`, `github_pat_` and the like) replaced
by `[token]`. An HTTP 403 or "Resource not accessible" line adds that the token needs Actions
secrets read/write. The stderr of `gh secret set` and `security` is still never read. This
supersedes the sentence of ADR-0022 that nothing reads stderr.

**Consequence.** The owner reviews one current setup PR per repo, and a commit the owner pushed to
it stays: the update goes on top. A token that lacks the Secrets permission shows as such, so the
owner fixes it without guessing.

## ADR-0024 — One approval starts a plan, and `Depends on #n` orders it

**Amended by ADR-0042**: factory_set_dependencies changes the order of a WorkItem no worker has touched.

**Context.** Day Desk turned a goal into issues with one `factory_propose_issue` call per issue,
then one `factory_approve_issue` call per issue, and Hermes asked the owner before each call: a
goal of 6 issues cost 12 approvals. The owner reviewed the plan once, in chat; the other approvals
added clicks and no review. The issues of a plan also build on each other, and nothing stopped the
factory from starting the second issue before the first one merged.

**Decision.** `factory_start_plan` takes the repo, the issues in order and who approved. Hermes
shows its full arguments in the approval prompt, so that one approval is the owner's review of the
plan. factory-mcp checks every issue before it opens one: 1 to 10 issues, each body with an
`## Acceptance` list that intake accepts, and no title or body that names a protected path. Code
cannot tell a request to change `.github/` from a mention of it, so a mention is refused. A
mention is a whole path word: `raw.githubusercontent.com` or `AGENTS.mdx` names no protected path.
Then it opens the issues in order, labelled `factory:ready`, and puts `Depends on #<previous>` at
the top of each body after the first. When one fails, the error names the issues the factory works
on, and an issue GitHub opened that intake then refused as one it will not run. The order lives in
GitHub (ADR-0009): a `ready` WorkItem whose issue has a `Depends on #n` line is not dispatched
while #n is open, and records `waitingOn: n` until it leaves `ready` by any route. When #n
is closed with a merged PR on `factory/<n>`, the next tick dispatches it. When #n was closed
without one, ended `cancelled` or `exhausted` (the store knows these, because such an issue can
stay open), or does not exist, the WorkItem goes to `human_waiting`: only the owner changes the
plan. The owner edits or removes the line and retries, or closes. Code makes each of these moves
(ADR-0010).

**Consequence.** A plan costs one approval. Any issue with a `Depends on #n` line waits, not only
the issues of a plan. A plan cannot name `.github/` or `AGENTS.md`, even to say that they must not
change. A second call opens the issues again. `factory_propose_issue` and `factory_approve_issue`
stay for single issues.

---

## ADR-0025 — A weekly report, computed by code, tells the owner where the factory needs a person

**Context.** The owner wants the factory to improve with little manual work. The audit log records
every escalation and every gate finding, and `factory_status` shows the autonomy rate on request,
but nothing told the owner, without asking, which reasons sent WorkItems to a person this week or
which lessons stopped working. Those are the inputs for the next change to the factory.

**Decision.** `factory report` prints one message for the 7 days to now: the autonomy rate of the
WorkItems that ended in them and in the 7 days before, how many WorkItems went to `human_waiting`,
`blocked` or `exhausted` and the 3 most frequent reasons, the lesson candidates that became
candidates in the window, and the lessons flagged as not working. The numbers come from
`metrics.weekly` over WorkItems and the audit log, through the read tool `factory_report`; the CLI
only formats them, at most 20 lines. Each move into one of those states records its reason in the
audit entry as `escalation`; reasons are grouped after commit shas are taken out. A Hermes no-agent
cron job, like `factory-watch`, delivers it to Telegram every Monday at 09:00. No model runs, and
nothing changes.

**Consequence.** The owner sees the week's escalation reasons without asking and can turn one into
an issue for the factory. Escalations logged before this change have no recorded reason and count
as "reason not recorded". The schedule is set up once on the owner's machine (RUNBOOK); a deploy
does not install it.

---

## ADR-0026 — pi, headless, is the factory's coding worker

**Amended by ADR-0028**: pi is one worker among any the relay registers.

Supersedes ADR-0016.

**Context.** Freebuff has no headless mode, so `worker/freebuff.py` drove its terminal UI and
accepted a pass only when Freebuff's session log recorded `Main prompt finished`. On agent-relay#43
it never did, and the pass timed out with no work handed back. A worker that needs its UI scraped
cannot run day to day without a person. pi runs the same model, GLM 5.3 Flash through OpenCode Go,
with `pi --print`, which exits when the prompt is done.

**Decision.** The relay agent `pi-glm-factory` runs `worker/runner.py` with `pi --print` and the
model in its args. The runner copies only `~/.pi/agent/auth.json` into a 0700 scratch directory,
mounts it in the container, and points `PI_CODING_AGENT_DIR` at it. The image pins pi and sets
`PYTHONDONTWRITEBYTECODE`, because the runner commits everything the harness leaves, and a test run
would otherwise leave `__pycache__` in the patch. The Freebuff adapter is deleted.

**Consequence.** A pass ends on the harness's exit code, not on a log line in a UI, and a non-zero
exit fails it before any patch is collected. The model key is the owner's OpenCode Go key. The
sandbox, the patch handoff and the base-commit verify are unchanged.

---

## ADR-0027 — A repo joins the factory on first use

Supersedes ADR-0014 for how a repo joins the allowlist.

**Context.** The owner wants the factory to work on any of their repos, not only the ones listed
ahead of time. An issue labelled `factory:ready` in a repo that was not listed was ignored without
a word, and Day Desk's tools refused such a repo until the owner ran `factory add-repo`. The
factory's token already reaches all the owner's repos.

**Decision.** Each rebuild runs one GitHub issue search per trusted author (`FACTORY_TRUSTED_AUTHORS`,
or `FACTORY_OWNER` when that is empty) for their open `factory:ready` issues in any repo. A repo
that is not listed is enabled through the same path as `factory_add_repo`: the repo check, the
worker checkout, the labels and an entry in `repos.json` that names who marked which issue. If the
check fails, the issue gets one comment with the failed checks and their fixes, recorded in the
audit log as `repo_not_ready`, and the next rebuild tries again. `factory_approve_issue`,
`factory_propose_issue` and `factory_start_plan` enable a repo the same way. Lessons stay limited
to listed repos.

**Consequence.** Only trusted authors' issues are searched, and a worker still never touches a repo
without the gate and the factory standard. `repos.json` remains the readable record of every repo
the factory enabled. A failing search is logged once per process and does not stop the tick. The
search API's index can lag behind a new label by a minute or so.

---

## ADR-0028 — The relay agent, not the runner, chooses the harness

Amends ADR-0026.

**Context.** agent-relay exists so that any registered agent can take a task, and the owner wants
the worker tied to no model or harness. After ADR-0026, `worker/runner.py` copied only pi's login
and the image held only pi, so an agent that ran Codex or OpenCode through the runner could not
log in.

**Decision.** The runner takes `--credential HOST_FILE:CONTAINER_DIR` and `--env KEY=VALUE` from its
relay agent's args. Each credential is a 0600 copy in its own 0700 scratch directory, mounted in the
harness container only; the env reaches only the harness. The runner names no harness. The image
pins pi, Codex and OpenCode, and creates the scratch HOME's XDG directories writable by any UID, so a
credential mount there leaves no root-owned parent. An agent may name another image with `--image`.

**Consequence.** Adding a worker is an agent definition in the relay and an entry in
`FACTORY_WORKERS`, with no factory change, and routing's failover can move between harnesses.
Nothing is copied back from the sandbox, so a login that rotates its tokens should not be used
there: Codex needs an API-key login.

---

## ADR-0029 — An issue chooses its worker with `Worker:` lines

**Context.** The owner wants to choose the worker for a piece of work, as agent-relay allows, and
not only through the order of `FACTORY_WORKERS`. agent-relay also registers agents that run on the
host with no sandbox, so an issue's text must not be able to reach any relay agent.

**Decision.** A `Worker: <relay agent id>` line in the issue chooses the worker; several lines give
an order, and routing's failover stays within them. The lines are read from the issue on each
tick, like `Depends on #n`, so editing the issue changes the choice and the WorkItem schema does not
change. Only agents in `FACTORY_WORKERS` may be named: a name outside it sends the WorkItem to
`human_waiting` with the allowed names, before any task is submitted. `factory new -w <id>` writes
the line, and Day Desk writes it when the owner names a worker.

**Consequence.** The owner, or Day Desk for them, picks the harness and model per issue.
`FACTORY_WORKERS` stays the allowlist of sandboxed workers and the default order.

---

## ADR-0030 — Day Desk drives the factory; factory-mcp keeps the guards

Supersedes ADR-0013 for the owner's yes on each write, and amends ADR-0010. **Superseded by
ADR-0041** for the yes on the owner-token tools.

**Context.** The owner wants Hermes to drive the factory and to be asked only for what the guards
cannot decide. Every factory write waited for the owner's yes, and every escalation waited for the
owner, even when the next step was plain: retry with a note, or send a PR back to a gate whose
reviewer failed. A guard on Day Desk could not rest on the `by` a model writes into a call.

**Decision.** factory-mcp accepts a second token, `FACTORY_AGENT_TOKEN`. With it, factory-mcp lists
and runs only `tools.AGENT_TOOLS`: status, repo check, lessons, report, and approving, proposing,
planning and resolving. A repo's setup, its gate secrets and the lessons workers read stay on the
owner's token. Day Desk's profile reaches the agent tools through a `trust: full` server with that
token, and the rest through the `trust: untrusted` server, which asks the owner. With the agent
token, `factory_resolve_human` resolves each WorkItem at most once; the next escalation waits for
the owner, and the audit log marks the call as the agent's. A Hermes cron job, `factory-drive`,
runs the skill's "Drive the factory" section whenever `factory drive-queue` changes; the skill
retries or regates only for reasons it names and never closes. `factory watch` tells the owner of
an escalation Day Desk may not answer, or one it left for 30 minutes. factory-mcp keeps its own
tick loop: the guards and the state machine do not depend on Hermes running.

**Consequence.** Most escalations a worker can fix get a second pass without the owner, and the
owner hears of the rest. A model still never merges, and nothing Day Desk calls can pass the
sandbox, the protected paths, the size limits, the trusted authors or the budget of passes. A
wrong retry costs at most one pass from that budget.

---

## ADR-0031 — A merged change deploys itself

**Context.** A change merged into software-factory reached the running factory only when someone
ran `factory update`, so every improvement waited for a person, and the factory could not improve
itself unattended. A deploy also left Day Desk behind: its skill, soul and sandbox image changed
only when the owner ran the steps by hand, and its profile was installed from a working checkout
that may hold unmerged work.

**Decision.** A Hermes no-agent job, `factory-update`, runs `factory update` every 10 minutes. The
command keeps its rule: it deploys only a commit whose tests pass. It now also rebuilds Day Desk's
sandbox image when `tenant/hermes/sandbox/` changed and runs `hermes profile update day-desk -y`
when `tenant/hermes/` changed, with the profile installed from the deployed checkout. A failed
image build or profile update stops the deploy with its message, which the job sends to the owner,
and runs again on the next update: a state file names the last commit for which every step
succeeded. The profile is updated only when Hermes names the deployed checkout as its source.
A change to `tenant/hermes/config.yaml` is reported, not applied, because the live file also holds
the owner's model and Telegram settings. The deploy does not restart Day Desk's gateway, which runs
the job.

**Consequence.** A merge goes live within 10 minutes with no person, and a broken commit never
does. The owner hears of a failed deploy and of a config change to merge. A bad commit that passes
the tests still deploys; the gate and the tests stay the safeguards.

---

## ADR-0032 — A PR that conflicts with its base is made again on the current base

**Context.** GitHub runs no `pull_request` workflow on a PR that conflicts with its base, so the
gate never answers it. A factory PR that main overtook waited `FACTORY_GATE_WAIT_SECONDS` (2 hours)
and then went to the owner with "no pr-gatekeeper verdict", which named the wrong cause. A fix
WorkOrder builds on the PR's head, so it would keep the conflict.

**Decision.** When GitHub reports `mergeable: false` for a factory PR in `pr_open`, `reviewing` or
`changes_requested`, the tick dispatches an implement WorkOrder on the current default branch. Its
first finding says the PR conflicts and the change must be made again; the gate's blocking
findings follow when the PR had them. The push replaces the branch, as each implement push does,
and the open PR follows it, so the gate runs on the new head. `mergeable: null`, which GitHub
reports while it computes, waits for the next tick.

**Consequence.** A conflict costs one worker pass, within `FACTORY_MAX_PASSES`, instead of 2 hours
and the owner's time. The worker redoes the change rather than resolving markers, so the gate
reviews a whole diff against the current base. Each tick reads each open PR once more.

---

## ADR-0033 — The gate's run is a secondary fact; its verdict decides

**Context.** A fine-grained token gets 403 on check runs in a private repo, and the observe step
raised on it, so every tick of a software-factory WorkItem failed before reconcile and the gate's
verdict on its PR was never taken in (#112). The check run only moves `pr_open` to `reviewing` and
catches a gate that finished without a verdict; the verdict comment decides the state.

**Decision.** A 403 on the check-run read falls back to the pr-gatekeeper workflow run on the
head. A 403 on that read too leaves the gate fact absent, and the WorkItem moves on its verdicts
alone. A cancelled run, from either read, is not the gate's answer, because a newer push or edit
cancels it. Any other error on either read still fails the tick.

**Consequence.** A private repo works with a token that cannot read check runs. Without the
Actions read permission, a gate that never answers reaches the owner only after
`FACTORY_GATE_WAIT_SECONDS`, with a reason that names the gate run, not the token.

---

## ADR-0034 — A regate starts the gate run it waits for

**Context.** When the gate fails itself ("reviewer verdict is invalid", a run that finished with
no verdict), `regate` moved the WorkItem back to `reviewing`, but nothing started a new gate run:
the PR had no new push. The WorkItem waited `FACTORY_GATE_WAIT_SECONDS` and escalated again, and
the answer Day Desk gives for exactly this case (ADR-0030) did nothing. `workflow_dispatch` needs
more than the factory's token may do, and it has never started the gate job in software-factory.

**Decision.** On `regate`, factory-mcp reads the PR. When no gate run is going and no verdict
newer than the accepted one is there, it edits the PR body: it adds `<!-- factory regate 1 -->`,
or counts the one already there up by one. The `edited` event starts a gate run. A regate edits
nothing while a gate run is going, when a verdict newer than the accepted one is on the head, or
when the PR is closed.

**Consequence.** A regate after the gate failed itself gets a new review within the gate's run
time. The PR body carries one hidden line per PR; its count restarts when factory-mcp rewrites
the body after a push, which is enough, because the count only has to change the body. The
WorkItem keeps `regatedAt`, the time of the regate, and a gate run that started before it is not
taken as the gate finishing without a verdict: until GitHub registers the new run, the old one is
still the latest. A run with no start time is taken as an answer, so the WorkItem never waits on
it forever. A failed edit fails the regate with GitHub's
error, and the WorkItem stays with the owner.

---

## ADR-0035 — A person's retry buys a fresh budget of passes

**Context.** Ami-Khokhar/Morningbrief#3 spent all 4 worker passes on one infrastructure fault: the
worker image had no `python` (#136). The owner answered `retry` after the fix, and the next tick
moved the WorkItem to `exhausted`, because the budget counted every pass the WorkItem ever used.
The person's decision bought nothing, and `exhausted` could not be answered at all.

**Decision.** `FACTORY_MAX_PASSES` limits what the factory spends on its own between two decisions
of a person. A person's retry records `passesBefore`, the passes used so far, and the budget counts
only the passes after it. Day Desk's retry keeps the budget the WorkItem has (#141), as ADR-0030
says. The owner may answer `exhausted` with `retry` or `close`. Day Desk may not: a model never
decides to spend a new budget on a WorkItem that used one up.

**Consequence.** Each retry by a person can cost up to `FACTORY_MAX_PASSES` more passes, and the
factory still stops on its own after that many. A WorkItem nobody retried stops exactly as before.

---

## ADR-0036 — Passes of one repo run at once and take its checkout in turns

**Context.** The Foreman ran one worker pass per repo at a time, because every pass of a repo
fetches in the same persistent checkout, and two fetches at once fail on git's ref locks. So
independent issues of one repo waited for each other, and most of a pass's time is the harness
and the verify, which never touch the checkout.

**Decision.** Up to `FACTORY_PASSES_PER_REPO` passes of one repo run at once (default 2, never
below 1). `worker/runner.py` holds an exclusive lock on `factory-runner.lock` in the checkout's git
directory while it fetches, copies the checkout and reads the verify command; the harness and the
verify run on the pass's own copy, outside the lock. The seconds a pass waits for the lock come out
of the harness's share of its time, so the pass still ends within the relay's timeout. The
Foreman's count is a soft limit: two ticks at once may each start a pass.

**Consequence.** Anything that reads or writes a worker checkout takes the lock. Two open PRs of
one repo can conflict; the second is made again on the current base (ADR-0032), which costs it a
pass and a gate round.

---

## ADR-0037 — The benchmark runs fixed tasks through the factory and grades them with hidden tests

**Context.** The autonomy rate counts what merged with no person, but not whether the merged code
is right. The factory's real work changes from week to week, so two workers, or the factory before
and after a change, cannot be compared on it.

**Decision.** `bench/` holds fixed tasks: an issue, hidden tests and a reference solution each.
`factory bench` files them as issues in a sandbox repo, and the factory runs them as it runs any
issue. `factory bench report` grades each merged result with its task's hidden tests, in the worker
image with no network. The hidden tests stay in this repo, so no worker reads them, and a test
shows that each passes on its reference. Each task builds a new module under `bench/<run>/`, so
runs never conflict.

**Consequence.** A run is a repeatable measure of a worker or of a factory change: autonomy,
correctness, size, time and passes. It spends real worker passes and gate reviews, so it runs when
the owner starts it, not on a schedule.

---

## ADR-0038 — The worker reads the issue's description

**Context.** A WorkOrder carried the issue's title as its goal and the acceptance list, but not the
text the owner wrote around them. Morningbrief#5's requirements, such as marking a delivery done
only after Telegram confirms it, never reached its worker, which then had to guess them.

**Decision.** Intake freezes the issue's text outside its Acceptance section as
`WorkItem.description`, as it freezes the goal and the acceptance list, and each WorkOrder carries
it. It is issue text from a trusted author, so it reaches the worker as data under the worker's
rules, like the goal (ADR-0007).

**Consequence.** Workers see the whole issue, and the prompt grows by its text. An edit to the
issue after intake does not reach the worker, so the trust check at intake still covers all the
text a worker reads.

---

## ADR-0039 — The gate's merge of the base branch is not a person's commit

**Context.** The autonomy rate counted any PR head that no worker Result produced as a person's
commit. Before it reviews a PR that fell behind its base, pr-gatekeeper merges the base branch into
it through GitHub's API. With two passes of one repo at once (ADR-0036), that happens whenever one
PR merges while another is open, so benchmark run r1003062707 reported a person on a task that no
person touched.

**Decision.** When a PR's head moves to a commit the factory did not push, the Foreman reads that
commit once. A merge whose first parent is the head before it, authored by `github-actions[bot]`
and signed by GitHub (committer `web-flow`, verified), is the gate's merge of the base branch: the
reconcile records `mergedBase`, and `metrics.autonomy` counts it with the workers' commits. Any
other commit, including a person's "Update branch" click on GitHub, still counts as a person's, and
so does a commit GitHub will not show (403 or 404).

**Consequence.** The autonomy rate stops counting the gate's own commits as people, at one GitHub
read per new head. History recorded before this change still counts such merges as a person's.

---

## ADR-0040 — A change over the size limits is refused before the push

**Context.** factory-mcp checked a PR's size only after it pushed a worker's patch. A push to an
open PR stays on it when the check then fails the attempt, and a fix WorkOrder builds on the PR's
head. Morningbrief#5's fix took PR #10 from 596 to 653 lines: the attempt failed, the 653 lines
stayed on the PR with a body that did not list the new file, and the gate's lint sent the
WorkItem to the owner with no finding a worker could fix.

**Decision.** `patches.apply` refuses a patch that would take the branch over the WorkOrder's
limits, counted from where the branch left the default branch as GitHub counts a PR, before it
pushes, as it already refuses a protected path. `Foreman.check_pushed` still checks what GitHub
holds after the push.

**Consequence.** A failed attempt over the limits leaves the PR as it was, so the next attempt
and the gate see the last good head. The count runs on the factory's own clone, so it does not
trust the worker; GitHub's count after the push stays the authority.

---

## ADR-0041 — Day Desk is a full Hermes agent on a standalone gateway

Supersedes ADR-0030 for the owner's yes on the owner-token tools, and ADR-0013 and ADR-0021 for
the toolsets that stay off.

**Context.** On 4 Oct 2026 the owner asked for Day Desk to be a normal Hermes agent with the
factory as one more capability, with no yes per factory call. Day Desk's sandboxed shell also
failed on every call since 3 Oct (118 calls in its log): Hermes treated the day-desk gateway as
multi-profile, so it read `DAYDESK_GITHUB_TOKEN` for the container outside any profile's secret
scope and raised `UnscopedSecretError`. With the multi-profile flag off, the same call ran.

**Decision.** Both factory servers are `trust: full`; the owner-token server still carries only
the owner-token tools. No toolset is off. terminal, file and code_execution run in the Docker
sandbox of ADR-0021; browser and computer_use act on the host as the owner. `approvals.mode` is
`manual`, so what Hermes gates waits for the owner, not a model. `gateway.standalone` is true.
SOUL and the skill let the shell do any task the owner asks for, and keep its factory-repo limits.

**Consequence.** Day Desk can set up a repo, add one and approve or retire a lesson with no yes;
the owner's token names the caller in the audit log. A prompt-injected Day Desk could change
the lessons every worker reads, or act on the host through browser or computer_use. The worker
sandbox, the gate, branch protection and factory-mcp's guards stay the boundaries.
`gateway.standalone` is a compatibility setting Hermes says it will remove; when it goes, check
the shell again before an update.

---

## ADR-0042 — A tool changes the dependencies of a WorkItem no worker has touched

Amends ADR-0024 and ADR-0030.

**Context.** `factory_start_plan` chains its issues: each one gets `Depends on #<previous>` and waits
for it to merge. Most plans have independent issues, and no tool could change the chain afterwards,
so Day Desk could not run them side by side. Editing the issue body by hand was the owner's only
way, and a hand edit has no guard against a cycle or a dead dependency.

**Decision.** `factory_set_dependencies` takes a WorkItem id, the issue numbers it waits for (`[]`
for none) and who asked. Because the order lives in the issue body (ADR-0024), it replaces the
body's `Depends on #n` lines (each whole, with any text after the number) and leaves the rest of the body as it was; the next tick reads them
like any edit. Code refuses the change when the WorkItem is not `issued`, `planned` or `ready`, or
has a WorkOrder on record (a retry back to `ready` has one), so a change never moves a WorkItem
a worker has started. Each number must be an issue of the same repo (the tool takes numbers, so
another repo cannot be named), not the WorkItem's own issue, not a pull request, and not an issue
that ended: missing, closed without a merged PR, `cancelled` or `exhausted`. A change that
would make a path of `Depends on` lines lead back to the WorkItem is refused as a cycle. The
audit log records `dependencies_changed` with who asked, the numbers before and after, and
`agent: true` when Day Desk called; a stale `waitingOn` is cleared at once.

The tool joins `tools.AGENT_TOOLS`, and Day Desk's `factory_drive` include list. A wrong change
can only start an issue earlier or later: the sandbox, the protected paths, the size limits, the
trusted authors, the budget of passes and the gate bound every WorkItem however it is ordered
(ADR-0030), and a conflict between two parallel PRs is made again on the new base (ADR-0032).
The guards above stop the changes that would strand a WorkItem. Asking the owner for each
change would put back the clicks ADR-0024 removed for a plan.

**Consequence.** Day Desk can start a plan with `factory_start_plan` and then free the independent
issues, at the cost of more parallel passes that may conflict. A change after a worker started is
refused, so the only way to reorder it is the owner's `retry` and an edit on GitHub. No contract
changes: `waitingOn` keeps its meaning and the dependencies stay in the issue body.

---

## ADR-0043 — Day Desk may retry a spent WorkItem when the owner asks in chat

**Context.** ADR-0035 said Day Desk may not retry a WorkItem that used its budget of passes, so
`exhausted` waited for `factory retry` on the Mac. The owner hit this on Ami-Khokhar/ideadump#2:
the owner was in Telegram, asked for the retry, and Day Desk had to refuse.

**Decision.** `factory_resolve_human` takes an optional boolean `ownerAsked`. With the agent token
and resolution `retry`, it lets Day Desk retry an `exhausted` WorkItem or a `retry` past the budget.
The retry buys a fresh budget as a person's does, and it does not use up Day Desk's one answer
(ADR-0030). The history row holds `agent` and `ownerAsked`. Without it, the refusal of ADR-0035
stays, and any other resolution with `ownerAsked` is an error. The skill sets it only when the
owner, in the current chat, asked to retry that WorkItem, with the owner's words as the note. The
factory-drive job never sets it, so no unattended run spends a new budget. A flag is a claim that
code cannot check, so the guard is the skill and the audit row. It is the same trust as the note.

**Consequence.** The owner can restart an exhausted WorkItem from Telegram. A model that sets the
flag wrongly can spend one budget of passes, and the history shows it. When a failure comes from
an environment limit, such as a verify the Linux worker cannot run, a retry fails the same way, so
Day Desk says that first. This supersedes ADR-0035's "Day Desk may not" for retries the owner asked for.

---

## ADR-0044 — Day Desk knows what the factory can run, and does not retry what cannot pass

**Context.** Ami-Khokhar/ideadump#2 used its whole budget of passes. Its verify is
`xcodegen generate && xcodebuild test ... -destination 'platform=iOS Simulator,...'`, and a worker
runs in the Linux container of `worker/Dockerfile` with none of those tools. Day Desk retried the
WorkItem and reported "xcodegen was missing" without seeing that the verify can never pass there.
The owner chose no macOS worker and no change to the verify: Day Desk needs enough context to
find workarounds and explain the failure.

**Decision.** The skill gets `environment.md`: what the worker image and the gate can run, the
pass budget, Day Desk's own sandbox, how to tell an environment limit from a code fault, the
workarounds to offer, and a check at enable time that reads the verify command in
factory_repo_check's `gate_config` detail. Day Desk does not retry an environment limit: it says so
and offers the workarounds, and the owner picks. No code changes: factory_repo_check already
returns the verify command, and each failed attempt in `tried` already holds the verify output.

**Consequence.** This is context, not a guard: a model can still misjudge, and `retry` stays
available. The file has no test that its facts match `worker/Dockerfile`; whoever changes the
Dockerfile, the budget or the gate updates it in the same pull request. An iOS repo gets work
only after the owner picks a workaround, such as a Swift package that tests on Linux, which also
needs a Swift toolchain in the worker image.
