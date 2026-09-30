# How the factory works

For anyone changing this repo. Running the factory is in [RUNBOOK.md](RUNBOOK.md), the four
documents the parts exchange are in [contracts.md](contracts.md), and the reasons behind the design
are in [decisions.md](decisions.md).

## Components

```
Owner ── Telegram ──► Day Desk (Hermes, tenant/hermes)        factory CLI (scripts/factory)
                          │ MCP tools; each write needs the owner's yes       │ MCP tools
                          ▼                                                   ▼
            ┌────────────────────────────── factory-mcp ──────────────────────────────┐
            │ the Foreman: for each WorkItem, take the lease, observe GitHub,         │
            │ reconcile, then act at most once. SQLite: WorkItems, WorkOrders, audit. │
            └───────┬─────────────────────────────┬──────────────────────────────┬────┘
          HTTP tasks│                  REST, git  │                         read │
                    ▼                             ▼                              ▼
             agent-relay ──► worker/runner.py     GitHub: issues, labels,   pr-gatekeeper:
                             (Docker sandbox,     PRs, check runs           reviews, merges,
                              Freebuff + GLM)                               comments a verdict
```

- **factory-mcp** (`factory_mcp/`) is the Foreman: a Python MCP server, standard library only, on
  the owner's Mac. It holds each WorkItem's state and decides every transition and every side
  effect. A background loop ticks every WorkItem every `FACTORY_POLL_SECONDS` (60).
- **agent-relay** ([repo](https://github.com/Ami-Khokhar/agent-relay)) runs worker tasks.
  factory-mcp submits one task per WorkOrder over HTTP and reads it on later ticks; it never waits.
  Each call carries the relay's token, which factory-mcp reads on every call from
  `AGENT_RELAY_TOKEN` or the file the relay writes on its first start.
- **worker/runner.py** is what a relay agent runs: Freebuff works in a Docker sandbox with no
  GitHub credential, and the runner turns its edits into a patch Result. `worker/freebuff.py`
  drives Freebuff's terminal UI and waits for its local completion log; Freebuff has no headless
  CLI mode in the pinned version.
- **GitHub** holds the facts. factory-mcp reads issues, labels, PRs, check runs and the gate's
  comments, and rebuilds its WorkItems from them when its store is lost (ADR-0009).
- **pr-gatekeeper** reviews every PR and merges it on MERGE. factory-mcp has no merge tool
  (ADR-0004).
- **Day Desk** is the owner's front door in Telegram (ADR-0013). The `factory` CLI calls the same
  tools. For a new repo, one owner-approved tool prepares a setup PR with only missing controls;
  the owner merges it before normal worker passes start (ADR-0019). One owner approval of
  `factory_start_plan` opens all the issues of a plan, and they run in order (ADR-0024).

## What crosses each boundary

| From → to | What crosses | Defined in |
|---|---|---|
| factory-mcp → worker | A WorkOrder, inside the task input, after the worker rules | `schemas/workorder.schema.json`, `documents.worker_input` |
| worker → factory-mcp | A Result: the last JSON line of the task output | `schemas/result.schema.json`, `documents.check_result` |
| pr-gatekeeper → factory-mcp | A Verdict, read from the marker JSON in the gate's latest PR comment | `schemas/verdict.schema.json`, `verdict.py` |
| factory-mcp → GitHub | The `factory/<issue>` branch, a PR in the gate's body format, labels, comments | `patches.py`, `github.py`, `documents.pr_body` |
| Keychain → factory-mcp → GitHub | A gate secret's value: `security`'s stdout, then `gh secret set`'s stdin, with the factory's token; never logged or returned | `gate_secrets.py` |
| Day Desk, CLI → factory-mcp | MCP tool calls, each one marked read or write | `tools.py` |

## Code map

| File | What it does |
|---|---|
| `factory_mcp/__init__.py`, `__main__.py` | The package; `python3 -m factory_mcp` starts the server |
| `factory_mcp/server.py` | Streamable HTTP for MCP on loopback: `POST /mcp`, the bearer token, the background tick loop |
| `factory_mcp/mcp.py` | The MCP protocol: one JSON-RPC message in, one response out, no I/O |
| `factory_mcp/tools.py` | The tools, and whether each one reads or writes |
| `factory_mcp/foreman.py` | The Foreman: intake, rebuild, tick, dispatch, poll a task, open a PR, and the person's acts (resolve, approve an issue, start a plan, add a repo, approve or retire a lesson) |
| `factory_mcp/workitem.py` | Intake from an issue, observing GitHub, and `reconcile`: the fact-driven half of the state machine |
| `factory_mcp/documents.py` | WorkOrders (with the repo's lessons), the worker input, the Result check, the PR body |
| `factory_mcp/verdict.py` | Reading the gate's verdict marker; finding fingerprints and blockers |
| `factory_mcp/store.py` | SQLite: WorkItems with optimistic concurrency, WorkOrders, the audit log, leases, migrations |
| `factory_mcp/github.py` | The GitHub REST client |
| `factory_mcp/relay.py` | The agent-relay task client, and where it finds the relay's token |
| `factory_mcp/patches.py` | The git work factory-mcp does: cloning a worker checkout, pushing a patch Result |
| `factory_mcp/repos.py` | The repo check, and `repos.json` (ADR-0014) |
| `factory_mcp/gate_secrets.py` | A repo's gate Actions secrets, set from the owner's Keychain through `gh` (ADR-0022) |
| `factory_mcp/lessons.py` | Lesson candidates from the audit log, and `lessons.json` (ADR-0020) |
| `factory_mcp/routing.py` | Which worker takes the next attempt: locality first, then one that has not failed |
| `factory_mcp/metrics.py` | The escalation text, the autonomy rate, and whether each lesson works |
| `factory_mcp/config.py` | Settings from the environment and `repos.json` |
| `worker/runner.py` | Runs one WorkOrder in the sandbox and prints its Result |
| `worker/freebuff.py` | Runs one Freebuff/GLM prompt through a PTY and checks its completion log |
| `worker/Dockerfile` | The sandbox image: git, Python, Node and Freebuff, and no GitHub credential |
| `scripts/factory` | The owner's CLI: `new`, `add-repo`, `status`, `watch`, `retry`/`regate`/`close`, `start`/`stop`, `update`, `serve` |
| `scripts/install-services.sh` | The launchd services for agent-relay and factory-mcp |
| `scripts/rehearsal.py` | One issue end to end, with the real agent-relay and a stand-in GitHub. Its scripted worker hands back a patch, as `worker/runner.py` does, and factory-mcp pushes it |

## One tick

A tick comes from the background loop or from `factory_tick`.

1. **Rebuild.** Every open `factory:ready` issue in an allowlisted repo that the store does not
   know is taken in, so a lost store fills again from GitHub.
2. **Each live WorkItem** whose repo is allowlisted (#38): take its lease or skip it; observe
   GitHub (the issue, the PR on `factory/<issue>`, the gate's verdicts and check run); reconcile the
   state with those facts and write it; then act at most once:
   - `ready`: dispatch an implement WorkOrder, unless the issue has a `Depends on #n` line and
     #n has not merged: then it waits, with `waitingOn: n` (ADR-0024). `changes_requested`:
     dispatch a fix WorkOrder that carries the gate's blocking findings as data.
   - `dispatching`: submit the task to the relay. The state is written before the call, and the
     WorkOrder's idempotency key is the relay's request id, so a retry never starts a second task.
   - `implementing`: read the task. When it is done, check the Result and what GitHub holds, push
     a patch, and open the PR or adopt the one already open on the branch.
   - `pr_open` or `reviewing` with no verdict for `FACTORY_GATE_WAIT_SECONDS`: `human_waiting`.
   - `merged`: close the issue, because GitHub skips `Closes #n` when the gate's workflow token
     merges. Then `closed`.
3. **Entering `human_waiting`, `blocked` or `exhausted`** comments on the issue and mentions the
   owner. `factory watch` also tells the owner in Telegram of `human_waiting` and `blocked`,
   the states the owner can resolve; `exhausted` is final, so the issue comment is all it gets.

## State machine

The states are the enum in `schemas/workitem.schema.json`.

| From | Fact or action | To |
|---|---|---|
| (none) | intake of an open issue | `issued`; `planned` if labelled `factory:proposed`; `ready` if labelled `factory:ready` |
| `issued`, `planned` | the issue gains `factory:ready` | `ready` |
| `ready` | the tick dispatches an implement WorkOrder, once each `Depends on #n` issue is closed with a merged PR on `factory/<n>` | `dispatching`, then `implementing` once the relay accepts |
| `ready` | a `Depends on #n` issue is open | `ready` with `waitingOn: n`; nothing is dispatched |
| `ready` | a `Depends on #n` issue closed without a merged PR, ended `cancelled` or `exhausted`, or does not exist | `human_waiting` |
| `implementing` | the task completed, the Result is valid and the guards pass | `pr_open`, or `reviewing` when the PR already exists |
| `implementing` | the task failed, or the Result or a guard failed | back to `ready` (`changes_requested` for a fix) with an `attempts` entry; `blocked` after 2 |
| `pr_open` | the gate's check run exists on the head | `reviewing` |
| `reviewing` | a verdict on the current head: MERGE, CHANGES or HUMAN | `merge_pending`, `changes_requested` or `human_waiting` |
| `reviewing` | the gate's check run finished with no verdict | `human_waiting` |
| `changes_requested` | the same blocking problem in 2 gate rounds (ADR-0005) | `human_waiting` |
| `changes_requested` | the tick dispatches a fix WorkOrder | `dispatching`, then `implementing` |
| `merge_pending` | GitHub reports the PR merged | `merged` |
| `merged` | factory-mcp closes the issue | `closed` |
| any live state | the PR closed without a merge | `human_waiting` |
| any live state | the issue closed without a merged PR | `cancelled` |
| `ready`, `changes_requested` | `FACTORY_MAX_PASSES` WorkOrders were dispatched already | `exhausted` |
| `human_waiting`, `blocked` | `factory_resolve_human`: retry, regate or close | `ready` or `changes_requested`, `reviewing`, or `cancelled` |

`closed`, `cancelled` and `exhausted` are terminal. `human_waiting` is not: it waits for the
owner's decision (ADR-0006).

## The worker contract

- The Foreman sends the task with the repo's worker checkout (`FACTORY_CHECKOUT_ROOT`) as its
  working directory, and runs one pass per repo at a time.
- `worker/runner.py` fetches in the checkout, makes a fresh copy with no remote, and puts it on
  `factory/<issue>` at the commit to build on: the default branch for an implement WorkOrder, the
  reviewed head for a fix.
- The harness runs in the `factory-worker` container with that copy and a copy of its model key,
  and no GitHub credential. It commits locally. Host git never runs in a tree the harness wrote,
  so a planted hook or filter cannot run on the host.
- The runner turns the commits into a unified patch, runs the gate's `verify` command from the
  base commit in the same sandbox, and prints the Result as its last line.
- factory-mcp checks the Result, refuses a patch that touches a protected path before pushing it,
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
| One PR's size: 600 lines, 20 files | `Foreman.check_pushed` | `test_actions`: `test_a_change_must_fit_one_pr` |
| GitHub holds the pushed commit | `Foreman.check_pushed` | `test_actions`: `test_bad_results_fail_attempts_then_block` |
| One PR per WorkItem | `Foreman.open_pr` | `test_actions`: `test_a_result_for_a_branch_with_an_open_pr_joins_that_pr`, `test_open_pr_adopts_a_pr_opened_after_the_reconcile` |
| A stale verdict is ignored | `workitem.current_verdict` | `test_actions`: `test_stale_verdict_ignored`; `test_reconcile`: `test_old_round_is_stale` |
| The same problem twice goes to a person | `workitem.reconcile` | `test_gate_loop`: `test_same_problem_twice_goes_to_a_human` |
| A merge without the gate is recorded | `workitem.reconcile` | `test_gate_loop`: `test_merge_without_a_merge_verdict_is_flagged` |
| No verdict for `FACTORY_GATE_WAIT_SECONDS` goes to a person | `Foreman.act` | `test_hardening`: `test_silent_gate_goes_to_a_person` |
| The budget ends a WorkItem | `Foreman.dispatch` | `test_actions`: `test_budget_exhausts` |
| A plan opens no issue when one of its issues fails a check | `Foreman.start_plan` | `test_plan`: `test_one_issue_that_fails_a_check_opens_no_issue` |
| An issue starts only after each `Depends on #n` issue merged | `Foreman._wait_for` | `test_plan`: `test_a_dependent_waits_while_its_dependency_is_open_and_starts_once_it_merged`, `test_a_dependency_that_ends_without_a_merge_sends_the_dependent_to_a_person` |
| The harness never touches the host | `worker/runner.py` | `test_worker_runner`: `test_the_harness_gets_a_copy_and_never_the_checkout`, `test_hooks_and_filters_the_harness_plants_never_run_on_the_host` |
| `verify` is the base commit's, not the worker's | `worker/runner.py` | `test_worker_runner`: `test_verify_comes_from_the_base_commit` |
| A truncated output yields no Result: the runner's comes last | `Foreman.poll` | `test_hardening`: `test_a_result_line_left_in_a_truncated_output_is_not_read` |
| The protected-path check sees the whole patch, whatever its base is called | `patches.apply` | `test_workers`: `test_a_protected_path_is_refused_whatever_name_the_base_has` |
| Every write tool waits for the owner in Hermes | `tools.py`, `tenant/hermes/config.yaml` | `test_mcp_server`: `test_every_tool_declares_whether_it_writes`; `test_hermes`: `test_day_desk_sees_the_tools_it_needs_and_asks_before_each_write` |

## Glossary

- **WorkItem**: one issue's durable record in factory-mcp. One issue, one branch, one PR.
- **WorkOrder**: one instruction to a worker: implement, or fix the gate's findings.
- **Result**: what a worker hands back: a patch, and the evidence.
- **Verdict**: the gate's decision on one PR head: MERGE, CHANGES or HUMAN, with findings.
- **Tick**: one pass of reconcile, then at most one action, for each WorkItem.
- **Pass**: one worker task. `FACTORY_MAX_PASSES` caps them per WorkItem.
- **Attempt**: a pass that failed. Two in a row block the WorkItem.
- **Blocked**: stopped after two failed attempts, until the owner decides.
- **Exhausted**: stopped because the budget of passes ran out.
- **Autonomy rate**: of the WorkItems that ended, the share that merged with no human state, no
  commit a worker did not make, and a MERGE verdict on the merged head
  (`metrics.autonomy`). It is the project's measure of success.
