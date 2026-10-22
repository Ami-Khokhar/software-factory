# How the factory works

For anyone changing this repo. Running the factory is in [RUNBOOK.md](RUNBOOK.md), the four
documents the parts exchange are in [contracts.md](contracts.md), and the reasons behind the design
are in [decisions.md](decisions.md).

## Components

```
                      Day Desk's shell (Docker sandbox) ──► GitHub: git, gh, one token
                          ▲
Owner ── Telegram ──► Day Desk (Hermes, tenant/hermes)        factory CLI (scripts/factory)
                          │ MCP tools: drives with its token; setup asks you  │ MCP tools
                          ▼                                                   ▼
            ┌────────────────────────────── factory-mcp ──────────────────────────────┐
            │ the Foreman: for each WorkItem, take the lease, observe GitHub,         │
            │ reconcile, then act at most once. SQLite: WorkItems, WorkOrders, the request journal, audit. │
            └───────┬─────────────────────────────┬──────────────────────────────┬────┘
          HTTP tasks│                  REST, git  │                         read │
                    ▼                             ▼                              ▼
             agent-relay ──► worker/runner.py     GitHub: issues, labels,   pr-gatekeeper:
                             (Docker sandbox,     PRs, check runs           reviews, merges,
                              any harness)                                  comments a verdict
```

- **factory-mcp** (`factory_mcp/`) is the Foreman: a Python MCP server, standard library only, on
  the owner's Mac. It holds each WorkItem's state and decides every transition and every side
  effect. A background loop ticks every WorkItem every `FACTORY_POLL_SECONDS` (60).
- **agent-relay** ([repo](https://github.com/Ami-Khokhar/agent-relay)) runs worker tasks.
  factory-mcp submits one task per WorkOrder over HTTP and reads it on later ticks; it never waits.
  Each call carries the relay's token, which factory-mcp reads on every call from
  `AGENT_RELAY_TOKEN` or the file the relay writes on its first start. Timeout overrides read
  `/healthz`'s `limits.maxTimeoutMs` (positive global cap or null), verified against relay
  `3ecf6b72973a5d9dbebb92bb8bfeb9a90f41802c` by `scripts/rehearsal.py`. An explicit task
  timeout overrides the registered agent's default `timeoutMs`; it cannot exceed the global cap.
- **worker/runner.py** is what a relay agent runs: the agent's harness (pi, Codex, OpenCode, ...)
  works in a Docker sandbox with copies of the login files the agent names and no GitHub
  credential, and the runner turns its edits into a patch Result (ADR-0026, ADR-0028).
- **GitHub** holds the facts. factory-mcp reads issues, labels, PRs, check runs and the gate's
  comments, and rebuilds its WorkItems from them when its store is lost (ADR-0009).
- **pr-gatekeeper** reviews every PR and merges it on MERGE. factory-mcp has no merge tool
  (ADR-0004).
- **Day Desk** is the owner's front door in Telegram (ADR-0013), and it drives the factory: with
  `FACTORY_AGENT_TOKEN` it plans, starts issues and answers each escalation once. Active Project
  members may be answered repeatedly (ADR-0102). factory-mcp lists only `tools.AGENT_TOOLS` for
  that token (ADR-0030). Within an owner-approved plan
  allowance (ADR-0069, scoped membership per ADR-0075) it retries on evidence and replaces with
  open issues the allowance's approved membership covers, counted, without asking once per
  recovery; safety stops and confirmed environment faults still need a person. Its bounded Docker tools
  check/start/stop the host engine without the terminal sandbox (ADR-0057). The `factory` CLI calls the same
  tools. Day Desk's shell runs in a Docker sandbox whose one secret is a GitHub token (ADR-0021).
  For a new repo, one owner-approved tool prepares a setup PR with only missing controls, after it
  pushes one README-only commit to an empty repo's default branch; the owner merges the PR before
  normal worker passes start (ADR-0019), or grants an auditable, revocable per-repo authorization
  and then only that PR may merge without a person, digest-checked and read back (ADR-0074). An
  installed gate caller that pins an older gate revision is upgraded on the same PR by rewriting
  only its two revision pins, so its verify command, permissions and secrets stay untouched
  (ADR-0080). One
 owner approval of `factory_start_plan` opens all the
  issues of a plan, and they run in order (ADR-0024); `factory_set_dependencies` then frees the
  independent ones before a worker starts them, and re-points one stranded on a dependency that
  ended (ADR-0042, ADR-0047). Day Desk starts a Project with one tool call after the owner's
  yes, quoted: `factory_start_project` stores its goal, budget and blueprint, opens the plan's
  issues in order and makes each a member (ADR-0100).

## What crosses each boundary

| From → to | What crosses | Defined in |
|---|---|---|
| factory-mcp → worker | A WorkOrder, inside the task input, after the worker rules | `schemas/workorder.schema.json`, `documents.worker_input` |
| worker → factory-mcp | A Result: the last JSON line of the task output | `schemas/result.schema.json`, `documents.check_result` |
| pr-gatekeeper → factory-mcp | A Verdict, read from the marker JSON in the gate's latest PR comment | `schemas/verdict.schema.json`, `verdict.py` |
| factory-mcp → GitHub | The `factory/<issue>` branch, a PR in the gate's body format, labels, comments | `patches.py`, `github.py`, `documents.pr_body` |
| Keychain → factory-mcp → GitHub | A gate secret's value: `security`'s stdout, then `gh secret set`'s stdin, with the factory's token; never logged or returned | `gate_secrets.py` |
| Day Desk, CLI → factory-mcp | MCP tool calls, each one marked read or write | `tools.py` |
| Day Desk's shell → GitHub | git and gh traffic from the Docker sandbox, with a fine-grained token for the factory's repos and no Administration; no other secret or host file | `tenant/hermes/config.yaml`, `tenant/hermes/sandbox/Dockerfile` |

## Code map

| File | What it does |
|---|---|
| `factory_mcp/__init__.py`, `__main__.py` | The package; `python3 -m factory_mcp` starts the server |
| `factory_mcp/server.py` | Streamable HTTP on loopback: `POST /mcp` with the bearer token, `POST /worker/ask` with a pass's own token (ADR-0098), the background tick loop |
| `factory_mcp/mcp.py` | The MCP protocol: one JSON-RPC message in, one response out, no I/O |
| `factory_mcp/tools.py` | The tools, and whether each one reads or writes |
| `factory_mcp/foreman.py` | The Foreman: intake, rebuild, tick, dispatch, poll a task, open a PR, and the person's acts (resolve, approve an issue, start a plan, add a repo, bootstrap a repo, approve or retire a lesson) |
| `factory_mcp/workitem.py` | Intake from an issue, observing GitHub, and `reconcile`: the fact-driven half of the state machine |
| `factory_mcp/documents.py` | WorkOrders (with the repo's lessons), the worker input, the Result check, the PR body |
| `factory_mcp/diagnostics.py` | What a failed attempt means, read from the Result's optional diagnostics (ADR-0050): one evidence-backed cause, or an unknown one, with sanitized bounded context. The next WorkOrder carries the latest attempt and, when that one is not actionable, the last failing-test finding an intervening timeout would otherwise bury — read from the durable attempt records, so it survives the person's retry (ADR-0078) |
| `factory_mcp/verdict.py` | Reading the gate's verdict marker; finding fingerprints and blockers |
| `factory_mcp/store.py` | SQLite: WorkItems with optimistic concurrency, WorkOrders, the durable request journal, pause barriers holding a recovery graph until released, the audit log, leases, migrations |
| `factory_mcp/projects.py` | What the Lead's Project fields must hold to store one (ADR-0099): the budget, blueprint and doneWhen limits, and the first limit a field set breaks |
| `factory_mcp/github.py` | The GitHub REST client |
| `factory_mcp/relay.py` | The agent-relay task client, and where it finds the relay's token |
| `factory_mcp/patches.py` | The git work factory-mcp does: cloning a worker checkout, pushing a patch Result, and the owner-approved bootstrap pushes (`initial_commit`, `bootstrap`, `update_bootstrap`) |
| `factory_mcp/repos.py` | The repo check, the bootstrap plan (`bootstrap_plan`, `plan_on`) that installs missing controls and upgrades stale gate revision pins, and `repos.json` (ADR-0014, ADR-0080) |
| `factory_mcp/setup_merge.py` | The owner-authorized merge of a repo's one canonical setup PR, digest-checked, fail-closed, journalled by request ID, with readback and readiness (ADR-0074) |
| `factory_mcp/gate_secrets.py` | A repo's gate Actions secrets, set from the owner's Keychain through `gh` (ADR-0022) |
| `factory_mcp/lessons.py` | Lesson candidates from the audit log, and `lessons.json` (ADR-0020) |
| `factory_mcp/jev_rank.py` | The opt-in Jev call that ranks a repo's lessons for one dispatch, with its fallbacks (ADR-0058) |
| `factory_mcp/routing.py` | Which worker takes the next attempt: locality first, then one that has not failed |
| `factory_mcp/metrics.py` | The escalation text, the autonomy rate, whether each lesson works, and the weekly report's numbers |
| `factory_mcp/runtime.py` | Sanitized, read-only runtime facts and observational MCP schema-cache diagnostics (ADR-0091) |
| `factory_mcp/worker_ops.py` | Validates and runs one bounded sanitized provider-health check, stores its binding/quarantine record, and calls the relay's fixed registry-reload endpoint (ADR-0094) |
| `factory_mcp/config.py` | Settings from the environment and `repos.json` |
| `worker/runner.py` | Runs one WorkOrder in the sandbox and prints its Result |
| `worker/Dockerfile` | The sandbox image: git, Python, Node, pinned pi, Codex and OpenCode, and no GitHub credential |
| `scripts/factory` | The owner's CLI: `new`, `add-repo`, `status`, `watch`, `report`, `retry`/`regate`/`close`, `start`/`stop`, `update`, `serve` |
| `scripts/install-services.sh` | The launchd services for agent-relay and factory-mcp |
| `scripts/rehearsal.py` | One issue end to end, with the real agent-relay and a stand-in GitHub. Its scripted worker hands back a patch, as `worker/runner.py` does, and factory-mcp pushes it |
| `scripts/provider_health.py` | The provider health pilot (ADR-0076): one synthetic non-WorkOrder probe through the same restricted sandbox path as the workers, whose `factory.health/v1` report carries only allowlisted timestamps, event types, tool names, statuses, retry counts and byte counts — never the prompt, response or harness text |
| `scripts/regression_docker.py` | The runner's real-container regression harness: real `docker run` sandboxes for runner success, failing verification, timeout, secret redaction and isolation, preflight failure codes, early size rejection and cleanup. Explicitly invoked; a missing Docker daemon or worker image fails loudly instead of passing |

## One tick

A tick comes from the background loop or from `factory_tick`.

1. **Rebuild.** First, for each trusted author, one GitHub search for their open `factory:ready`
   issues in any repo; a repo not listed yet is enabled when it passes the repo check, and an issue
   in one that fails gets one comment with the fixes (ADR-0027). Then every open `factory:ready`
   issue in an allowlisted repo that the store does not
   know is taken in, so a lost store fills again from GitHub.
2. **Each live WorkItem** whose repo is allowlisted (#38): take its lease or skip it; observe
   GitHub (the issue, the PR on `factory/<issue>`, the gate's verdicts and check run); reconcile the
   state with those facts and write it, reading once a PR head the factory did not push to see if
   it is the gate's merge of the base branch (ADR-0039); then act at most once. A fine-grained token cannot read
   check runs on a private repo, so the gate's workflow run stands in; when the token cannot read
   that either, the verdicts and `FACTORY_GATE_WAIT_SECONDS` still move the WorkItem:
   - `ready`: dispatch an implement WorkOrder, unless the issue has a `Depends on #n` line and
     #n has not merged: then it waits, with `waitingOn: n` (ADR-0024). `changes_requested`:
     dispatch a fix WorkOrder that carries the gate's blocking findings as data.
   - `implementing` with a remote-verify WorkOrder: the first tick after the Result pushes the patch to
     the scratch branch and stores `remoteVerify`; later ticks look for the finished `factory-verify`
     run (no more often than the tick) and, after `FACTORY_REMOTE_VERIFY_SECONDS`, fail the attempt.
     The scratch branch is deleted when the run ends. A repo without
     `.github/workflows/factory-verify.yml` goes to `human_waiting` at once, with no attempt used.
   - `dispatching`: submit the task to the relay. The state is written before the call, and the
     WorkOrder's idempotency key is the relay's request id, so a retry never starts a second task.
   - `implementing`: read the task. When it is done, check the Result and what GitHub holds, push
     a patch, and open the PR or adopt the one already open on the branch. A push to an open PR
     rewrites its body, because the gate lints the body against the paths the PR now changes.
   - `pr_open`, `reviewing` or `changes_requested` with a PR that GitHub says conflicts with its
     base: dispatch an implement WorkOrder on the current default branch, with the conflict and
     any gate findings as data; its push replaces the branch, and the gate runs again (ADR-0032).
   - `pr_open` or `reviewing` with no verdict for `FACTORY_GATE_WAIT_SECONDS`: `human_waiting`.
   - `merged`: close the issue, because GitHub skips `Closes #n` when the gate's workflow token
     merges. Then `closed`.
3. **Entering `human_waiting`, `blocked` or `exhausted`** comments on the issue and mentions the
   owner. `factory watch` also tells the owner in Telegram of `human_waiting`, `blocked` and
   `exhausted`, and deduplicates material state, PR, head, gate-round and worker transitions. It
   keeps Day Desk's grace rules, suppresses historical terminal items on its first progress baseline,
   and says that merge or closure does not establish runtime acceptance (ADR-0035, ADR-0043,
   ADR-0046, ADR-0081, ADR-0093).

## Projects

A Project has states `active`, `paused`, `done` and `stopped` (ADR-0105). Starting one appends
an acceptance issue after every product issue, leaving room for it within the plan limit. Its
body runs each `doneWhen` command on the default branch, records the command, exit code and last
40 output lines in `docs/acceptance/<id>.md`, and requires product fixes with regression tests
when a check fails. The checks stay unchanged, real and subject to the gate's review.

At the end of a full tick, `_advance_projects` reads each active Project. A `closed` acceptance
WorkItem makes it `done` and clears `pausedReason`, even if the deadline has since passed. A
missing, `cancelled` or `exhausted` acceptance item pauses it with a reason. Otherwise a passed
deadline pauses it. Version conflicts wait for the next tick, and each other error is logged
without stopping the other Projects. Single-WorkItem ticks and non-active Projects do not advance.
These transitions log `project_done` or `project_paused` and do not change WorkItem ticking.

## State machine

The states are the enum in `schemas/workitem.schema.json`.

| From | Fact or action | To |
|---|---|---|
| (none) | intake of an open issue | `issued`; `planned` if labelled `factory:proposed`; `ready` if labelled `factory:ready` |
| `issued`, `planned` | the issue gains `factory:ready` | `ready` |
| `ready` | the tick dispatches an implement WorkOrder, once each `Depends on #n` issue is closed with a merged PR on `factory/<n>` | `dispatching`, then `implementing` once the relay accepts |
| `ready` | a `Depends on #n` issue is open | `ready` with `waitingOn: n`; nothing is dispatched |
| `ready`, `changes_requested` | a `Worker:` line names an agent that is not in `FACTORY_WORKERS` | the name is dropped and routing picks among the named allowed workers, else all allowed ones, with an `unknown_worker_rerouted` audit row (ADR-0110); `human_waiting` only when none is eligible |
| `ready` | a `Depends on #n` issue closed without a merged PR, ended `cancelled` or `exhausted`, or does not exist | `human_waiting` |
| `human_waiting` on a dependency | each tick re-reads it: merged or open again resumes the WorkItem (`ready`, `waitingOn: n` when open); a differently ended dependency re-arms its incident once; an unchanged one writes nothing (ADR-0081) |
| state that would dispatch | a recovery pause barrier (ADR-0061) holds the WorkItem | nothing is dispatched: the decision refuses, or `dispatching` holds the relay call until the barrier releases |
| `implementing` | the task completed, the Result is valid and the guards pass | `pr_open`, or `reviewing` when the PR already exists |
| `implementing` | the task failed, or the Result or a guard failed | back to `ready` (`changes_requested` for a fix) with an `attempts` entry, routed to another model family when the failure ran out of the clock; `blocked` after 2, with one deferred attempt first after a timeout-class failure while an untried worker remains (once per budget, ADR-0081); a WorkItem whose second failure is a timeout gets one 60-minute run first, unless it belongs to a `paused`, `stopped` or missing Project (a `done` Project's member counts as a non-member) (`auto_longer_run`, ADR-0103, ADR-0108); a diagnosis that names the environment pauses as `human_waiting` on the first attempt (ADR-0051) |
| `implementing` | the relay no longer knows the accepted task | `dispatching` again with the same WorkOrder and idempotency key, no `attempts` entry, bounded per budget by `RELAY_RETRIES_MAX`; past the bound or with a pending remote verify, an ordinary failed attempt (ADR-0081) |
| `pr_open` | the gate's check run, or its workflow run when the token cannot read check runs, exists on the head | `reviewing` |
| `reviewing` | a verdict on the current head: MERGE, CHANGES or HUMAN | `merge_pending`, `changes_requested` or `human_waiting` |
| `reviewing` | the gate's check run (or workflow run) finished, not cancelled, with no verdict | `human_waiting` |
| `changes_requested` | the same blocking problem in 2 gate rounds (ADR-0005) | `human_waiting` |
| `reviewing`, `changes_requested` | a CHANGES verdict that names no finding, for example when only the gate's PR lint failed | `human_waiting`: a fix pass would have nothing to fix |
| `changes_requested` | the tick dispatches a fix WorkOrder | `dispatching`, then `implementing` |
| `pr_open`, `reviewing`, `changes_requested` | the PR conflicts with its base; the tick dispatches an implement WorkOrder on the current base | `dispatching`, then `implementing` |
| `merge_pending` | GitHub reports the PR merged | `merged` |
| `merged` | factory-mcp closes the issue | `closed` |
| any live state | the PR closed without a merge | `human_waiting` |
| any live state | the issue closed without a merged PR | `cancelled` |
| `ready`, `changes_requested` (a non-member, or a member of a done Project) | `FACTORY_MAX_PASSES` WorkOrders were dispatched since the last person's retry, and the gate's round has already used its free fix pass (ADR-0112) | `exhausted` |
| `ready`, `changes_requested` (a member) | the Project's budget is spent | `exhausted`, and the Project is `paused` |
| `ready`, `changes_requested` (a member) | the Project is `paused` or `stopped` | unchanged, with no dispatch or audit write |
| `ready`, `changes_requested` (a member) | 8 WorkOrders were dispatched over the member's lifetime | `exhausted`; the Lead splits it or the owner decides |
| `human_waiting`, `blocked` | `factory_resolve_human`: retry, regate or close; for a member of an active Project, Day Desk answers as often as the budget allows, except a safety stop or environment fault (ADR-0102) | `ready` or `changes_requested`, `reviewing`, or `cancelled`; a regate with no gate run going and no new verdict edits the PR body's `<!-- factory regate n -->` line, which starts a gate run (ADR-0034) |
| `exhausted` | the owner's `factory_resolve_human`: retry or close; for a member of an active Project, Day Desk answers as often as the budget allows, except a safety stop or environment fault, and cannot retry, close or regate a member exhausted by its lifetime pass cap (ADR-0102); otherwise Day Desk needs `ownerAsked` (ADR-0043, ADR-0046) | `ready` or `changes_requested` with a fresh budget of passes for non-members, or `cancelled`; a member keeps its lifetime cap and Project budget (ADR-0035, ADR-0101) |

`closed` and `cancelled` are terminal. `exhausted` waits for the owner's retry or the Lead's
recovery of an active Project member within its lifetime cap (ADR-0102). `human_waiting` waits
for a decision `factory_resolve_human` makes, Day Desk's once per incident or the owner's
(ADR-0030, ADR-0097), with repeated answers for active Project members (ADR-0102).

## The worker contract

- The Foreman sends the task with the repo's worker checkout (`FACTORY_CHECKOUT_ROOT`) as its
  working directory, and runs up to `FACTORY_PASSES_PER_REPO` (2) passes of one repo at once.
  The runner gives them the checkout in turns, and the time a pass waits for it comes out of the
  harness's share of the pass (ADR-0036).
- `worker/runner.py` fetches in the checkout, makes a fresh copy with no remote, and puts it on
  `factory/<issue>` at the commit to build on: the default branch for an implement WorkOrder, the
  reviewed head for a fix.
- The harness runs in the `factory-worker` container with that copy and a copy of its model key,
  and no GitHub credential. It commits locally. Host git never runs in a tree the harness wrote,
  so a planted hook or filter cannot run on the host.
- A pass that cannot decide from the issue may ask Day Desk (ADR-0098). The WorkOrder carries
  `ask: {url, token}`, and the task input carries a one-line ask block before the WorkOrder that the
  runner splits off before it hands the text to the harness: the values reach the harness as
  `FACTORY_ASK_*` in its environment, never in the prompt. The image's `factory-ask` posts one question to
  `http://host.docker.internal:<FACTORY_PORT>/worker/ask` — the same loopback listener as MCP,
  which Docker Desktop maps to the host — and blocks for a bounded wait. The
  server stores the question on the WorkItem and `factory_answer_question` answers it; the worker
  proceeds with its own reading when nobody answers in time. The token names that one pass, so it
  can ask about nothing else, and it dies with the pass. `factory-ask` answers `-h` and `--help`
  itself with its usage, without contacting the channel (ADR-0111).
- The runner turns the commits into a unified patch, measures the whole pull request — the branch's
  diff against its merge base with the default branch, so a fix is counted with the implementation it
  follows — and checks an oversized one before the gate's `verify` command from the base commit runs
  in the same sandbox: over the file count it refuses the pass; over the line target it warns
  (advisory, ADR-0070) and continues. It then prints the Result as its last line.
- The harness's own test runs are its session's business; the runner's `verification` phase is the
  post-harness run of the same `verify` command the gate uses. When that verify fails, the runner
  keeps a bounded digest of the failing/error test identities and a representative traceback line
  each — not the deployment preamble at the head of the log; when no identity is found it keeps the
  end of the output instead — redacted and bounded, so the next pass sees what to fix even after a
  later pass times out with no output (ADR-0078).
- For a repo in `FACTORY_REMOTE_VERIFY_REPOS` the WorkOrder has `remoteVerify: true` and the runner
  skips that verify step: the Linux container cannot run it. factory-mcp pushes the patch to
  `factory-verify/<issue>-<n>`, where the repo's `factory-verify` workflow runs the same verify
  command on a GitHub macOS runner. A pass there continues as below; a failure is a failed attempt
  whose finding is the run's log tail (ADR-0045).
- factory-mcp checks the Result, refuses a patch that touches a protected path or takes the PR over
  its file-count limit before pushing it (a line-count exceedance warns instead, ADR-0070),
  pushes `factory/<issue>`, checks what GitHub holds, and opens the PR.

## Guards

| Guard | Where | Test |
|---|---|---|
| One tick at a time per WorkItem | `store.acquire` | `test_actions`: `test_lease_holder_excludes_other_ticks`; `test_ops`: `test_a_decision_waits_while_a_tick_holds_the_lease` |
| A stale write loses | `store.put` | `test_store`: `test_stale_version_loses` |
| No duplicate worker task | idempotency key = relay request id | `test_actions`: `test_relay_down_then_resubmit_with_same_key`, `test_duplicate_tick_is_a_no_op` |
| Only allowlisted repos | `Foreman.intake`, `Foreman.tick` | `test_reconcile`: `test_intake_is_idempotent_and_allowlisted`; `test_actions`: `test_a_repo_off_the_allowlist_is_left_alone` |
| Only trusted authors reach a worker | `Foreman._admit` | `test_safety`: `test_only_trusted_authors_reach_a_worker`, `test_logins_ignore_case` |
| A Result meets its contract | `documents.check_result` | `test_schemas`: `test_check_result_accepts_what_the_result_schema_accepts` |
| Only factory-mcp pushes, and only a patch (ADR-0017) | `Foreman.push_patch`, `patches.apply` | `test_actions`: `test_ready_to_pr_open`; `test_gate_loop`: `test_changes_fix_merge_closed`; `test_workers`: `test_unified_and_mbox_patches_push_the_workers_change` |
| Changed paths come from GitHub, protected paths refused | `Foreman.check_pushed`, `patches.apply` | `test_actions`: `test_renaming_a_protected_path_fails_the_result`; `test_workers`: `test_a_patch_to_a_protected_path_is_never_pushed` |
| One PR's size: 700 source lines target, 1000 source ceiling, 20 files binding, tests/docs separate, whole PR measured from the default branch (ADR-0040 with ADR-0079) | `factory_mcp/sizing.py` (the metric), `worker/runner.py` `collect` (whole PR, before the verify — source target warns, source ceiling refuses as size-only), `Foreman.check_pushed` (covers the file count, records `pr_size_advisory` with the totals), `patches.apply` (the push's own check, for a base that advanced), `Foreman._size_recovery` (proposes an acceptance-preserving decomposition) | `test_sizing`: `test_the_accepted_sizes_and_the_recovery_boundary`, `test_it_reports_source_tests_and_docs_separately`; `test_worker_runner`: `test_tests_and_docs_do_not_count_against_the_source_target`, `test_a_source_total_over_the_ceiling_is_a_size_only_recovery`, `test_a_hard_line_bound_still_refuses_when_the_workorder_names_it`; `test_actions`: `test_a_change_must_fit_one_pr`, `test_a_source_ceiling_excess_routes_to_a_decomposition_not_an_attempt`, `test_an_allowance_never_buys_a_planned_decomposition_without_the_owner`; `test_workers`: `test_a_patch_that_takes_the_branch_over_its_limits_is_never_pushed` |
| GitHub holds the pushed commit | `Foreman.check_pushed` | `test_actions`: `test_bad_results_fail_attempts_then_block` |
| One PR per WorkItem | `Foreman.open_pr` | `test_actions`: `test_a_result_for_a_branch_with_an_open_pr_joins_that_pr`, `test_open_pr_adopts_a_pr_opened_after_the_reconcile` |
| A verdict is selected for the exact live PR head despite later comments for another head, and older rounds remain stale | `workitem.current_verdict`, `Foreman.act` | `test_gate_loop`: `test_a_live_head_changes_verdict_survives_a_later_comment_for_another_head`; `test_actions`: `test_stale_verdict_ignored`; `test_reconcile`: `test_old_round_is_stale` |
| Transient GitHub GET resets, rate limits and server errors retry within a fixed wait bound; exhausted transport failures normalize to `GitHubError`; writes never retry | `GitHub._request` | `test_github`: `test_get_retries_bounded_rate_limits_and_server_errors`, `test_non_finite_retry_after_does_not_sleep_or_retry`, `test_get_retries_a_connection_reset`, `test_exhausted_get_transport_failures_are_github_errors`, `test_a_write_is_never_retried_after_a_server_error` |
| The same problem twice goes to a person | `workitem.reconcile` | `test_gate_loop`: `test_same_problem_twice_goes_to_a_human` |
| A merge without the gate is recorded | `workitem.reconcile` | `test_gate_loop`: `test_merge_without_a_merge_verdict_is_flagged` |
| A PR that conflicts with its base is made again on the current base | `Foreman.act` | `test_gate_loop`: `test_a_pr_that_conflicts_with_main_is_made_again_on_the_current_main` |
| No verdict for `FACTORY_GATE_WAIT_SECONDS` goes to a person | `Foreman.act` | `test_hardening`: `test_silent_gate_goes_to_a_person` |
| The budget ends a WorkItem | `Foreman.dispatch` | `test_actions`: `test_budget_exhausts` |
| A timeout moves the next attempt to another model family; the two-strike block waits for the failover the policy covers, once per budget, and stands when no untried worker remains (ADR-0081) | `factory_mcp/routing.py` `route`, `Foreman._dispatch`, `Foreman._defer_block` | `test_workers`: `test_a_timeout_family_is_avoided_while_another_family_may_run`; `test_autonomous_recovery`: `test_a_first_timeout_moves_the_next_attempt_to_another_model_family`, `test_two_timeouts_do_not_block_while_an_untried_worker_remains`, `test_two_timeouts_block_when_no_untried_worker_remains`, `test_the_pass_budget_still_stops_the_recovery_attempt` |
| A relay that lost an accepted task gets the same WorkOrder again, same idempotency key, no strike, bounded per WorkItem (ADR-0081) | `Foreman._relay_lost` | `test_autonomous_recovery`: `test_a_relay_loss_never_charges_a_strike_and_is_bounded`, `test_a_pending_remote_verify_falls_back_to_the_ordinary_attempt` |
| A plan opens no issue when one of its issues fails a check | `Foreman.start_plan` | `test_plan`: `test_one_issue_that_fails_a_check_opens_no_issue` |
| A planned replacement is opened `factory:proposed` and adopted by its marker, never duplicated (ADR-0064) | `Foreman._resolve_replacements`, `Foreman._open_replacement` | `test_recovery_creation`: `test_a_planned_replacement_is_opened_proposed_and_chained`, `test_a_lost_create_response_adopts_the_exact_issue`, `test_after_a_restart_the_barrier_holds_and_the_resume_adopts`, `test_mismatched_words_refuse` |
| A replacement that changes the work runs only on the owner's approval (Day Desk with `ownerAsked`, the owner's note); a split that carries the original's acceptance bullets across the replacements runs without it (ADR-0097), under one stable `requestId`; a different ID for the same WorkItem conflicts before any write (ADR-0067) | `Foreman.replace_issue` | `test_recovery`: `test_day_desk_may_split_an_issue_that_keeps_its_acceptance`, `test_day_desk_may_replace_only_on_the_owners_approval`, `test_ownerAsked_is_not_the_owners_own_flag`, `test_a_different_requestId_for_the_same_original_conflicts` |
| A rewired dependent body is verified by exact readback before the operation claims it; a lost edit response or a drift refuses and the journalled retry replays it (ADR-0065) | `Foreman.replace_issue` | `test_recovery_creation`: `test_the_rewiring_reads_back_exactly_and_keeps_the_unrelated_edges`, `test_a_drifted_edit_refuses_and_the_journalled_retry_replays_it`, `test_a_lost_edit_response_replays_without_duplicate_or_lost_edges`, `test_a_kill_between_rewirings_replays_the_remaining_edges` |
| A plan allowance backs only scoped recovery (ADR-0069, ADR-0075): one approval per plan id naming the repo and exact members it covers, each use one committed count with the membership checked at the spend, a safety stop or a confirmed environment fault refused, an item that used its pass budget refused before any count or state moves, an unscoped legacy record refused by default, an exhausted or out-of-scope allowance refused, and a journalled replay never pays a second use | `store.spend_allowance`, `Foreman.approve_plan_allowance`, `Foreman.use_allowance`, `Foreman.replace_issue` | `test_plan_allowance` |
| The original retires before the replacement can run: its unmerged PR is closed, never merged, and a merged or unverifiable PR stops the run with the barrier held; a replay closes nothing twice (ADR-0066) | `Foreman._retire_original`, `Foreman.replace_issue` | `test_recovery_creation`: `test_the_original_pr_is_closed_before_the_issue_and_is_never_merged`, `test_a_merged_original_pr_stops_before_anything_can_run`, `test_an_unverifiable_original_pr_stops_before_activation`, `test_a_lost_pr_close_replays_and_closes_nothing_twice` |
| An issue starts only after each `Depends on #n` issue merged | `Foreman._wait_for` | `test_plan`: `test_a_dependent_waits_while_its_dependency_is_open_and_starts_once_it_merged`, `test_a_dependency_that_ends_without_a_merge_sends_the_dependent_to_a_person` |
| Dependencies change only before any worker pass, in one repo, without a cycle or an ended issue; a stranded `human_waiting` one may change too | `Foreman.set_dependencies` | `test_dependencies`: `test_a_workitem_no_worker_has_touched_may_change_and_any_other_may_not`, `test_a_human_waiting_workitem_with_no_pass_may_change_its_order`, `test_a_dependency_must_be_an_open_issue_of_the_same_repo_that_has_not_ended`, `test_a_change_that_would_make_a_cycle_is_refused` |
| An escalation is an incident answered once; a member of an active Project is answered as often as the budget allows, except a safety stop, an environment fault and the 8-pass cap; only a material change re-arms a non-member incident, a dependency that resumed clears it, and a tick alone never re-arms it (ADR-0081) | `Foreman._write`, `Foreman._recheck_dependency`, `foreman.agent_may_resolve` | `test_autonomous_recovery`: `test_a_material_change_re_arms_the_incident_once_and_an_unchanged_one_never_does`, `test_an_unchanged_dependency_never_re_arms_and_a_changed_one_re_arms_once`, `test_a_dependency_that_comes_back_resumes_the_workitem_without_a_person`, `test_a_human_verdict_stays_for_the_owner_across_ticks_and_recoveries`; `test_ops`: `test_an_owner_asked_close_answers_a_workitem_day_desk_already_answered` |
| The harness never touches the host | `worker/runner.py` | `test_worker_runner`: `test_the_harness_gets_a_copy_and_never_the_checkout`, `test_hooks_and_filters_the_harness_plants_never_run_on_the_host` |
| A patch for a remote-verify repo reaches the WorkItem's branch only after its `factory-verify` run passed | `Foreman._remote_verify` | `test_remote_verify`: `test_a_pass_deletes_the_scratch_branch_then_pushes_the_branch_and_opens_the_pr`, `test_a_failure_is_an_attempt_with_the_log_tail_and_the_next_order_carries_it` |
| Passes of one repo take its checkout in turns, up to `FACTORY_PASSES_PER_REPO` at once | `worker/runner.py` `checkout_lock`, `Foreman.dispatch` | `test_worker_runner`: `test_a_runner_waits_for_the_checkout_while_another_pass_holds_it`; `test_safety`: `test_passes_of_one_repo_run_at_once_up_to_the_limit` |
| `verify` is the base commit's, not the worker's | `worker/runner.py` | `test_worker_runner`: `test_verify_comes_from_the_base_commit` |
| A truncated output yields no Result: the runner's comes last | `Foreman.poll` | `test_hardening`: `test_a_result_line_left_in_a_truncated_output_is_not_read` |
| The protected-path check sees the whole patch, whatever its base is called | `patches.apply` | `test_workers`: `test_a_protected_path_is_refused_whatever_name_the_base_has` |
| Every tool declares whether it writes; Day Desk runs the agent tools on the agent token and the rest on the owner's (ADR-0041) | `tools.py`, `tenant/hermes/config.yaml` | `test_mcp_server`: `test_every_tool_declares_whether_it_writes`; `test_hermes`: `test_day_desk_drives_with_the_agent_token_and_runs_the_rest_on_the_owner_token` |
| Day Desk's shell is a Docker sandbox with one secret, on a standalone gateway (ADR-0021, ADR-0041) | `tenant/hermes/config.yaml`, `tenant/hermes/sandbox/Dockerfile` | `test_hermes`: `test_the_gateway_is_standalone_so_the_sandbox_gets_its_token`, `test_the_shell_runs_in_docker_with_the_github_token_as_its_only_secret`, `test_the_sandbox_hands_gh_the_token_the_config_passes_in` |

## Glossary

- **WorkItem**: one issue's durable record in factory-mcp. One issue, one branch, one PR.
- **WorkOrder**: one instruction to a worker: implement, or fix the gate's findings.
- **Result**: what a worker hands back: a patch, and the evidence.
- **Verdict**: the gate's decision on one PR head: MERGE, CHANGES or HUMAN, with findings.
- **Tick**: one pass of reconcile, then at most one action, for each WorkItem.
- **Pass**: one worker task. Active Project members charge the shared Project budget at dispatch,
  after worker routing and timeout checks, with a lifetime cap of 8 passes per member (ADR-0101).
  Non-members and members of done Projects keep `FACTORY_MAX_PASSES` per retry budget.
- **Attempt**: a pass that failed. Two in a row block the WorkItem, unless a timeout-class failure
  still has an untried worker the policy would choose: that one recovery attempt runs first
  (ADR-0081).
- **Blocked**: stopped after the failed attempts of its budget, until a person answers or the
  Lead recovers an active Project member (ADR-0102).
- **Exhausted**: stopped because the budget of passes ran out; only the owner's retry, which buys
  a fresh budget, starts it again for non-members. Members need remaining Project budget and
  fewer than 8 lifetime passes. The owner extends or resumes a Project with `factory extend`
  (ADR-0101). The owner may ask Day Desk for a retry in Telegram (ADR-0043).
- **Autonomy rate**: of the WorkItems that ended, the share that merged with no human state, no
  commit other than a worker's or the gate's merge of the base branch (ADR-0039), and a MERGE
  verdict on the merged head (`metrics.autonomy`). It is the project's measure of success.
