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

**Superseded by ADR-0013** for the front door. Decisions stay deterministic.

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
