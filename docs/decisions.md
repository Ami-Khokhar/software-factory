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
lesson, and a fingerprint that is not a candidate is refused. `factory lessons-path` prints the
path the service resolves, env or the default next to repos.json, and verifies the file loads
without any credential (Issue 204). A WorkOrder carries at most 10 of the
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

**Amended by ADR-0082**: a plan item may name the earlier issues it waits for as `after` positions,
so a plan is a graph and not only a chain.

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

**Extended by ADR-0046** to `close` and `regate`.

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

---

## ADR-0045 — A repo whose verify needs macOS is verified on a GitHub macOS runner

**Context.** ADR-0044 told Day Desk what a worker cannot run, and left a repo such as
Ami-Khokhar/ideadump (verify: `xcodegen generate && xcodebuild test ...`) with no way to pass. The
owner decided on 4 October 2026 that the factory should run that verify, not the model and not the
owner. Two ways were rejected. Running the verify on the owner's Mac as a separate macOS user: that
user can list the owner's home, which is group-readable, and reach the services that listen on
127.0.0.1, so model-written code would run next to the owner's data. A local VM with Xcode (tart):
an Xcode image is 50 to 100 GB, and the Mac has 66 GB free.

**Decision.** `FACTORY_REMOTE_VERIFY_REPOS` lists the repos. For them the WorkOrder has
`remoteVerify: true`, the worker runner skips its in-container verify, and the Foreman does not
require test evidence in the Result. After a valid Result, factory-mcp pushes the patch to the
scratch branch `factory-verify/<issue>-<n>` with the same checks as any push (`patches.apply`:
protected paths, size limits). The repo's own `.github/workflows/factory-verify.yml`, on push to
that prefix, runs the base commit's `verify` on a macOS runner with read-only permissions and no
secrets. A patch cannot change `.github/`, so the branch's workflow is the merged one. Each tick
looks for the finished run and stores the wait as `WorkItem.remoteVerify`, so the tick never
blocks. Pass: delete the scratch branch, then push `factory/<issue>` and open the PR as before,
with the run as evidence. Failure: delete the branch and record a failed attempt whose finding is
the log tail, so the next WorkOrder gets it; it counts in the pass budget. No finished run after
`FACTORY_REMOTE_VERIFY_SECONDS` (default 1800): a failed attempt too. A repo whose base commit has no workflow
file goes to `human_waiting` before any push, with no attempt used. A run query that fails (a token
without Actions read) is waited out until that deadline, then fails the attempt and deletes the branch. The workflow is not written by
bootstrap: `.github/` is the owner's, so the runbook gives the file and the owner merges it.

**Consequence.** The gate must pass on macOS too, and pr-gatekeeper fixes its `gates` job to
`runs-on: ubuntu-latest` (gatekeeper.yml:45) with no input to change it: an iOS repo's gate
cannot pass until pr-gatekeeper gets one. This ADR does not change pr-gatekeeper. A public repo's
macOS minutes are free, a private repo's cost 10 times the Linux rate. The verify runs twice for
a passing patch (scratch branch, then the PR's gate run). The scratch branch lives in the target
repo for a few minutes, and a crash may leave one: it is harmless and the next push replaces it.
The log tail is model-influenced text: it travels as data, like every finding (ADR-0007). Not
tested against a real macOS run: the workflow in the runbook was not run.

---

## ADR-0046 — Day Desk may close or regate when the owner asks in chat

**Context.** ADR-0030 lets Day Desk answer each WorkItem once, and ADR-0043 gave the owner's
in-chat retry its own path. On 8 October 2026 the owner was in Telegram with
Ami-Khokhar/Agentspace#12: Day Desk had answered it once already, the owner said "yes" to replacing
it with a smaller plan, and the factory refused Day Desk's close. The owner had to run
`factory close Ami-Khokhar/Agentspace#12` on the Mac, and #13, which waited on #12, waited on that.

**Decision.** `factory_resolve_human`'s `ownerAsked` applies to `retry`, `regate` and `close`. With
the agent token and the flag, it answers a WorkItem Day Desk already answered once, and, for a
retry, an exhausted WorkItem or one past its budget (which then buys a fresh budget). The skill
sets the flag only when the owner, in the current chat, asked for that choice, with the owner's
words as the note; the factory-drive job never sets it. The owner's own call does not need the flag.
A wrong `close` cancels a WorkItem the owner can open again; it does not merge, push or spend.

**Consequence.** The owner's decision in chat is executable without a terminal command. The flag
is still a claim the code cannot check, so the guard is the skill and the audit row holds `agent`
and `ownerAsked`, as in ADR-0043. An owner-asked answer does not use up ADR-0030's one answer.

---

## ADR-0047 — A WorkItem stranded on a dependency that ended may change its order

**Context.** ADR-0042 lets factory_set_dependencies rewrite the `Depends on #n` lines of a WorkItem
no worker has touched, but it allowed only the states issued, planned and ready. On 8 October 2026
Ami-Khokhar/Agentspace#5 depended on #4; the owner replaced and cancelled #4 with #11–#14, so #5
went human_waiting with no worker pass, and Day Desk could not point it at #14. The owner edited
the GitHub issue body by hand. Ami-Khokhar/Agentspace#13 hit the same wall that evening.

**Decision.** `set_dependencies` also accepts a human_waiting WorkItem, and still refuses one that
has had a worker pass. It changes the issue's dependency lines and clears a live `waitingOn`; the
WorkItem stays human_waiting until a person or Day Desk answers it with `factory_resolve_human`
retry, which reads the new lines on the next tick.

**Consequence.** The stranded-dependency case is fixable from Telegram: change the order, then
retry. The skill says so and names the replacement issues. A human_waiting WorkItem that ended for
another reason with no pass can also change its order; that is safe because no worker has built on
it. This extends ADR-0042.

---

## ADR-0048 — Day Desk's driving tool list comes from the agent token, not its config

**Context.** factory-mcp lists only `tools.AGENT_TOOLS` under `FACTORY_AGENT_TOKEN` (ADR-0030), and
Day Desk's profile also enumerated those tools in `mcp_servers.factory_drive.tools.include`.
`hermes profile update` keeps the live config.yaml, because it also holds the owner's model and
Telegram settings, and the deploy only printed a note to merge the change by hand. That copy went
stale: `factory_set_dependencies` merged into the template on 4 October 2026 and never reached the
running Day Desk, so on 8 October the owner edited Ami-Khokhar/Agentspace#5 on GitHub by hand.

**Decision.** `tenant/hermes/config.yaml` keeps no `tools.include` for `factory_drive`: the agent
token is the guard and the source of the list, so a new AGENT_TOOL needs no profile edit. When
`tenant/hermes/` changes, `factory update` removes `mcp_servers.factory_drive.tools.include` from
the live profile with `hermes config unset` if it is set, and says so. The owner-token server keeps
its include list: there the list is a real restriction, and setup, secrets and lessons stay
owner-only (ADR-0041).

**Consequence.** A driving tool reaches Day Desk within one deploy with no hand merge. The
factory-owned key is safe to remove; every other live setting is untouched. An install that never
runs the update can remove the list by hand (RUNBOOK).

---

## ADR-0049 — `factory update` verifies Day Desk's tool discovery after a profile update

**Context.** ADR-0048 removed the hand-merged driving tool list, so a profile update is the only
step between a merged AGENT_TOOL and Day Desk being able to drive it — and until now nothing
checked that the updated profile actually serves the tools. A profile update that silently drops a
factory server or a tool again leaves Day Desk blind, just as ADR-0048's stale `tools.include` did,
while the deploy records success.

**Decision.** When `factory update` updates Day Desk's profile, it verifies before it advances the
deploy record: it calls `tools/list` on the running factory-mcp for both tokens, and asks the live
profile (`hermes -p day-desk mcp tools <server>`) what it discovers from `factory_drive` and
`factory`. The drive server, whose discovery is not filtered, must discover every tool the agent
token lists; the owner server filters through its tools.include, so there the shipped
`tenant/hermes/config.yaml` include list must be discovered and each of those tools must really be
on the owner token's list (an old name fails). Anything missing, a failed profile update, or a
factory-mcp that answers with no tool list fails the deploy with the missing names. The
verification only reads; it never writes the profile, and it never prints a token or a setting.

**Consequence.** A deploy record can no longer claim success while Day Desk cannot reach a tool the
factory just merged; the update retries on the next run with the previous baseline in place. A
deploy on a machine where factory-mcp is down cannot record success either, which is honest for a
deploy whose steps must prove the profile works. The tokens stay in the secrets file; nothing new
to keep in sync. The check needs an installed `hermes` that can list a profile's discovered
servers; where it cannot, the update fails with the profile update itself already done, and the
next update retries the whole set of steps.

## ADR-0050 — A failed attempt is named only where its evidence names it

## ADR-0051 — A confirmed environment fault pauses instead of a second pass

**Context.** ADR-0050 made the Foreman name a failed attempt's cause from the Result's optional
diagnostics, including `checkout or setup`. The Foreman still treated every failure the same way:
back to `ready` and another pass, with routing steering it away from the worker that failed to an
alternate one — as if the failure belonged to the code. When the failure belongs to the shared
environment (the checkout a pass runs in), every pass of that attempt meets it again, whatever
worker takes it, and the only thing a second pass buys is the same failure and one more pass spent.

**Decision.** When the diagnosis names the environment — `checkout or setup`, a setup or checkout
phase the harness's own diagnostics reported as failed — the Foreman pauses the WorkItem on the
first failed attempt: `human_waiting`, with a repair reason in `blockedOn` that names
`factory_resolve_human`, and no dispatch until a person answers. A `failing test` is the work, an
`empty patch` is the worker's output, and a harness timeout — a phase that only ran out of the
clock, and a timeout one of the reason strings suggests with no supporting diagnostics stays
unknown — is not evidence against the environment: those keep the normal recovery. The producer
side stays untouched: no worker is enabled by #191 either, and a diagnosis still names the
environment only from a Result's own evidence.

**Consequence.** factory_status shows the WorkItem as `human_waiting` with the repair reason, the
owner is told on the issue as for any pause, and `factory_resolve_human` with `retry` — a person's
own, or the owner's, or Day Desk's subject to ADR-0030 and ADR-0035 — starts the next pass once
the environment is repaired. One confirmed environment failure costs one pass instead of every
pass the budget had. A failing test or a timeout that would once have been read as an environment
fault stays code: the pause keys on the named cause alone, not on the reason strings.

## ADR-0050 — A failed attempt is named only where its evidence names it

**Context.** Since #189 a Result may carry optional diagnostics, but the Foreman logged only the
reason string. Two attempts whose reasons matched a timeout pattern were escalated as "the issue is
likely too big for one pass", which sends owners to split issues that may have failed for an
environment reason a retry would fix. Reason strings alone cannot tell that apart, so they must not
be read as a diagnosis.

**Decision.** A failed attempt gets a cause only from evidence: a completed Result without a patch
(unknown), a setup or verification phase that failed in the Result's own diagnostics, a failing test
evidence entry, or a harness execution phase that failed while running at least 90% of the
WorkOrder's `budget.wallMs` (a timeout). A timeout the reason strings suggest with no such evidence
stays an unknown cause, and the blocked guidance never says an issue is too big. Whatever text the
diagnosis carries — into the audit `attempt_failed` entry, escalation, and the next WorkOrder's
`findingsToAddress` as `previous-attempt` problem text — is sanitized at the controller too: one
line of at most 300 characters with things that look like credential contents, environment values,
or the WorkOrder's own command payload redacted, whatever the worker's producer already did.
Workers gain nothing yet: no producer is enabled by #190.

**Consequence.** factory_status shows, in the one `attempt_failed` audit entry it already returned,
which of checkout/setup, harness timeout, empty patch or failing test an attempt was, or no cause at
all when nothing names one; owners (and Day Desk) decide retry versus split from that evidence; a cause that names the
environment pauses the WorkItem instead of a second pass (ADR-0051)
instead of the old scope guess. Tails the worker did not redact cannot cross into a retry WorkOrder
or an escalation. An unknown-cause timeout now costs a fresh attempt instead of a split
recommendation, which is the honest reading of what the factory knows. The diagnosis runs whatever
else the Result carries, including the diagnostics the Result check rejected: malformed shapes and
evidence with no exit name no cause and cannot raise out of a tick, so a WorkItem is never left
`implementing` serving a crash on every later tick instead of the check's wording.

## ADR-0052 — The runner ships its diagnostics behind a deployed consumer

**Context.** #189–#191 deployed the consumer side of `Result.diagnostics` — the Result check, the
attempt diagnosis and the environment pause — while no producer emitted the field. #192 wires
`worker/runner.py` to emit it: one phase entry per pass stage with elapsed milliseconds, the exit
code where there is one, and a bounded, redacted tail, with a harness timeout keeping the output it
had already printed. The worker image runs on every machine separately from factory-mcp, so a
producer that ships before the consumer's flag-deployed controller can put a field a controller
rejects into every Result until the image is rebuilt down — a pass lost per WorkOrder.

**Decision.** The producer rollout is gated on the consumer: rebuild the worker image (or enable it
at all) only once `factory-mcp` with the #189–#191 consumer is deployed and running. The gate's
verify proves the ordering in tests: the phase-diagnostic tests fail against the runner from before
this change (red before the fix), and the tests that existed before it pass unchanged with a legacy
Result — one without `diagnostics` — still valid, still checked, still merged. The runner redacts
before serializing (copied credentials, configured secret values, environment values, the
WorkOrder's command payload), and the controller's sanitize behind it remains the second line, so
neither side trusts the other.

**Consequence.** A pass that rolls out in either order still works: an old image keeps producing
legacy Results the deployed controller accepts, and a rebuilt image produces diagnostics only a
deployed consumer reads. A harness timeout's output — bounded and redacted — rides into the
attempt diagnosis, so a worker that ran out of time is named from its own evidence instead of a
reason string.

## ADR-0053 — The runner preflights the environment before the harness

**Context.** A pass against an environment the harness cannot use — Docker down, the configured
worker image missing from the local store, the harness absent from that image, a credential file
missing or unreadable, the base commit's verify command gone — still reached the harness first:
the first failure the diagnostics saw was the harness's, which the pause consumer (ADR-0051) reads
as unknown or as the work, so the budget spent a pass learning what a host check already knew.
The environment-fault fields of ADR-0051 and the phase diagnostics of #192 are deployed consumers,
so the producer side is free to add evidence.

**Decision.** `worker/runner.py` runs a `preflight` before the coding harness runs: the Docker
daemon, the configured worker image, the harness resolved inside that image through a probe
container, the configured credential-copy (readable on the host) and the base commit's verify
command. Each failing check raises its own nonzero exit code (69 daemon, 72 image, 71 harness,
66 credential, 78 verify) on the `environment setup` phase, whose tail is the bounded preflight
label — a host path or a command payload never rides on it; the full reason stays in the Result's
`error` per the existing tests. Any nonzero code on a setup phase names "checkout or setup", so
the pass pauses instead of a second pass, and a code fault on the harness keeps the normal retry.

**Consequence.** An unavailable environment costs one pass and pauses for repair on the first
attempt; a healthy environment runs the harness exactly as before. A missing preflight check is
still a lost pass, which a rehearsal against real Docker (the runbook's preflight check) covers.

## ADR-0054 — The runner's real-container regression runs on demand and fails loudly

**Context.** The runner's unit tests fake the Docker boundary (tests/fakes.py, the sandbox and
its mounts), so a change to `worker/runner.py` or `worker/Dockerfile` could pass the gate while
breaking a behavior only a real container shows: the timeout kill, a credential mount, the
`collect` git flow, the preflight's probe. The gate's verify runs with no secrets and must stay
fast and deterministic, so real containers cannot be part of it.

**Decision.** `scripts/regression_docker.py`, invoked explicitly on demand, exercises the runner
against real `docker run` sandboxes through the same `Sandbox` class the runner uses: success,
failing verification, timeout, secret redaction and credential isolation, the preflight failure
codes, the early size rejection and cleanup. It needs no model credential; every case's
observations are recorded in a JSON report, and a case that recorded nothing counts as failed.
A missing Docker daemon or worker image is an explicit nonzero failure naming the fix — it never
skips a case and never reports a pass it did not run. The unittest gate stays boundary-faked
(`tests/test_regression_docker.py` covers the harness's honesty and its explicit-failure paths).

**Consequence.** A runner or image change is checked against real containers before deploy
without making the gate slower or secret-dependent; the harness's honesty rules (no unrecorded
passes, no silent skips) keep its green from being an empty one. It is on demand rather than a
hook, so a forgetful deploy can still ship an unrun check — the runbook says when to run it.

## ADR-0055 — A producer change is piloted on one owner-approved live issue first

**Context.** Batch A's checkpoint (#196) integrated the merged deployment, diagnostics, environment
and size work. The recorded blocker was exact — no Docker daemon and no agent-relay checkout reach a
worker sandbox, so the rehearsal (normal and `--lose-store`), the real-Docker regression harness and
the bare-repo deployment case could not run here — and ADR-0052's hold point leaves a wider gap: it
gates one producer on its own consumer, but nothing stops several producer–consumer pairs from
deploying interleaved, or a producer from shipping to every issue at once. The previous attempt's
finding (`pi-glm-factory: unknown field diagnostics`) is what it costs when the rollout order breaks.

**Decision.** Roll out one change at a time, consumers before their producers (ADR-0052 stood).
A producer change goes live on one owner-approved representative issue first; broader rollout only
after that issue merges with a clean live run behind it. A verification checkpoint never claims
macOS or Hermes live discovery from mocks: implementation merged, runtime verified and host deployed
are named separately, and what a sandbox could not reach is reported with its exact blocker, as
unfinished — not as a pass. Authority, the protected paths and the patch limits (600 lines, 20
files) do not change in a rollout.

**Consequence.** A rollout problem meets one issue, not every WorkItem, and is seen before it is
everywhere. The checkpoint's exact commands and blockers are recorded in
[`verification-checkpoint-batch-a.md`](verification-checkpoint-batch-a.md); the runbook's worker-image
hold point carries the pilot rule.

## ADR-0056 — Verify Day Desk with the installed Hermes interface

**Context.** ADR-0049 used `hermes mcp tools`, a command the installed Hermes CLI
does not implement. Its unit tests invented that interface, so every profile rollout failed.

**Decision.** Use the supported `hermes -p day-desk mcp test <server>` probe and
read only the live server's `tools` configuration with `config get --json`. The probe
prints raw server tools, so apply its include/exclude patterns before checking required
names. Missing names, unreadable output, and unreadable filters keep the deployment
record unchanged. This supersedes ADR-0049's discovery command.

**Consequence.** Tests use the installed CLI's text output and exercise hidden tools
as well as failed probes. Connection success alone cannot advance the deploy record.

## ADR-0057 — Day Desk controls Docker through bounded host tools

**Context.** A stopped Docker engine prevents Day Desk's terminal sandbox from
starting. Giving that sandbox a host socket would grant arbitrary host control.

**Decision.** Add agent-token tools for Docker status and start/stop through
factory-mcp's host process. Commands are fixed argv calls to Docker and OrbStack.
Stop refuses queued/running factory work, unreadable container inventory, and any
running container. Dispatch and control share a process lock. Start verifies readiness.

**Consequence.** Day Desk can recover the engine without a host shell. Stop can be
refused while its own sandbox or another app is running. A later approved issue
may start Docker again through the existing worker recovery. Other host users must
coordinate their container starts with an owner-requested engine shutdown.


---

## ADR-0058 — Jev may order the lessons; code still owns who rides

**Context.** A WorkOrder carries the repo's approved lessons newest-first (ADR-0020), capped at
ten. Recency is the only order code can compute, but a lesson approved months ago can matter more
for today's task than one approved yesterday: judging which past mistake could recur in a task is
a semantic call. TypeSafe's Jev returns a Choice — one option picked from a named set, with a
probability each — which fits "which lesson matters here".

**Decision.** With `FACTORY_JEV_LESSON_RANKING` on and `TYPESAFE_API_KEY` set (config reads both
from the service environment once, and refuses to start with the flag on and the key missing),
each dispatch makes one standard-library HTTPS call to TypeSafe's System One endpoint
(`factory_mcp/jev_rank.py`): one Choice question whose options are the repo's eligible lessons
each described by its full text, plus two abstentions ("none of the lessons applies to this
work", "uncertain"). The state is only the current task — the WorkItem's goal and acceptance
bullets, and this dispatch's exact `findingsToAddress` when it has any. The rubric stays in code.
When the answer names a lesson with probability ≥ 0.5 and a well-formed distribution and
confidence, the lessons ride in the answer's probability order, the newest-first order breaking
ties; `documents.workorder` still caps at ten after ranking. Everything else keeps the
deterministic selection: ranking off, a missing key, more eligible lessons than the endpoint's
option limit (253 after the abstentions), a request over 60 KB, a timeout, a non-2xx, an
unparseable or malformed answer, an unknown option, either abstention, or a top probability under
0.5. Each stop logs `lesson_rank_fallback` with one fixed non-secret category on the WorkItem —
never request or response text, the key, or a provider error body — and no fallback spends a
worker pass or fails an attempt. One call per dispatch, no retry loop in the tick. Jev sees no
other repo's lessons, no issue comments, no command output, no diffs, and no secret; its answer
can only reorder lessons code already chose.

**Consequence.** An older lesson the model ranks relevant rides above a newer irrelevant one,
which the newest-first order could never do. Nothing else moves: eligibility (ADR-0020), the cap,
the WorkOrder schema, the worker rules, retries, gate decisions and merge authority are
untouched, and a hostile or broken answer can at worst reproduce the old order. The ranking is
off until the owner turns it on after judging it on real labeled tasks — the unit tests prove
control flow, not Jev's quality — and the two floors (`MIN_PROBABILITY`, the confidence shape
check) are where that judgment lands. `FACTORY_JEV_URL` is not a setting: tests point a locally
built Config at a fake endpoint on 127.0.0.1, and the service always calls the documented host.

---

## ADR-0059 — A replacement of a stranded WorkItem is prevalidated against the whole graph

**Context.** ADR-0047 lets a person re-point one WorkItem that a dependency stranded, but when an
issue itself is beyond recovery the owner must close it, open replacements, and re-point every
dependent by hand. #197 journalled such a replacement request; running it wrote issue bodies as it
went, so a refusal discovered halfway left the graph half-changed.

**Decision.** `factory_replace_issue` replaces a WorkItem that is waiting on recovery
(`human_waiting` or `blocked`) and has no worker pass with ordered replacement issues
(extended by ADR-0060 for an original whose past attempts all failed). It reads the
dependency graph of the repo's WorkItems and plans every edge change with a pure function
(`workitem.replacement_plan`, not exposed as a tool) before any write: the first replacement
inherits the original's upstream dependencies, each next one waits for the one before it, and every
direct dependent of the original swaps only its edge to the last replacement, keeping its other
dependencies. A replacement or dependent that already had a worker pass, a replacement that
waits for the original, and a plan that would make a cycle are refused before any GitHub call
changes anything. Then the
changed issues are edited, the original is cancelled and closed, and the run is journalled under its
request ID (guard 7), so a retry of the same inputs resumes instead of running twice.

**Consequence.** Replacing a stranded issue is one Day Desk call instead of a hand edit per issue,
and no refusal can leave a half-changed graph. Dependents that are not factory WorkItems are not
re-pointed, and a replacement may not itself wait on the issue it replaces; both are stated in the
tool's description.

---

## ADR-0060 — A failed original with prior attempts may be replaced when nothing is live on it

**Context.** ADR-0059 refused any original with a worker pass, because a pass meant work was or had
been running. On 12 October 2026 the owner asked to replace an original whose passes had all failed
(#199), while the replacement plan (#233–#235) sat unused because the tool's own guard refused it.
A refusal intended to protect a running worker was blocking recovery of a WorkItem with no worker
running on it at all.

**Decision.** The refusal now names a **live** worker, not any worker pass: a stored active
WorkOrder (`activeWorkOrderId`), a stored live relay task (`providerTaskId`), or a state whose next
step dispatches or polls one (`dispatching`, `implementing`). A per-WorkItem lease held elsewhere is
refused by the existing lease check, so a concurrent tick cannot race the replacement.
The failed passes stay on the cancelled WorkItem: the replacement no longer wipes `attempts`, so the
history reads on through the recovery. The untouched-replacement, untouched-dependent and
cycle checks, the lease, and the request journal are unchanged.

**Consequence.** An original that failed with former attempts and no worker running is replacable in
one call; and an owner may only react to a live retry after it has stopped. Deployment: the owner
confirms which factory-mcp version is running (the guard is in the running server, not the merged
file) and reads the tool description back before applying the existing #233–#235 plan. Merged code
is not live proof.

---

## ADR-0061 — The pause barrier is enforced at the submission point

**Context.** #233 gave recovery a durable pause barrier, but nothing enforced it against
dispatch: a WorkItem a barrier holds could still receive a worker pass, and a barrier recorded
after the dispatch decision would race the relay call.

**Decision.** The Foreman refuses the dispatch decision of a WorkItem named in an active pause
barrier (`store.active_pauses`), and repeats the check immediately before the relay submit — the
last guard before a worker runs. A refusal before the intent is written leaves the WorkItem as it
was, and the next tick retries; a refusal after leaves state `dispatching` with its saved
WorkOrder and no relay task, so releasing the barrier submits it on the next tick with no new pass
spent. No tool writes barriers yet, so only a recovery operation can trip the guard.

**Consequence.** Fail-closed only for the work the barrier names: unrelated WorkItems dispatch as
usual while recovery is incomplete, and a held WorkItem needs no person to resume after release.
The checks run without a store write, so they cost nothing when no barrier exists.

## ADR-0062 — A replacement operation holds its graph from journal to done, not just at the dispatch point

**Context.** ADR-0061 made the Foreman refuse a dispatch decision and a relay submit for work an
active pause barrier names, but nothing recorded a barrier: a replacement operation that is
interrupted or cut mid-run left its replacements ready-labelled and, once the original's
successors had nothing left to wait for, dispatchable — recovery was bargaining against ticks that
amend nothing.

**Decision.** `factory_replace_issue` records its durable barrier now: its journalling call also
writes, in the same exchange, a barrier over the original, every replacement and every work its
plan re-points, before any GitHub write. A resume keeps the barrier its first attempt recorded
(the graph was read before the edits and cannot be derived again) and the explicit release comes
after the journal reaches `done`, so a failed or killed attempt holds the graph until its journal
says otherwise. A call that finds the journal `done` with the barrier still active releases it,
even if it otherwise refuses a cancelled original — a kill between the journal advance and the
release leaves nothing held. `store.pause` takes a role that holds one WorkItem or many.

**Consequence.** After a restart, rebuild and discovery take the ready-labelled members in but
dispatch none until the operation completes and releases, while work the barrier does not name
runs as usual. The dispatch and submission checks stay where ADR-0061 put them: no new guard was
needed on the tick paths, only barriers for them to read. Merge authority, gate secrets and patch
limits are untouched, and no new tool is exposed.

## ADR-0063 — A repeat of the same replace call is the way out of an interrupted run

**Context.** ADR-0062 released a barrier whose journal was already `done` (the kill between the
journal advance and the release), but an earlier window stayed: a kill — or a store error on the
cancel write — between the original's close and the journal's advance left the journal at
`pushing`. The next tick reconciled the closed original to `cancelled`, the retry refused it for
good, and every member the barrier named stayed paused with no tool to release it. The same
deadlock reached a person's route: closing the original or a replacement during the run refused
the retry on the state and the ended checks, while a different request conflicted with the
journal (guard 6, ADR-0059).

**Decision.** `factory_replace_issue` re-reads its request journal where it re-reads the WorkItem.
A journal at `pushing` with an active barrier is a resume in two forms: when the original is still
the one the first attempt left (waiting, or cancelled with its own `replaced by` reason), the call
replays the remaining writes — each edit and the close is the same call again, so it is a no-op
once it has happened — and finishes as the first attempt meant to. When instead a person moved a
member in the meantime (the original by a resolve or the tick's reconcile, or a replacement
closed, or one that a worker pass ran on), the call is a takeover: it takes the journal to `done`,
logs `replace_abandoned` on the WorkItem, releases the barrier and returns the WorkItem it found.
The person's route then stands and the barrier holds nothing; the members go by their own
dependencies, a dependency that ended and does not block. A journal at `started` follows the same
rule, since the run there has written nothing yet anyway. Inputs still fingerprint into the
journal: only the same call resumes; a different one conflicts (guard 6), as before. A call whose
journal is already `done` ends there too: it returns the WorkItem it found and records no new
barrier, so a graph can never be held over an operation that will not run again.

**Consequence.** A barrier is never unreleasable: its operation's journal advances or the same
call's repeat clears it within one retry, without hand-editing the store. No new tool, no schema
change: the journal, barrier and release stay what ADR-0062 made them, and the takeover is
visible in the audit log. Recovery is safe against slips in the takeover, too: no worker runs
against a graph a person moved, because the release only ends the hold, and the members then go
by their own dependencies. Merge authority, gate secrets, patch limits and the dispatch/submission
checks are untouched, and no replacement tool is activated.

## ADR-0064 — A replacement the factory opens is created factory:proposed and adopted by its marker

**Context.** ADR-0059's `factory_replace_issue` takes only replacement issues that already exist,
so recovery meant opening each issue by hand (or with `factory_propose_issue`) before the call.
Round 0 of the work put the creation on the tool itself, and immediately hit the classic window:
GitHub may make the issue and then lose the response, or the process may die between the create
and any save under the request journal, and a blind retry would open a duplicate while recovery
was already against a partly formed graph.

**Decision.** A `replacements` entry may now be a `{"title", "body"}` plan item, checked exactly
like a start_plan item (`_plan_problem`: title, acceptance bullets, protected paths; a planned
item also may not carry its own `Depends on` lines, because the plan gives it the chain, the
original's upstream and each earlier replacement). The tool opens it labelled `factory:proposed`
only, never `factory:ready`, with a durable identity marker
`<!-- factory replace <requestId> <position> -->` that names the journalled request and the item's
position in the `replacements` order. Before it creates, the call looks for that marker among the
repo's open `factory:proposed` issues and verifies the marker, the title and the body the request
named before it adopts that exact issue number; two issues with the same marker refuse, and one
whose words were changed after the fact refuses rather than being silently overwritten. Creation
happens after the request journal opens and before the pause barrier is recorded: the create
touches only the new issue, and a proposal can never dispatch, so the pause-before-write
invariant holds for every member a barrier must protect. The barrier then names the created
numbers among the replacement role, so a kill after the puts holds them like the rest of the
graph until the journal reaches `done` and the release runs. A planned item whose words a person
edited during a downed run refuses; fixing it or closing it, then the same call, is the route a
person takes — the takeover from ADR-0063 stays what it was.

**Consequence.** Replacing a stranded issue is one call even when the replacements do not exist
yet, and a response lost mid-create or a restart of the same call recovers the exact issue
already created, with no duplicate. The proposals stay proposed across the whole operation and
after it: workers run on them only when a person approves each one (factory_approve_issue), so
readiness arrives after the cleanup completes. Disposal: a created proposal that a run never
reached stays an ordinary factory:proposed issue on GitHub, for the owner to use or close.
Merge authority, gate secrets, patch limits, the journal and the barrier roles are untouched.

## ADR-0065 — The dependent rewiring is verified by exact readback

**Context.** Round 0 also left the dependent rewrites trusting their edit responses: a lost
response left forever unknown whether GitHub holds the new edge, a drifted body (GitHub rewrote
the text, or a person pushed an edit) would stand as if the call had written it, and a retry had
no way to tell the two apart.

**Decision.** After each plan-edge edit the call reads the issue back and compares the whole body
with exactly the body it computed (`workitem.with_dependencies` of the pre-edit body and the
plan's edges). A mismatch refuses while the pause barrier and the `pushing` journal stand, so
nothing on GitHub beyond that one issue can move during it. A replay of the same journalled call
recomputes the plan from the paused graph, re-reads each affected body and sets the whole
`Depends on` list again, so a replay never doubles an edge and never loses an unrelated one.

**Consequence.** An interrupt anywhere between the dependent rewrites, or a response that arrived
unverifiable, leaves the run exactly where a mid-run kill leaves it, and the same call carries it
the rest of the way. Recovery of a cancelled original is verified on GitHub before the call
reports the operation done, and a person whose edit a replay would disturb fixes their words
before the call runs again.

## ADR-0066 — The original retires before the replacement can run, and only an unmerged PR is ever closed

**Context.** ADR-0065 left the operation trusting GitHub for the rewiring but acting on the
original blind: it closed the issue and let the release run without first reading what its own
PR, when a worker opened one, was doing. A PR that a person or the gate had already merged was
no obstacle, so the replacement could run against work GitHub said had landed, and an original
PR could never be retired at all.

**Decision.** Cleanup precedes activation. After the verified rewiring, the call reads the
original's PR (`wi.prNumber`) back from GitHub before any cleanup write: a PR whose head is not
the WorkItem's own branch in its own repo is an unverifiable original state, and a merged PR is
the original already delivered. Each stops the operation with the pause barrier held and the
journal at `pushing`, so no replacement or dependent can dispatch or intake while a person
resolves it (resolve the WorkItem, or close the replacement issues, and the same call's takeover
route releases). An unmerged PR is closed once and the close is verified by readback that repeats the merge
check: the gate or a person can merge in the window between the run's read and the readback, and
a merged PR does read back closed, so a readback of state alone could wave the merge through.
The issue
close after it is verified the same way. A lost close response refuses, and a replay of the same
journalled call re-checks what GitHub holds and writes only what still reads open, so closes are
never doubled and the store's `cancelled` write, the `done` journal and the barrier release
follow only after every cleanup readback agrees. The store's record never pre-serves the
activation and is checkpointed into it after it agrees.

**Consequence.** The original can never leave a merged work history behind a running
replacement, and a person who already delivered the original stops the operation for good
instead of a fresh worker redoing it. Merge authority and the gate's tools stay untouched: the
replacement tool only ever closes, never merges a PR, and only the PR the WorkItem itself
records. The already-journalled replay machinery carries any interrupt inside the cleanup, so
the boundary between cleanup and activation is restart-safe without a new journal field.

---

## ADR-0067 — factory_replace_issue runs only on the owner's approval, under one stable request ID

**Context.** ADR-0059's `factory_replace_issue` sat in the agent tool list without an approval
rule of its own: a driving Day Desk call that had a plausible plan could cancel the owner's work,
retire its PR and open the factory's own replacement issues on its own say-so, exactly the kind
of decision ADR-0030 reserves for the owner. And because the caller, not the factory, chose how
to repeat the call, a chat that lost its thread could start a second replacement run under a
fresh identity where the journal of the first one was waiting to be replayed.

**Decision.** The tool takes an approval `note`, a stable `requestId` and (Day Desk only)
`ownerAsked`. A driving call without `ownerAsked` refuses: replacing is the owner's decision.
`ownerAsked` stays a Day Desk flag, as in `factory_resolve_human` (ADR-0043): the owner's own
call carries its approval as the note and leaves it unset. The skill's guidance must show the
owner, before they approve, the original's acceptance, each replacement's acceptance, which
dependents move, and the passes each replacement may spend. The journal for a replacement is
keyed by the WorkItem and includes the `requestId` in its inputs, so the same ID with the same
inputs replays where the run stopped (ADR-0063's machinery), and a different ID for the same
WorkItem conflicts and refuses before any write. The skill says every repeat, retries included,
carries the same `requestId`.

**Consequence.** One replacement request is one journalled run under one ID: an interrupted call
resumes and a lost-ID caller stops with a refusal instead of racing its own journal. The guard is
the code for the agent path and the store's fingerprint for the replay path. Verification that
the owner really said the words stays a claim, as in ADR-0030; Day Desk quotes them as the note,
and the audit row holds the note and the ID.

## ADR-0068 — Merged is not complete: a feature ends when its agreed runtime check passes on the deployed feature

**Context.** A multi-issue feature ends when its last implementation issue merges, but nothing in
the plan then proves the feature works end to end: the owner specification's API, browser, platform,
failure and responsive checks belong in the issues' acceptance and the repo's verify, and the
integrated check (gate, deployment, real workers, live interfaces) was until now left to chance. An
owner was told a feature was complete when only implementation had merged. Deployment, hosting and
install were also left implicit, and a simulator was presented as the real adapter.

**Decision.** A plan for a feature ends with a runtime-verification issue that runs the agreed
integrated check — gate, host deployment, real Docker worker, relay rehearsal, live discovery and
audit comparison — after the implementation issues merge. The factory and Day Desk report three
states and never blur them: implementation merged, runtime verified, live deployed. Completion is
merge-observed plus the integrated check's recorded output: until it passes, the report says the
feature is not complete. Deployment, hosting and install steps are explicit plan work; a preview
address is verified immediately before it is shared; a simulator is never reported as the real
adapter. Rollout lands one change at a time — deployed consumers before worker producers
(ADR-0052's rule) — and the first live run is one owner-approved representative issue; broader
rollout follows only after that issue closes with the live discovery and audit confirmed by
reading the host, not inferred from mocks. A host check a worker cannot reach is recorded as an
explicit incomplete blocker, never omitted.

**Consequence.** Nothing reports a whole feature complete on the strength of a unit suite. The
completion report names exactly which of merged, verified and deployed each state is, and missing
live access stands as a blocker the owner clears. Merge authority, the protected paths and the
PR size limits are unchanged by any of this.

## ADR-0069 — Plan-level recovery is one owner-approved, counted allowance

**Context.** Day Desk answered each trivial escalation one owner reply at a time, and its only
route around that was setting `ownerAsked` per attempt, which claims words the owner never said.
Recovery is bounded today, but every use still costs the owner a decision.

**Decision.** The owner approves, once per plan id and durably in the store, a recovery
allowance: which recovery actions Day Desk may take (`retry`, `replace`), and how many uses it
buys (at most `MAX_ALLOWANCE`); nothing unlimited is approved. Within it, Day Desk retries a
waiting WorkItem on the decision evidence (`factory_use_allowance`) and may replace with open
issues the repo already has (`factory_replace_issue` with `allowancePlanId`), without setting
`ownerAsked`. Every use is one committed count against the allowance with the sanitized evidence
in the audit; a replay of a journalled request never pays a second use. The safety stops stay
with a person whatever the allowance says: a HUMAN verdict, a protected path and access trouble
refuse an allowance decision, as does a confirmed environment fault until a person answers it.
An exhausted allowance, or an exhausted per-item budget, sends the WorkItem back to the owner;
the per-item pass budget stays as it is, so an allowance retry buys no fresh budget. A
replacement that opens title-and-body issues is new scope: the owner decides it. With no
allowance approved, everything behaves exactly as before.

**Consequence.** The owner approves one bounded policy instead of repeating themselves, and the
code stops needing a false `ownerAsked` for each attempt. Approvals are visible (`factory_status`
names the plan allowances and the latest use with its evidence), and spending beyond scope
fails loudly with what to do next. Broader or unreviewed agent authority has not changed: the
rollout stays owner-directed (ADR-0068), and the representative first use precedes any wider use.

---

## ADR-0070 — The 600-line target is advisory: a modest exceedance warns and never rejects on line count alone

**Context.** The whole-pull-request size check (ADR-0040) rejected a branch at 600 changed lines:
a good, coherent change of 601 lines failed the same way a runaway one did, and fixes on branches
that a previous attempt already carried past the target were refused for size before the gate
could judge their content. The owner asked for small, coherent pull requests to stay guidance —
to permit a modest exceedance without rejection solely on line count — while every other bound
keeps its force, and asked the counterpart pr-gatekeeper policy to coordinate, not to weaken the
gate's authority.

**Decision.** `maxChangedLines` becomes an advisory target everywhere the factory measures the
whole pull request: the WorkOrder's constraints carry `maxChangedLinesAdvisory` (default true;
absent, as in every WorkOrder written before this ADR, reads as advisory — no stored WorkItem
migrates; a WorkOrder may set it false to restore the old hard bound). In the three producers —
`worker/runner.py` `collect` (before the verify), `Foreman.check_pushed` (what GitHub holds) and
`patches.apply` (the push's own check, for a base that advanced) — a lines exceedance prints a
`factory advisory:` warning naming the actual size, the target and what remains, and the pass
continues; the Foreman records the exceedance in the audit log as `pr_size_advisory` instead of
failing the attempt. The file count, the worker's patch-byte ceiling (`MAX_PATCH_BYTES`, the
distinct resource ceiling: a patch over its bytes is a transferability limit, not a review
standard, and it binds) and the protected paths are independent and unchanged. pr-gatekeeper's
own repo policy is tracked as a separate change against that repository; this repo's `.github/`
gate configuration and the factory standard's statement to customer repos are not changed here.
Small, split pull requests stay preferred in the issue-writing guidance and the worker's rules
text.

**Consequence.** A whole PR of 601 or 610 lines opens, its size named, and the gate judges the
content; nothing reads "600" as a rejection anymore. Whole-pull-request measurement — the
diff from the default branch's merge base, a fix counted with the attempt it follows (ADR-0040) —
is unchanged. The guard's tests move to the file count and to an explicit `false` flag for the
old refusal, so the hard path keeps coverage. Repeated careless exceedance is left to review and
the owner, not to an automatic rejection.

## ADR-0071 — The deploy widens the owner include list; it never guesses at any other filter

**Context.** ADR-0069 added `factory_approve_plan_allowance` to the shipped owner include list in
`tenant/hermes/config.yaml`, and ADR-0048 had made
`mcp_servers.factory_drive.tools.include` deploy-removed from the live profile — but the
owner-server list ADR-0041 keeps in the live profile is not deploy-managed. `hermes profile
update` preserves the live config (it also holds model, Telegram and secret settings), so a live
include list that predates a merged tool stayed short and hid the new tool at exactly the moment
ADR-0049's discovery check tasted it: Day Desk discovered 4 of the 5 owner tools and the deploy
could not advance, with no step that could ever repair it.

**Decision.** When `factory update` has updated Day Desk's profile and before the ADR-0049
discovery check, it widens the live `mcp_servers.factory.tools.include` to the shipped template's
reviewed names: a stable, deduplicated union written in place with `hermes config set` (the
owner's explicit entries first, in their order, then the template's missing ones), verified by an
exact readback of the written key before discovery runs. It is the only live write the deploy
may make. A profile with no include, a null one, a malformed one (not a list of names), or an
include beside an exclude is refused: setting the key there would silently replace an
unrestricted or exclude-only policy with a narrower one, or overwrite invalid config, so the
deploy fails with the key named and keeps the deployment record; the owner sets the list by hand
(RUNBOOK) and the next update verifies it. No wildcard, no force switch, no whole-file rewrite;
the model, provider, Telegram, secret and URL settings, the other MCP servers and the driving
identity are never written, and the approval tool stays on the owner token (ADR-0041).

**Consequence.** A merged owner tool reaches a stale live profile within one deploy, and a
repeated update writes nothing once the lists match. A profile whose filter cannot be parsed is
an operator action, not a silent narrowing; a genuinely absent server tool still fails discovery
(ADR-0049). The readback makes a partial or ignored write fail the deploy instead of letting the
discovery check evaluate a filter nobody verified.

---

## ADR-0072 — The stacked-PR pilot stays factory:proposed until the owner approves activation

**Context.** Issue #227 asks for a guarded two-issue stacked-pull-request pilot after the recovery
work is done, not broad stacked dispatch. Stacked layers touch the merge authority that ADR-0004
gives pr-gatekeeper and the one-issue/one-PR identity that ADR-0002 gives every deliverable.

**Decision.** The pilot lives in `scripts/stack_pilot` and `docs/stacked-pr-pilot.md` as a
specification plus an isolated fixture. The issue stays `factory:proposed`. Activation requires
evidence that the current recovery implementation, integrated verification and the agreed
representative rollout are complete, plus the owner's explicit approval; nothing in the pilot
merges, opens or dispatches anything.

**Consequence.** No stacked behaviour is live. Later implementation arrives as separate reviewed
issues that obey the existing limits, keeps the worker's credential isolation and never bypasses
the gate as the only orchestrator of a bottom-up merge.

## ADR-0073 — The worker image leaves pip's install location to pip

**Context.** The worker image set `PIP_USER=1` so a pass could pip-install as its non-root user.
The env value reaches pip inside a virtualenv too, and pip has no env value that cancels a
boolean flag: an approved chained verify like `python3 -m venv .venv && .venv/bin/python -m pip
install -e . pytest && .venv/bin/python -m pytest -q` died with "Can not perform a '--user'
install", and no rewriting of the verify command by the runner or the issue can fix that. The
env lives in `worker/Dockerfile`, so it is produced by the image only.

**Decision.** The image sets no `PIP_USER`. Plain pip falls back to a user install on its own
when system site-packages is not writable, which is the pass's ordinary non-venv non-root case,
and an explicit `pip install --user` stays explicit. The image keeps
`PIP_BREAK_SYSTEM_PACKAGES=1` and its other env unchanged. Rebuild the worker image after
merging: until it is rebuilt, a pass keeps dying there. No consumer deploys first — the same
image produces and consumes the env — but a repository whose approved virtualenv verify failed
this way (self-maintaining-apis#2) stays blocked until the rebuilt image is live and a real pass
has run its chained virtualenv install and tests.

**Consequence.** Virtualenv installs and ordinary installs both work, no command is rewritten,
and no run logs env values. A test reads the image ENV and runs the chained pattern offline, so
re-adding the flag fails the gate.

---

## ADR-0074 — A setup PR merges unattended only under an owner grant pinned to a digest

**Context.** The owner asks that Day Desk can finish routine repository setup end to end (#263).
The bootstrap tool opens a setup PR whose merge is manual, and rightly so: it installs controls,
and ADR-0019 makes the owner the reviewer of it, while ADR-0004 gives pr-gatekeeper the merge
authority over worker PRs only. Something wider — generic PR merging, shell merges, default-branch
pushes — is out of bounds.

**Decision.** An owner may grant, per repo and nothing else, an authorization to merge that
repo's one canonical setup PR without watching it: `factory_authorize_setup_merge` pins the
digest of the setup plan computed fresh from GitHub at approval time, including the verify
command when the gate config is still missing. `factory_merge_setup_pr` (an agent tool, Day
Desk's) then merges only the open setup PR whose branch, digest, base, head and per-file content
still match that grant exactly, with a green pr-gatekeeper check run and no blocking verdict on
that head; it calls GitHub's merge API with the verified head, so branch protection still applies
at GitHub, journals the request by `requestId` so a retry reconciles the exact same PR and never
duplicates one, and reads the merged pull with its merge commit back before it reports whether
the repo's checks pass. Any difference — moved base or head, changed verify command or control
content, an added code path, missing, pending or failed checks — refuses before any merge call;
a refused or unresolved merge calls GitHub's API and still leaves the PR open, and a merge call
that heard no answer is resumed, not repeated blind. The grant is audited on both ends and
revoked by `factory_revoke_setup_merge`. A repo with no grant keeps the manual merge.

**Consequence.** Setup PRs can complete without the owner in the loop, exactly where and when the
owner granted it, and both normal-PR merging, the worker sandbox and the repo's own branch
protection stay untouched. Readiness is a readback fact, so Day Desk never dispatches against an
incomplete setup; whether it may drive this path at all stays the deployment's decision, not the
model's.

## ADR-0075 — A plan allowance spends only inside its approved membership, and never past a spent budget

**Context.** ADR-0069 recorded a plan approval under an opaque store key the callers quote back:
the approval named actions and a count, but never which repo or which WorkItems it covers. A
spend checked the key, the action and the count, so an approval of one plan could quietly back
recovery of an unrelated repo or a WorkItem the owner never approved, and `factory_use_allowance`
charged the allowance and marked a WorkItem `ready` without checking the per-item pass budget —
dispatch then stopped the item for spent budget while one allowance use was gone. Older stored
approvals carry none of this membership at all.

**Decision.** One approval per plan id names its membership explicitly: the repo the plan covers,
and optionally the exact WorkItems it allows (`factory_approve_plan_allowance` now requires
`repo`). The scope is stored with the approval, shown in `factory_status`, and checked at the
spend itself, in the spend's transaction, for both routes — `factory_use_allowance` and
`factory_replace_issue` with `allowancePlanId` — so a use of one WorkItem cannot pay for recovery
outside the membership. A stored approval without a scope is legacy and refuses every spend until
the owner approves a new scoped plan id: there is no default widening, no one silently aliased to
a legacy record. `factory_use_allowance` also refuses, before the allowance count moves or any
state does, a WorkItem that already used its pass budget: the allowance recovers an attempt,
never buys fresh budget (ADR-0035), and an exhausted item goes back to the owner. A stores
migration widens `allowances` with the nullable scope, writing nothing else and changing no
WorkItem. Nothing in this adds a new grant, a wider include list or any live activation: the
owner still approves every allowance live, per ADR-0069, and the broad chat permission is never
read as fresh budget (ADR-0041, ADR-0046).

**Consequence.** The owner approves a bounded policy that cannot reach beyond what they named, and
Day Desk's spend refuses across repos or members instead of discovering it after the count. An
allowance use is never swallowed by an item no pass could ever dispatch to; the refusal happens
before the token, so a retry of the decision costs nothing already spent. Legacy rows fail loudly
with what to do next, and the two-phase budget — allowance and per-item passes — keeps its old
meaning on every other route.

---

## ADR-0076 — DeepSeek V4.1 Flash joins the factory through a sandboxed pi worker, proven by a provider health pilot

**Context.** The owner asked for DeepSeek V4.1 Flash instead of the current GLM workers (ADR-0026).
Relay's discovery registers a generic `pi-deepseek` agent that runs outside the factory sandbox, and
no confirmed sandboxed DeepSeek factory worker exists, so no production WorkOrder may go to it. The
four restarted #271/#273 pi attempts all died at an inner deadline with exit 124 and no stdout,
with no observed 429/402/quota signal and credits unknown; pi print mode buffers the final response
until completion, so an empty stdout is not inactivity and must never be read as a quota or auth
failure.

**Decision.** A new relay agent `pi-deepseek-factory` runs the same restricted worker path as every
factory worker — `worker/runner.py`'s preflight, its `docker run` sandbox, its credential copies
(`~/.pi/agent/auth.json` mounted only into the container, `PI_CODING_AGENT_DIR` at it), the runner's
patch handoff and the base-commit verify — with `pi --print --provider deepseek --model
deepseek-flash --models deepseek/deepseek-flash`. The model ID is the provider-verified one: pi's
official direct alias `deepseek-flash` for DeepSeek V4.1 Flash. The same ID is never assumed to
hold through another provider; each provider's exact alias is verified with that provider before
use. Nothing in this decision changes a credential boundary: model credentials stay outside coding
workers.

Before any dispatch, `scripts/provider_health.py` sends one short synthetic, non-WorkOrder probe
through that same restricted path and reports `factory.health/v1`: `ok` only when the intended
model answers `--expect`, with latency, exit status and byte counts recorded; provider errors
classified as `provider_quota`, `provider_auth` or `provider_rate_limit`; a `timeout` boundary
reported as `harness_deadline`, distinct from every provider error; and an unavailable access
boundary reported honestly at its own preflight exit code, never bypassed through the host shell
and never restyled into a pass. The report carries only allowlisted fields (timestamp, event type,
tool name, status, exit code, retry count, byte counts); the prompt, response, harness text and any
WorkOrder payload are never printed or archived, and the pilot makes one attempt with no retry and
never grants a pass or budget as a side effect.

**Consequence.** Implementation and deployment stay distinct facts: this repository lands the
documented registration, the pilot and their tests, while adding the agent to `agents.json`, to
`FACTORY_WORKERS`, restarting factory-mcp and running the pilot on the host are the owner's
separate, live steps — and until the pilot returns `ok` once, no production WorkOrder is dispatched
to DeepSeek. The GLM workers stay registered behind the DeepSeek agent, so chat model/provider,
Telegram, settings, secrets and every other profile are untouched, and #271/#273 remain blocked
until the owner explicitly retries; no new pass or allowance is extended to them by this change.
A worker whose sandboxed probe answers with the wrong text fails the pilot loudly
(`unexpected_response`), because a valid response from the wrong model is not a pass either.

---

## ADR-0077 — Day Desk chooses the factory worker and registers one by copying an approved template

**Context.** Each factory worker pins one model in its relay agent args (ADR-0028), so the model is
chosen by which agent runs. The only chooser is a `Worker: <id>` line in the issue body (ADR-0029),
which the owner writes by hand with `factory new -w` or an edit; Day Desk has no tool to pick a
worker, so it cannot steer work to a model the owner asks for in chat. Nor is there any sanctioned
way to add a worker: the relay has no API to register an agent (`POST /v1/agents` is refused), so a
new model means the owner hand-editing `~/.config/agent-relay/agents.json` and reloading the relay.

**Decision.** Two write tools on Day Desk's driving token (ADR-0030), each inside the existing
allowlist and credential boundary:

- `factory_set_worker(id, workers, by)` replaces a WorkItem's issue `Worker:` lines with the agent
  ids given, in order; `[]` clears them so `FACTORY_WORKERS` order decides. Only agents in
  `FACTORY_WORKERS` may be named — an unknown id is refused before the edit, because it would be
  refused at dispatch and the relay holds unsandboxed agents an issue must never reach. It is
  refused while a worker is live and on a terminal WorkItem; unlike dependencies it may change
  between attempts, which is how a retry is steered to another model. The order lives in the issue
  body, so this edits the issue and the next tick reads it.
- `factory_register_worker(worker, model, by)` adds a worker to the relay's `agents.json` by copying
  the approved template (`FACTORY_WORKER_TEMPLATE`, default `pi-glm-factory`) and pinning only
  `model`. The command, credential mount, env, cwd, allowedRoots, image and timeout are the
  template's, and the provider stays the template's, so a registered worker cannot reach anything a
  factory worker could not already reach. It refuses a duplicate id and an id or model outside a
  safe name, writes the registry atomically, and reloads the relay. It needs `FACTORY_AGENTS_FILE`
  set; without it the tool is refused rather than guessing a path.

**Consequence.** Registering is not promoting: `factory_register_worker` never writes
`FACTORY_WORKERS`, so a registered worker receives no WorkOrder until the owner lists it there and
its provider-health pilot (ADR-0076) is green. The registry file is the relay's own, so factory-mcp
now writes one file outside its own state; that write is constrained to a template copy and an
atomic replace, and a relay that does not answer the reload discovers the worker at its next start.
The GLM workers, Day Desk's model, Telegram settings, secrets and every other profile are untouched,
and no budget, pass or allowance changes. A model still never merges: these tools choose and
register workers, and every merge still goes through pr-gatekeeper's verdict.

---

## ADR-0078 — A verification failure survives the timeout that follows it, bounded and redacted

**Context.** #271 ran as timeout → verify with 629 tests, 3 failures and 7 errors → timeout →
timeout. The Result's verification phase kept only the head of the log, which is the deployment
preamble, so one assertion line survived and the other failures and errors did not; the next attempt
then carried only the latest timeout. The original #271 patch is not accessible from this checkout,
so its exact behaviour is unavailable and is recorded here rather than reproduced or guessed at.
The loss has two causes: the runner truncates from the start of the verify output, and the retry
finding keeps only the latest attempt, so an intervening timeout with no output buries a still-fixable
test failure. The harness's own in-session test runs are not the runner's post-harness `verify`, and
only the latter is the evidence the factory reads.

**Decision.** Two narrow changes, with no orchestration rewrite:

- On a failing verification, `worker/runner.py` builds a bounded, deterministic digest of the output:
  one line per unittest-style `FAIL:`/`ERROR:` identity, in output order, each with the last
  meaningful traceback line, an explicit `<truncated: n more failures/errors omitted>` marker when
  more than a fixed number are dropped, and a fall back to the end of the whitespace-collapsed
  output when no identity is found. It is redacted like every other tail before it is serialized,
  and the same digest is used for the phase tail and the test evidence tail. The full verify output
  is not archived: the fallback's end is where a pytest or linter summary is, not the deployment
  preamble at the head.
- On a retry, `diagnostics.attempt_findings` carries the latest attempt, and when that attempt is
  not itself a failing-test finding, the most recent earlier attempt that is, chronologically, so
  the last actionable verification finding rides beside a separate latest timeout. The Foreman
  reads those attempts from the durable `attempt_failed` audit, not only the WorkItem's `attempts`
  list: a person's retry clears that list to reset the pass count, and the finding must survive
  that boundary. A successful push ends the round, so attempts before it do not resurface. The
  control is the existing `diagnosis: failing test:` wording in the attempt string; nothing is
  invented, and the carried text is the same bounded, already-sanitized attempt.

**Consequence.** A later timeout no longer erases the test identities a pass has to fix, while the
latest cause still names the current state. Existing Results and WorkOrders stay valid: the Result
schema, the `diagnostics` shapes and the WorkOrder finding shape are unchanged. No pass budget,
retry, merge or sandbox permission changes, no automatic resubmission is added, and the worker
still exports no WorkOrder payload, environment, credential or repository source.

---

## ADR-0079 — Changed-code size counts source lines: 700 target, 1000 reviewable ceiling, excess routes to decomposition

**Context.** ADR-0070 made the 600 all-lines count advisory, but every line still counted the
same: a change of 650 source lines with 400 test lines and 100 doc lines was measured as 1150 and
warned as though it were three times the size. The owner asked for one metric the worker
producer, the pusher, factory-mcp and the gate all agree on: 700 changed **source** additions and
deletions as the target, 1000 as the reviewable ceiling, with test and doc lines retained as
separate evidence rather than size. An excess of source over the ceiling is not a failed
implementation; it is a signal that the work must be decomposed, and the owner wants that
recovery proposed rather than a bare question.

**Decision.** `factory_mcp/sizing.py` is the one metric source: `kind(path)` classifies a path as
source, tests or docs, and `measure` returns `source`, `tests`, `docs`, `total` and `files`.
Markdown/reStructuredText files and anything under `docs/` are docs; `tests/` and `test`/`tests`
directories and `test_*`/`*_test` files are tests; everything else is source. The WorkOrder gains
three optional constraints — `maxSourceChangedLines` (default 700, set from
`FACTORY_SOURCE_CHANGED_LINES`), `maxSourceChangedLinesAdvisory` (default true) and
`maxSourceChangedLinesCeiling` (default 1000, from `FACTORY_SOURCE_CHANGED_LINES_CEILING`) — while
`maxChangedLines` keeps its old meaning for a WorkOrder without them, so stored WorkItems do not
migrate. All three producers measure source with `sizing`: `worker/runner.py` `collect` before the
verify, `patches.apply` at the push (including a base that advanced) and `Foreman.check_pushed`
on what GitHub holds. Over the target warns (`pr_size_advisory` now carries the source, test, doc,
total and file totals) and never rejects alone; over the ceiling is a size-only refusal carrying
the stable `source-size-ceiling` marker, never a failing verify.

Over the ceiling, `Foreman._size_recovery` records a pure, acceptance-preserving
`workitem.decomposition_plan` — one ordered slice per acceptance criterion, each depending on the
one before — and moves the WorkItem to `human_waiting` with that plan. Opening the replacement
issues changes the material scope, so the existing recovery guards stand: `factory_replace_issue`
still needs the owner (or an allowance naming open issues only), the journal, graph and dependent
cleanup run only when it is activated, and `passesBefore` and the pass budget are untouched. A
scope, budget, access or security finding never carries the marker and keeps its own fail-closed
path.

**Consequence.** A 659-source/384-test/77-doc pull request is accepted though its total is 1120;
701 and 1000 source warn; 1001 becomes a decomposition proposal instead of a failed attempt. The
20-file bound, the 2 MB patch ceiling, the protected paths, the secret controls, the test
evidence and every merge authority are unchanged. The runner image and the factory standard must
be updated together with this change (the image's `worker/runner.py` and `templates/AGENTS.md`),
and pr-gatekeeper's `.github/pr-gatekeeper.json` still carries `max_changed_lines: 600` on this
branch: aligning the gate's own policy is a separate `.github/` change a person owns, and no
source merge is claimed as a live rollout until the image, the profile template and the gate
policy are deployed together.

---

## ADR-0080 — An installed gate caller is upgraded by its two revision pins, on the same reviewed setup PR

**Context.** `factory_bootstrap_repo` wrote a control file only when the repo lacked it, so a repo
that already had `.github/workflows/pr-gatekeeper.yml` kept whatever gate revision it pinned. The
factory's own caller and self-maintaining-apis pinned an older wrapper and `gatekeeper_ref`, and
their pull requests were reviewed under that stale policy. Editing the installed file by hand, or
rewriting it wholesale from the checkout, would either bypass the owner's review or change
permissions, secrets and the verify command that the repo chose.

**Decision.** `repos.bootstrap_plan` reads the checkout's `.github/workflows/pr-gatekeeper.yml` as
the canonical caller. When the repo already has a file that names both a
`uses: .../gatekeeper.yml@<sha>` and a `gatekeeper_ref: <sha>`, `_upgraded_workflow` rewrites only
those two 40-hex revisions to the canonical ones and keeps every other line (permissions, secrets,
runner, comments) exactly as the repo wrote it. A file that is not such a caller, or already
current, is left alone, so a repo with its own workflow is never silently replaced. The upgrade
rides the existing owner-reviewed setup PR (`factory_bootstrap_repo`), whose idempotent adoption,
stale-head refusal and digest-checked authorization (ADR-0074) already guard it, and `repos.check`
now fails a stale caller and names that fix. The canonical revision is whatever a person merged
into this checkout's `.github/`; the factory never invents or pins a gate revision of its own.

**Consequence.** A stale caller becomes a two-line, reviewable update that cannot widen a
permission or change the verify command, and it can merge only by the owner's hand or an explicit
setup-merge grant. Until the owner ships the corrected gate revision to this repo's protected
`.github/`, the upgrade moves a repo to the revision this checkout carries, not to a 700/1000
policy that does not exist yet; that `.github/` change, and regating an existing pull request after
it, stay with the owner.

---

## ADR-0081 — A worker running out of its clock never blocks the issue, and an escalation is an incident with one answer

**Context.** An audit of the live factory (85 WorkItems, 74 failed attempts, 49 escalations) found
that 35 failed attempts were harness timeouts, not code failures, and that routing cycled
`pi-glm-factory` → `opencode-glm-factory` — one model family — so the third worker,
`pi-deepseek-factory`, never ran (#271); a timed-out first attempt therefore landed on the same
family again and two strikes blocked the WorkItem. 29 of the 49 escalations were such two-strike
blocks. `agent_resolved` spent Day Desk's one answer for the lifetime of a WorkItem, so a
materially new escalation — new causes, a dependency that changed — was refused, and about two
thirds of escalations waited on the owner. And the relay keeps its tasks and its requestId
idempotency in memory only: a relay restart lost accepted tasks, and the poll that found the task
gone charged the attempt a strike, so infrastructure interruptions consumed implementation
strikes.

**Decision.** Four rules, all inside the existing Foreman state machine, the one-action-per-tick
lease and the pass budget, audited per decision (`recovery`) with its outcome
(`recovery_result`), so the audit says the category, the action, the previous and next worker, the
budget as it stood, whether the recovery succeeded and whether it avoided a human escalation.

- **A timeout moves families; it never widens the pool.** After a timeout-class failure (a
  `harness timeout` cause, or a `timed out` / `the harness timed out` reason) the next attempt's
  routing prefers a worker outside every model family that ran here; a family is the model token
  in a worker id, so `pi-glm-factory` and `opencode-glm-factory` are one family
  (`routing.route(..., avoid_families=...)`). The preference only reorders what the locality,
  `Worker:` lines and failover already allow, and no worker may run that could run before.
- **The block waits for the failover the policy covers.** When the attempt that would become the
  second strike follows a timeout-class failure and routing would still choose a worker this
  WorkItem has never tried, one further attempt is granted before the block — once per budget,
  with no new passes and no strike reset; when the choice would be a worker that already failed
  here, the block stands exactly as before.
- **An escalation is an incident, answered once.** The WorkItem carries the incident's identity
  (the action, its normalized reason and its facts); a write with the same identity keeps the
  record and its one-answer flag, a material change (new causes, a differently ended dependency, a
  new gate round) is a new incident and earns Day Desk one fresh answer
  (`agent_may_resolve`). A WorkItem that escalated before this field keeps the once-ever rule
  over its history. Gates stay out of reach: only real writes change an incident, a
  `human_waiting` dependency is re-read on every tick — merged or open resumes the WorkItem with
  no person, a differently ended one re-arms once, an unchanged one does nothing — and a HUMAN
  verdict, the block and the owner's authority are never rewritten by a tick.
- **A lost relay task is an infrastructure interruption.** When the relay no longer knows an
  accepted task, the facts are reconciled first (the tick's own observation, no worker credential,
  no pending remote-verify scratch branch), then the same WorkOrder is re-queued under the same
  idempotency key, recording no attempt; the re-queue is bounded per budget
  (`RELAY_RETRIES_MAX`, reset by every write that starts a fresh budget of passes), and beyond
  the bound or with any uncertainty the ordinary failed attempt stands. A relay call that fails
  mid-answer (a restart) is one `RelayError`, a clean no-op the next tick retries. Coding failures
  keep their finite strikes.

**Consequence.** A harness timeout no longer spends a strike on code it never judged, and the
second attempt starts on another family before any block; an escalation that is genuinely new
reaches Day Desk for one more answer, while an unresolved incident stays with the owner and one
that a dependency change or recovery has made moot resolves itself from facts. The budget still
ends a WorkItem in `exhausted`, the two-strike block still stops a failed budget, every merge,
HUMAN and protected-path authority is untouched, and no new background orchestrator exists: the
rules ride the existing tick, the existing audit and the existing tool calls.

## ADR-0082 — A plan item names what it waits for: `after` turns a plan into a graph

**Amends ADR-0024.**

**Context.** ADR-0024 ordered a plan as a chain: every issue after the first got
`Depends on #<previous>`, so two independent issues could not run side by side until a person
called factory_set_dependencies on each one after the plan was open — one call per issue, only
after the issues existed. The chain also hid the real order: an issue that truly depends on two
earlier ones still showed one line, and the plan the owner approved in chat did not look like the
graph the factory would run.

**Decision.** A `factory_start_plan` item may carry `after`: the 1-based positions of the earlier
issues it waits for. An item without `after` keeps ADR-0024's chain line; `after: []` writes no
Depends-on line, so the issue starts as soon as it is open. Each position is checked with the
plan's other checks, before any issue opens and before the repo is enabled: a whole number,
earlier than its own item, and not repeated — the positions map to the issues this call opens, so
a position that is not earlier could never resolve. One bad position refuses the whole plan.
One `Depends on #<issue>` line is written per position, mapped to the issue this call opened for
that item, and `_wait_for` already holds a ready WorkItem until every line it names merges
(ADR-0042 made dependency lists ordinary), so fan-in needs no change.

**Consequence.** A plan is a graph held in the issue bodies, which stay the order's source of
truth (ADR-0009). Independent issues run in parallel from the first tick with no follow-up
factory_set_dependencies calls, an issue with two dependencies starts only when both merge, and
the owner sees the real graph in the plan before the one approval. The chain stays the default,
so every existing plan and every existing Depends-on line behaves exactly as before.

## ADR-0083 — The weekly report carries first-pass gauges, each stamped with its window and gate pin

**Context.** The factory's only quality number was the autonomy rate: merges ÷ ends. It counts
merges, not how the first pass went, so a builder that needs two fix rounds looks the same as one
that merges first time, and no number compares workers, pass kinds or gate pins. The graph plan
makes later slices judgeable only if the before-state is measurable now, and 74 worker PRs over
three weeks showed first-verdict MERGE falling with PR size while the pin, wall budget and task
mix all moved — confounded samples that need the pin recorded with the window.

**Decision.** `tick_one` records the accepted verdict's `outcome` and `round` on each reconcile
audit row, and `metrics.first_pass` computes seven gauges from the audit history and the stored
WorkOrders: first-verdict MERGE rate, clean one-shot rate (one pass, first verdict MERGE, merged,
no failed attempt, no human state), rounds to merge, accepted-Result rate by pass kind and by
worker family, the timeout share of failed passes, and dependency-wait hours. A pass is a
`dispatch` row; it was accepted when a `result` row carries its WorkOrder. Rows written before
this ADR carry no outcome: the state move infers it (merged was MERGE, changes_requested CHANGES,
a HUMAN says so in its escalation). Every gauge names its `n`, the window in IST and each repo's
gate pin (`gatekeeper_ref` from the gate workflow, best effort, "unknown" when unreadable), so two
windows under different pins never read as one trend. Plans have no id in the store, so
dependency-wait hours are per WorkItem; a plan's nodes are visible by repo and issue range.

**Consequence.** Each graph slice can be judged by the window after it: slice 3 should move
first-verdict MERGE, slice 5 and 6 move the timeout share and one-shot rate, slice 4 shows per
family. The recount on a copy of the live store anchors the baseline. Old history is inferred, so
the gauges are honest about their own blind spot: an old HUMAN escalation that does not name the
verdict is not counted as one.

## ADR-0084 — A fix pass receives the gate's whole answer: each finding's fix and test, and the marker's problems as blocking findings

**Context.** The factory kept only `fingerprint`, `path`, `line` and `problem` from each gate
finding, so the fix pass lost the reviewer's suggested fix and the named test, and a CHANGES
verdict whose problems were pipeline-level (verify, pr-lint, proof of work — the marker's
`problems` list, no findings) had nothing a worker could act on: it stopped for a person. The
reviewer's suggestion re-derived from scratch every round, and the worker never learned which
test the gate wanted green.

**Decision.** `verdict.to_verdict` keeps each finding's optional `test`, and when the outcome is
CHANGES it reads the marker's optional `problems` list (kind, path, problem) as blocking findings
with the usual fingerprint; `foreman.act` copies `fix` and `test` into the fix order's
`findingsToAddress`; the WorkOrder contract's `findingRef` gains optional `fix` and `test`, and
the verdict contract's finding likewise. A CHANGES marker with problems and no findings now
dispatches a fix instead of stopping for a person; one with neither still does (its reason is in
the gate's comment prose, which no worker may read). `worker_input` tells the fix worker the fix
is the reviewer's suggestion and its data — resolve the problem with the smallest change, make the
named test pass, check the same pattern at sibling call sites. A retry on a stuck-round incident
carries one decision line in its note (Day Desk reads the PR diff and the gate findings first).

**Consequence.** The same problem reported as a `problems` entry counts in per-problem round
rounding like any finding, so two rounds of the same pipeline problem still escalate. A marker's
`kind` is dropped: the finding's path and problem text carry what the worker needs. Old verdict
comments re-parse with fix and test null, which validates.

## ADR-0085 — No pass runs in the gate reviewer's model family

**Context.** pr-gatekeeper reviews every factory PR with a model family (its `reviewer_model`,
`deepseek-v4.1-flash` by default, `strong_reviewer_model` on round 1 and sensitive paths). When
the builder shares that family, the gate's review is a model grading its own family's work: the
same blind spots on both sides, and the family-level gauges (ADR-0083) cannot separate builder
quality from reviewer leniency. The factory already avoids a family after a timeout (ADR-0081),
but only as a soft preference: with no other worker eligible it falls back inside the family.

**Decision.** `routing.route` gains `exclude_families`, a hard filter applied after the issue's
`Worker:` lines and before the ADR-0081 preference. `routing.family` also reads a model id
(`deepseek-v4.1-flash` → `deepseek`), so the gate's config and worker ids compare in one space.
Foreman's `_dispatch` and `_defer_block` read `reviewer_model` and `strong_reviewer_model` from
the repo's `.github/pr-gatekeeper.json` on the default branch with `github.file` — the gate's
config: the factory reads it, never writes it. A missing key means pr-gatekeeper's default
(`deepseek-v4.1-flash`); a repo with no gate file has no reviewer and routes as before. When
every candidate a dispatch could reach is excluded, the WorkItem waits for a person with
`every allowed worker is in the reviewer family <f>` instead of running. The ADR-0081 deferral
takes the same wall, so a timeout cannot launder a pass into the reviewer family.

**Consequence.** With the default gate config, `pi-deepseek-factory` cannot take a pass on any
gated repo while DeepSeek reviews — the RUNBOOK's deepseek pilot and this wall agree. An issue
whose `Worker:` line names only excluded workers needs the owner to edit the line or the gate
config. A repo the gate does not review keeps its `FACTORY_WORKERS` order untouched, which is why
the exclusion is read per repo at dispatch time rather than configured globally.
