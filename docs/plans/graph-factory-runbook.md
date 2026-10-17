# Factory graph plan: runbook from start to finish

Status: ready to start, 17 Oct 2026 IST. Nothing is built yet.

Plan: `docs/plans/graph-factory.md` on branch `plan/graph-factory`. This runbook puts the plan's
slices in order as work orders, with a check by the owner after each merge.

## Roles in this run

- Builder: MiMo V2.6 Flash (free), through OpenCode or pi. One slice per session.
- Reviewer: pr-gatekeeper on each PR. In software-factory the reviewer is `deepseek-v4.1-flash`
  and the verifier is `glm-5.3-flash`. The pr-gatekeeper repo uses the default reviewer,
  `deepseek-v4.1-flash`. MiMo is a third model family, so no builder is reviewed by its own family.
- Owner: the pre-flight, the config switches, the checkpoint after each merge, and every PR on a
  protected path.

## Checked on 17 Oct 2026, 20:15 to 20:30 IST

- software-factory `origin/main` is `8c0d602`. Its factory code is the same as at `dac66f2`, the
  plan's base: `#297` changed only the gate pin and two test strings. The line numbers in this
  runbook are at `8c0d602`.
- The software-factory gate already runs pr-gatekeeper `2a53e2c`. That commit contains `999ee2b`
  (the problems key), `e08f2e9` and `c828439`. So owner decision d4 is done for this repo.
- pr-gatekeeper `origin/main` is `2a53e2c`. Branch `mimo/b-weakened-tests` (`cf9a8da`) is not merged.
- Baseline on this Mac, in clean copies of main:
  - software-factory with `python3` (3.9.6): 704 tests OK in 94 s.
  - pr-gatekeeper with `python3.12`: 811 tests OK in 37 s. With `python3` (3.9.6), 7 tests in
    `tests/test_llm.py` fail before any change. Python 3.9 does not treat `socket.timeout` as a
    `TimeoutError`. So use `python3.12` in that repo.

## How to run one slice (owner)

1. Make the slice worktree. For slice N in software-factory:

   ```
   git -C "/Users/amteshwar/Documents/Coding projects/software-factory" fetch origin
   git -C "/Users/amteshwar/Documents/Coding projects/software-factory" worktree add "/Users/amteshwar/Documents/Coding projects/software-factory-worktrees/slice-N" -b graph/slice-N origin/main
   ```

   For slice 9 in pr-gatekeeper, use the same two commands with
   `/Users/amteshwar/Documents/Coding projects/pr-gatekeeper` and
   `/Users/amteshwar/Documents/Coding projects/pr-gatekeeper-worktrees/slice-9`.
2. Start OpenCode or pi with MiMo V2.6 Flash in that worktree.
3. Send this message. If the tool asks to read a file outside the worktree, allow it.

   ```
   Read /Users/amteshwar/Documents/Coding projects/software-factory-worktrees/graph-factory-plan/docs/plans/graph-factory-runbook.md. Do slice N only. Follow "Rules for every slice". When you finish or reach a stop rule, report and stop.
   ```

4. Read MiMo's report and its PR. pr-gatekeeper reviews the PR and merges it.
5. Do the owner checkpoint of the slice (see "Order"). Then start the next slice in a new worktree.
6. After the PR merges, remove the worktree: `git -C "<repo>" worktree remove "<worktree>"`.

## Rules for every slice (MiMo: read these first)

1. Do one slice only. Read the whole slice block before you change a file.
2. Work only in the slice worktree. Do not change files in another folder.
3. Change only the allowed paths of the slice. You can read any file in the worktree. Never change
   `.github/`, `AGENTS.md`, `~/.config/`, a `.env` file, the factory store or another repo.
4. The line numbers in a slice block are at the checked commit. Earlier slices move them. Find
   code by its function name. If the function or the described behavior is not there, stop.
5. Write the new tests first. Run them on the code before your change. Each new test must fail.
   Then change the code. Each new test must then pass.
6. A test that must also pass on the old code is a guard. Start its docstring with `Guard:`.
7. Do not delete, skip or weaken an existing test. If the slice changes a contract that old tests
   use, update their inputs. Do not change what they assert.
8. Keep the change near the estimate of the slice. The gate refuses more than 600 changed lines or
   more than 20 changed files.
9. Add the slice's ADR at the end of `docs/decisions.md`. Copy the shape of ADR-0081: a
   `## ADR-00NN — <title>` heading, then a `**Context.**`, a `**Decision.**` and a
   `**Consequence.**` paragraph.
10. Run the verify command of the repo. It must exit 0. Copy its last two lines into your report.
11. Commit on the slice branch. Message: `Slice N: <title> (ADR-00NN)`.
12. Push the branch. Open one PR to `main` with `gh pr create`. In the PR body, list each done-when
    check with its result, the verify output, and what you did not verify.
13. Do not merge. pr-gatekeeper reviews and merges. Never use `--admin`. Never force-push `main`.
14. If the gate answers CHANGES, fix only what its findings name. Push to the same branch.
15. Do not call factory tools (`factory_*`). Do not run `factory update`. Do not touch the live
    factory, its services or its store.
16. Stop and report when one of these occurs:
    - the code does not match the slice block (rule 4),
    - the change needs a file outside the allowed paths,
    - a new test does not fail on the old code,
    - the verify command fails 3 times with the same error,
    - the diff grows past 2 times the estimate,
    - the gate answers HUMAN, or answers CHANGES for the third time.

    In the report, give what you did, the exact error and the files you changed.

Verify commands:

- software-factory: `python3 -m unittest discover -s tests -p 'test_*.py'`
- pr-gatekeeper: `python3.12 -m unittest discover -s tests -p 'test_*.py'`

## Start: pre-flight (owner, once)

- [ ] Approve the plan: decision d0 on the plan page,
      https://claude.ai/artifact/9jHGQWdUWXTDwFZb5BunMg
- [ ] Record the builder choice (d6, d11): MiMo builds the slices. The plan recommended a strong
      builder for slices 3 to 8, because they change Foreman and runner decisions. With MiMo, keep
      their extra checks: the mutation_proof entries, the pilots and the rehearsal scripts.
- [ ] Check MiMo's free-tier limits and data policy now. MiMo reads the factory code from slice 1 on.
- [ ] d2, now and apart from MiMo: put `pi-glm-factory` first in `FACTORY_WORKERS` in
      `~/.config/factory-mcp/env`, or change the gate's `reviewer_model`.
- [ ] Optional config, no PR: d5 (`FACTORY_SOURCE_CHANGED_LINES` to about 150 to 200), d7 (set
      `FACTORY_LESSONS_FILE`), d8 (drop `opencode-glm-factory` from `FACTORY_WORKERS`).
- [ ] In the MiMo session: `gh auth status` shows Ami-Khokhar, and `git`, `python3` and
      `python3.12` run.

## Order

| Step | Slice | Repo | Lines | Risk | Starts when | Owner checkpoint after the merge |
|---|---|---|---|---|---|---|
| 1 | 1. Plans become DAGs | software-factory | 90 | Low | The pre-flight is done | Approve one plan with two independent issues. Both `submitted` audit rows come before the first `result` row. |
| 2 | 2. First-pass gauges | software-factory | 230 | Low | Slice 1 merged | Run the report read-only on a copy of the live store. It shows 189 passes, 115 accepted, implement 77 of 142, fix 38 of 47 and 35 timeouts. First-verdict MERGE is 35 of 74, or the gap has a reason. |
| 3 | 3. The gate's whole answer reaches the fix pass | software-factory | 200 | Low to medium | Slice 2 merged | None |
| 4 | 4. The builder stays outside the reviewer's family | software-factory | 150 | Low to medium | Slice 3 merged. Decide d3 (strong reviewer) first if you want it. | The next software-factory dispatch goes to a GLM worker. |
| 5 | 5. A timed-out pass hands back its work | software-factory | 120 | Low | Slice 4 merged | Rebuild the image by hand: `docker build -t factory-worker:latest worker/`. `factory update` does not rebuild it for a `runner.py` change. Then run the `scripts/regression_docker.py` timeout case. |
| 6 | 6. The Foreman keeps partial work | software-factory | 280 | Medium | Slice 5 merged and its image rebuilt | Run `scripts/rehearsal.py` and `scripts/regression_docker.py` before deploy. Pilot on one owner-approved issue (ADR-0055): the audit shows a wip push, then a finish pass. |
| 7 | 7. Paths lines fence source changes | software-factory | 280 | Low to medium | Slice 6 merged | Run `factory update`. The installed Day Desk skill matches `origin/main`. |
| 8 | 8. Overlapping Paths never run at once | software-factory | 200 | Medium | Slice 7 merged | None |
| 9 | 9. The gate proves each new test | pr-gatekeeper, then software-factory | 300 | Medium | Any time. It can run beside steps 2 to 8. | Move the gate pins with the ADR-0080 setup PRs. They touch `.github/`, so you merge them. |
| 10 | 10. Optional: pinned acceptance tests | software-factory | 300 | Medium | Only if the gauges ask for it (see "Finish") | Pilot on one Risk: high issue (ADR-0055). |

From slice 3 on, read the slice-2 gauges before and after each slice. The plan's rule: stop after
any slice if its gauges do not improve.

## Slice 1: Plans become DAGs

Repo: software-factory. Branch: `graph/slice-1`. ADR-0082 (amends ADR-0024). About 90 lines.

Goal: `factory_start_plan` takes an optional `after` list for each plan item, so plans become
graphs. A plan without `after` keeps today's chain.

Read first:
- `factory_mcp/foreman.py`: `start_plan` (line 1371), the chain line (1385), `_plan_problem`
  (1888), `_wait_for` (557).
- `factory_mcp/tools.py`: the `factory_start_plan` tool (231).
- `tests/test_plan.py`. ADR-0024 in `docs/decisions.md` (line 458).

Allowed paths: `factory_mcp/foreman.py`, `factory_mcp/tools.py`,
`tenant/hermes/skills/software-factory/SKILL.md`, `docs/decisions.md`, `tests/test_plan.py`.

Change:
- A plan item can carry `after`: the 1-based positions of earlier items that it waits for.
- No `after`: keep today's line `Depends on #<previous>`. `after: []`: write no Depends-on line.
- Each position must be an integer, earlier than its own item, and not repeated. If not, refuse
  the whole plan before any issue opens. Do this check with the other plan checks, before
  `self._enabled(...)`.
- Write one `Depends on #<issue number>` line for each position. Map a position to the issue that
  this call opened for that item.
- `_wait_for` already waits on every line, so fan-in needs no change.
- `tools.py`: add optional `after` (an array of integers, minimum 1) to the item schema. The item
  schema has `additionalProperties: false` today. Explain `after` in the tool description.
- SKILL.md, section "Plan work": set `after` only for real dependencies. Two issues that edit the
  same file, `docs/decisions.md` included, get an `after` edge. Tell the owner which issues run
  side by side. Before asking for the yes, list a pre-flight: credentials, gate secrets and verify
  tools (`factory_repo_check`).

New tests in `tests/test_plan.py` (4):
1. `after: []` opens an issue with no Depends-on line.
2. `after: [1, 2]` writes two Depends-on lines, and a tick holds that WorkItem until both merge.
3. A self, later, repeated or non-integer position refuses the plan and opens no issue.
4. The `factory_start_plan` tool accepts `after` and passes it to `start_plan`.

Done when: the 4 new tests fail on `8c0d602` and pass after the change. The existing chain test
still passes. The verify command exits 0.

## Slice 2: First-pass gauges in the weekly report

Repo: software-factory. Branch: `graph/slice-2`. ADR-0083. About 230 lines.

Goal: the weekly report shows first-pass quality, so each later slice can be judged.

Read first:
- `factory_mcp/foreman.py`: `tick_one` (393). Its audit row is the
  `self._write(wi, new, "reconcile", **detail)` call.
- `factory_mcp/metrics.py`: `weekly` (129).
- `factory_mcp/tools.py`: the `factory_report` tool (393) and its call of `metrics.weekly` (92).
- `tests/test_weekly_report.py`.

Allowed paths: `factory_mcp/foreman.py`, `factory_mcp/metrics.py`, `factory_mcp/tools.py`,
`docs/decisions.md`, `tests/test_weekly_report.py`.

Change:
- `tick_one`: the reconcile audit row adds the accepted verdict's outcome and round.
- `metrics.py` adds these gauges: the first-verdict MERGE rate, the clean one-shot rate (one pass,
  first verdict MERGE, merged, no failed attempt, no human state), rounds to merge, the
  accepted-Result rate by pass kind and by worker family, the timeout share of failed passes, and
  dependency-wait hours per plan. Old rows infer the outcome from the state move.
- `metrics.weekly()` and `factory_report` print each gauge with its n, the IST window and the gate
  pin.

New tests in `tests/test_weekly_report.py`: a fixture history with rates that you count by hand.
Each gauge in the report output names its n and its window.

Done when: the new tests fail on the old code and pass after. The verify command exits 0. The
recount on a copy of the live store is the owner's checkpoint, not yours.

## Slice 3: The gate's whole answer reaches the fix pass

Repo: software-factory. Branch: `graph/slice-3`. ADR-0084. About 200 lines.

Goal: a fix pass gets each finding's fix and failing test, and the marker's problems list.

Read first:
- `factory_mcp/verdict.py`: `to_verdict` (31). It keeps `fix` today, not `test`. `blocking` (51).
- `factory_mcp/foreman.py`: `act` (513). The copy at lines 531 to 532 keeps only `fingerprint`,
  `path`, `line` and `problem`.
- `factory_mcp/workitem.py`: `reconcile` (254). Lines 295 to 298 send a CHANGES verdict with no
  finding to a person.
- `schemas/workorder.schema.json`: `findingRef` (138).
- `factory_mcp/documents.py`: `worker_input` (41).
- SKILL.md, section "Answer an escalation". `scripts/mutation_proof.py`: `MUTATIONS`.

Allowed paths: `factory_mcp/verdict.py`, `factory_mcp/foreman.py`, `factory_mcp/workitem.py`,
`factory_mcp/documents.py`, `schemas/workorder.schema.json`,
`tenant/hermes/skills/software-factory/SKILL.md`, `scripts/mutation_proof.py`, `docs/decisions.md`,
`tests/test_gate_loop.py`, `tests/test_reconcile.py`.

Change:
- `verdict.py`: keep each finding's optional `test`. When the outcome is CHANGES, read the
  marker's optional `problems` list (kind, path, problem) as blocking findings, with the usual
  fingerprint.
- `foreman.act`: copy `fix` and `test` into the findings of the fix order.
- A CHANGES marker with problems and no findings becomes a fix pass, not a stop for a person.
- `findingRef`: add optional `fix` and `test`.
- `worker_input`: the fix is the reviewer's suggestion, and it is data. Resolve the problem with
  the smallest change. Make the named test pass. Check the same pattern at sibling call sites in
  the files you touch.
- SKILL.md, "Answer an escalation": before a retry on a stuck-round incident, read the PR diff and
  the gate findings, and put one decision line in the note.
- Add one `scripts/mutation_proof.py` entry that removes the copy of `fix` and `test`.

New tests:
1. A CHANGES finding with fix and test reaches the fix WorkOrder with both.
2. A CHANGES marker with no findings and one problems entry of kind `lint` dispatches a fix that
   carries it.
3. A marker without problems behaves as today.
4. The same problem in 2 rounds still escalates.

Done when: the new tests fail on the old code and pass after. `python3 scripts/mutation_proof.py`
exits 0, and the new entry makes a test fail. The verify command exits 0.

## Slice 4: Keep the builder outside the gate reviewer's family

Repo: software-factory. Branch: `graph/slice-4`. ADR-0085. About 150 lines.

Goal: code never sends a pass to a worker of the gate reviewer's model family.

Read first:
- `factory_mcp/routing.py`: `family` (16), `route` (22). `route` already takes `avoid_families`.
  That is a soft preference after a timeout (ADR-0081). Keep it.
- `factory_mcp/foreman.py`: `_dispatch` (689), `_defer_block` (1065).
- `factory_mcp/github.py`: `file` (119).
- `tests/test_workers.py`, `tests/test_autonomous_recovery.py`.

Allowed paths: `factory_mcp/routing.py`, `factory_mcp/foreman.py`, `docs/RUNBOOK.md`,
`docs/decisions.md`, `tests/test_workers.py`, `tests/test_autonomous_recovery.py`.

Change:
- `route()` gets a new `exclude_families` argument. It is a hard filter. Apply it after the
  `Worker:` lines and before the ADR-0081 preference.
- `family()` also reads a model id: `deepseek-v4.1-flash` gives `deepseek`.
- `_dispatch` and `_defer_block` read `reviewer_model` and `strong_reviewer_model` from the default
  branch's `.github/pr-gatekeeper.json` with `github.file`. A missing key means pr-gatekeeper's
  default, `deepseek-v4.1-flash`. You read that file. You never change it.
- When no worker is left, the WorkItem waits for a person with the reason
  `every allowed worker is in the reviewer family <f>`.
- Add a note to `docs/RUNBOOK.md`.

New tests:
1. With `reviewer_model` `deepseek-v4.1-flash` and `FACTORY_WORKERS` listing `pi-deepseek-factory`
   first, the dispatch goes to `pi-glm-factory`.
2. A `Worker:` line that names only the reviewer's family waits for a person with that reason.
3. The ADR-0081 deferral picks only outside the family.
4. A repo without a gate config routes as before.

Done when: the new tests fail on the old code and pass after. The verify command exits 0.

## Slice 5: A timed-out pass hands back its work (runner)

Repo: software-factory. Branch: `graph/slice-5`. ADR-0086 (salvage, both halves). About 120 lines.

Goal: when the harness times out, the runner still returns the partial work as a patch.

Read first:
- `worker/runner.py`: `main` (503). The harness runs at line 575, and
  `if code == 124: raise WorkerError("the harness timed out")` is at lines 580 to 581. `collect`
  (391) and its call (586). `verify_command` (227).
- `schemas/result.schema.json`. `tests/test_worker_runner.py`. `scripts/regression_docker.py`,
  `tests/test_regression_docker.py`.

Allowed paths: `worker/runner.py`, `scripts/regression_docker.py`, `docs/decisions.md`,
`tests/test_worker_runner.py`, `tests/test_regression_docker.py`.

Change:
- On harness exit 124, run `collect()` without verify. `collect()` already commits uncommitted work
  and applies the size and file limits.
- Return status `failed`, the error `the harness timed out`, and the partial patch.
  `result.schema.json` already allows a patch on a failed Result. Today's Foreman ignores it, so
  this producer can ship first (ADR-0052).
- A refused or empty partial returns no patch.
- Add a timeout case to `scripts/regression_docker.py`.
- ADR-0086 covers both halves of salvage: this runner change and the Foreman change of slice 6.

New tests in `tests/test_worker_runner.py`:
1. A fake harness that writes a file and then times out returns status `failed`, the timeout error
   and a patch with that file.
2. A timeout with no change returns no patch and today's error.

Done when: the new tests fail on the old code and pass after. The verify command exits 0. The
Docker run of the timeout case is the owner's checkpoint, not yours.

## Slice 6: The Foreman keeps the partial work and sends a finish pass

Repo: software-factory. Branch: `graph/slice-6`. No new ADR: if the code differs from ADR-0086,
update ADR-0086. About 280 lines.

Goal: a timed-out pass's partial work is kept on a scratch branch, and the next pass finishes it.

Read first:
- `factory_mcp/foreman.py`: `OUT_OF_TIME` (42), `poll` (753), `push_patch` (883), `fail` (1110),
  `_dispatch` (689).
- `factory_mcp/config.py`. `schemas/workitem.schema.json`. `docs/RUNBOOK.md`.
- `tests/fakes.py`: `FakeGit` (251). `tests/test_autonomous_recovery.py`.

Allowed paths: `factory_mcp/foreman.py`, `factory_mcp/config.py`, `schemas/workitem.schema.json`,
`docs/RUNBOOK.md`, `docs/decisions.md`, `scripts/mutation_proof.py`,
`tests/test_autonomous_recovery.py`.

Change:
- In `poll()`: take a failed implement Result whose error matches `OUT_OF_TIME` and whose patch
  passes the protected-path check. Push it with `push_patch()` to the scratch branch
  `factory-wip/<issue>`. Save `WorkItem.wip` = `{branch, sha, workOrder}`. Then `fail()` runs as
  today (a strike, and the ADR-0081 family preference).
- In `_dispatch()`: the next implement WorkOrder takes `wip.sha` as `baseSha`, and adds a
  `partial-work` finding with this text: "A worker ran out of time. Its unfinished work is the
  commit you start from. Check each acceptance bullet against the code, not against any report.
  Keep what is right, fix or remove the rest, and finish."
- wip moves forward at most twice.
- A completed Result, a merge or an end deletes the scratch branch and clears `wip`.
- `FACTORY_SALVAGE=off` turns salvage off. Read it in `config.py`.
- `schemas/workitem.schema.json`: optional `wip`. `docs/RUNBOOK.md`: a note on scratch branches.
- Add one `scripts/mutation_proof.py` entry for the protected-path check on a partial.

New tests with `FakeGit`:
1. A timeout Result with a patch pushes `factory-wip/7`, and the next WorkOrder's `baseSha` is that
   commit, with the partial-work finding.
2. A third timeout from a wip does not move it.
3. A completed Result deletes the scratch branch.
4. A partial that touches a protected path is not kept.

Done when: the new tests fail on the old code and pass after. `python3 scripts/mutation_proof.py`
exits 0. The verify command exits 0. The pilot on a live issue is the owner's checkpoint.

## Slice 7: Paths lines fence a node's source changes

Repo: software-factory. Branch: `graph/slice-7`. ADR-0087. About 280 lines.

Goal: each node lists the source files it may change, and code refuses a change outside them.

Read first:
- `factory_mcp/workitem.py`: `workers_named` (157) and its `WORKER` pattern, `intake` (162).
- `factory_mcp/documents.py`: the constraints (27), `worker_input` (41).
- `factory_mcp/foreman.py`: `check_pushed` (902), `_plan_problem` (1888).
- `factory_mcp/sizing.py`: `kind` (51).
- SKILL.md, section "Plan work". `tests/test_actions.py` (the `check_pushed` tests near line 150),
  `tests/test_plan.py`.

Allowed paths: `factory_mcp/workitem.py`, `factory_mcp/documents.py`, `factory_mcp/foreman.py`,
`schemas/workitem.schema.json`, `schemas/workorder.schema.json`,
`tenant/hermes/skills/software-factory/SKILL.md`, `docs/decisions.md`, `tests/test_actions.py`,
`tests/test_plan.py`.

Change:
- Parse `Paths: <glob>, <glob>` lines like `Worker:` lines, into `WorkItem.paths`.
- Put them in the WorkOrder as `constraints.allowedPaths`. No list means no fence. Add both
  optional fields to the schemas.
- `worker_input` gets a rule: read these files first. Change source only inside them. Tests and
  docs can change anywhere.
- `check_pushed()`: a source file (`sizing.kind`) outside `allowedPaths` fails the attempt and
  names each path. Put this check next to the protected-path check. Renames count.
- `_plan_problem` requires a `Paths:` line on every plan item. The existing plan tests then need a
  `Paths:` line in their items. Add it, and keep what they assert.
- SKILL.md, "Plan work": clone the repo, read the files, list each issue's source files in
  `Paths:`. This must ship in the same PR, because plans without Paths stop working once it lands.

New tests:
1. A pushed source change outside Paths fails the attempt and names the file.
2. A test or doc file outside Paths is accepted.
3. An issue without Paths runs unfenced.
4. `factory_start_plan` refuses an item without a `Paths:` line and opens nothing.
5. `worker_input` lists the fence.

Done when: the new tests fail on the old code and pass after. The verify command exits 0.

## Slice 8: Nodes with overlapping Paths never run at once

Repo: software-factory. Branch: `graph/slice-8`. ADR-0088. About 200 lines.

Goal: two nodes that list the same files run one after the other.

Read first: `factory_mcp/foreman.py`: `_wait_for` (557). `tests/test_dependencies.py`.

Allowed paths: `factory_mcp/foreman.py`, `docs/decisions.md`, `tests/test_dependencies.py`.

Change:
- After its dependencies merge, a ready WorkItem waits when its Paths overlap the Paths of an
  active WorkItem in the same repo. Active states: `dispatching`, `implementing`, `pr_open`,
  `reviewing`, `changes_requested`, `merge_pending`.
- Write an audit `waiting` row that names the overlap.
- Overlap is a glob match in either direction, or a directory prefix.
- A WorkItem without Paths has no overlap wait.
- A WorkItem that waits for a person (`human_waiting`) does not hold its files.

New tests:
1. Two ready WorkItems with disjoint Paths dispatch in one tick, up to `FACTORY_PASSES_PER_REPO`.
2. Two with overlapping Paths run in turn. The second dispatches on the tick after the first merges.
3. A `human_waiting` WorkItem does not hold its Paths.

Done when: the new tests fail on the old code and pass after. The verify command exits 0.

## Slice 9: The gate proves each new test fails without the change

Repo: pr-gatekeeper first, then one small PR in software-factory. Branches: `graph/slice-9` in
pr-gatekeeper, `graph/slice-9-rule` in software-factory. About 300 lines in pr-gatekeeper.

Goal: the gate runs each new Python test alone on the reverted source. A new test that still
passes there is weak, unless it is a guard.

Read first (pr-gatekeeper):
- `gatekeeper/gates.py`: `revert_probe` (298), `_clear_bytecode` (264). The `weak_tests` status
  exists already (39).
- `SPEC.md`: the revert probe (211). `tests/test_gates.py`.
- Branch `mimo/b-weakened-tests` (`cf9a8da`) is not merged. Do not copy from it. If it merges
  first, rebase on `main`.

Allowed paths (pr-gatekeeper): `gatekeeper/gates.py`, `SPEC.md`, `tests/test_gates.py`.

Change (pr-gatekeeper):
- For Python test files, find the test functions that the PR adds: compare the AST of head with
  the AST of base.
- Run each new test alone on the reverted source, with bytecode cleared.
- A new test that passes there is weak, unless its docstring starts with `Guard:`.
- The `weak_tests` status names each weak test in the marker's problems list.
- Above 20 new tests, or for other languages, keep today's suite-level probe.

New tests in `tests/test_gates.py`:
1. A PR that adds one test that fails on reverted source and one that passes gets `weak_tests`,
   which names the second.
2. The same PR with `Guard:` on the second test passes.
3. A PR whose only new test fails on reverted source is ok.
4. A JavaScript-only PR keeps today's result.
5. An equal-length edit in the same second is still caught.

Done when: the new tests fail on the old code and pass after.
`python3.12 -m unittest discover -s tests -p 'test_*.py'` exits 0.

Then the software-factory PR (branch `graph/slice-9-rule`, about 10 lines): one rule in
`factory_mcp/documents.py` `worker_input`: a new test that must also pass on the old code starts
its docstring with `Guard:`. Allowed paths: `factory_mcp/documents.py`,
`tests/test_worker_runner.py` (it tests `worker_input` at lines 191 and 413). One new test: the
worker input contains the rule.

## Slice 10 (optional): sol pins acceptance tests on high-risk issues

Repo: software-factory. Branch: `graph/slice-10`. ADR-0089. About 300 lines.

Start only when: after about 30 new nodes, the slice-2 gauges show Risk: high nodes below the
first-verdict MERGE rate of the rest.

Goal: Day Desk can pin acceptance tests on a high-risk issue, and a worker cannot change them.

Read first: `factory_mcp/workitem.py`: `intake` (162). `worker/runner.py`: `main` (503), `collect`
(391). `factory_mcp/documents.py`: `worker_input` (41). SKILL.md.

Allowed paths: `factory_mcp/workitem.py`, `worker/runner.py`, `factory_mcp/documents.py`,
`schemas/workitem.schema.json`, `schemas/workorder.schema.json`,
`tenant/hermes/skills/software-factory/SKILL.md`, `docs/decisions.md`,
`tests/test_worker_runner.py`, `tests/test_reconcile.py`.

Change:
- `intake()` reads an optional `## Acceptance tests` section: `File: <test path>` lines, each
  followed by a fenced block. Test paths only (`sizing.kind`). At most 3 files and 20 KB. Freeze
  them as `WorkItem.acceptanceTests`, like the acceptance list. The WorkOrder carries them.
- `worker_input`: these files are fixed. Make them pass. Do not edit them. If one is wrong, print
  `FAILED: acceptance test wrong: <why>`.
- `worker/runner.py` writes them before the harness and again before `collect()`. So an edit is
  discarded, and the tests ship in the PR.
- SKILL.md: on Risk: high issues (decision or safety logic, parsers, gates, untrusted input), Day
  Desk writes these tests after it reads the code, with hostile inputs where input is untrusted.

New tests:
1. A runner test: a harness that edits a pinned test still ships the issue's version, and verify
   runs it.
2. Intake tests: the section is parsed. A non-test path or an oversize file is refused with a
   reason.
3. A `FAILED: acceptance test wrong` Result is an ordinary failed attempt, and its reason reaches
   the incident.

Done when: the new tests fail on the old code and pass after. The verify command exits 0. The
pilot on one Risk: high issue is the owner's checkpoint.

## Finish (owner)

The plan is done when all of these are true:

1. Slices 1 to 9 are merged, and each owner checkpoint passed.
2. About 30 new nodes ran on the new factory. You read the slice-2 gauges for that window, with the
   gate pin, the worker and the wall budget (d10).
3. You decided whether salvage stays on, or set `FACTORY_SALVAGE=off`.
4. You decided on slice 10. Build it only when Risk: high nodes have a lower first-verdict MERGE
   rate than the rest.

Stop the run early when a slice makes its gauges worse, or when a slice fails its owner checkpoint
twice.
