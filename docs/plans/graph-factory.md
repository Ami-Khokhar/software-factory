# Plan: Small-node issue graph, proven by code

Status: proposal, 2026-10-17 IST. Nothing here is built. Slice 1 starts only after the owner approves this plan.

Goal: make software-factory work like the vaniloop hackathon session: a graph of small, checked work orders. Any model can fill each role. Small models write the code. Reliability comes from the structure, not from model strength.

Source: the read-only factory-graph-design workflow of 2026-10-17 (5 readers, 3 competing designs, 1 judge that merged them). Code references are at software-factory dac66f2 and pr-gatekeeper c9a6462. Main is now 8c0d602 (#297, gate pin only). The orchestrator did not re-verify every code reference and number. Check a reference before a slice depends on it.

## How the hackathon worked

You worked with one Opus 5.5 foreman for about 49 hours (8 Oct 20:38 to 10 Oct 21:48 IST), through 12 context compactions. It wrote almost no code: only 16 of its 2,995 tool calls edited source or tests directly. It turned each goal into GitHub issues that acted as work orders: allowed and forbidden paths, a verify command, a stop rule, 'claim, done when: check' bullets and Depends-on links. Each issue got one worktree, one branch and one PR of about 600 lines at most. Cheaper workers built them: 66 Sonnet subagents and 27 GLM or DeepSeek tasks in code-only clones. Codex, another model family, reviewed the PRs for at most 3 rounds, and the same implementer fixed the findings. Checks made 'done' mechanical: tests that fail without the change, lint and PR-body checks. Independent issues on disjoint files ran in parallel. Once, 14 PRs merged in 82 minutes. The foreman decided at forks, audited its own test harness when results looked wrong, and checked real screens and data. It worked because the truth lived in issues, git and a STATE file, and another model family read each change. Your 116 short messages set policy, not code.

## Core idea

Keep the factory's deterministic Foreman as the loop. Use GPT 6.1 sol only to plan and to decide at forks. Sol reads the code and plans a DAG of small issues, about 150 to 250 changed lines each. Each issue is a work order: a Paths fence, 'claim, done when: check' bullets, and Depends-on lines only for real dependencies. Code runs nodes that touch different files side by side. It keeps the work of a timed-out pass, sends the gate's full answer to the fix pass, and keeps the builder out of the reviewer's model family. The gate proves that each new test fails without the change. A strong reviewer from a third family takes round 1.

## Roles and models

Roles are slots with a per-role model setting, so any model can fill them and the gauges compare them. CONTROL PLANE: Day Desk on GPT 6.1 sol at high effort (its Hermes profile). It clones and reads the code, plans the DAG and each node's contract, writes the pre-flight list, answers each incident once with a decision line, runs the plan's final runtime check, and may write pinned tests for Risk: high nodes (slice 10). It never writes product code, pushes, merges or reviews a PR, and the Foreman never calls it (ADR-0010, ADR-0030). It works at plan time and at forks only, which also bounds Codex quota. FOREMAN (factory-mcp): deterministic code with no model. It owns every state change, check, route and gauge. BUILDERS (FACTORY_WORKERS): small models in single-shot Docker passes without a GitHub credential, today GLM 5.3 Flash on pi. DeepSeek V4.1 Flash builds only where the gate reviewer is not DeepSeek (slice 4). REVIEWER (pr-gatekeeper, per repo): reviewer_model from another family than the builder, strong_reviewer_model from a third family on round 1 and on sensitive paths (tiering.py at c9a6462 supports it, every repo leaves it empty), verifier_model to confirm or drop findings, at most 3 rounds per problem, and the only routine merge authority (ADR-0004). DETERMINISTIC CHECKS: the gate's verify read from the base commit (the CI command), protected paths, size target and ceiling, Paths fence, revert probe (per new test after slice 9), proof replay, secret scan, PR lint, update-branch before merge, leases, idempotency keys, the two-strike incident and the same-problem-twice stop. No advisor tier by default. The owner may ask for a strongest-model memo on a design fork, as the hackathon did three times.

## Model pool (owner constraint, 2026-10-17)

Only these models fill factory roles:

| Model | Access | Family | Roles |
|---|---|---|---|
| GPT 6.1 sol | Codex (ChatGPT plan), local only | OpenAI | Control plane: Day Desk plans, forks, incidents, optional pinned tests (slice 10) |
| GLM 5.3 Flash | OpenCode or pi | Zhipu | Builder, reviewer, verifier |
| DeepSeek V4.1 Flash | OpenCode or pi | DeepSeek | Builder, reviewer, verifier |
| MiMo V2.6 Flash (free) | OpenCode or pi | Xiaomi | Builder, reviewer, verifier |

Rules:
- sol stays out of the gate. The gate runs in GitHub Actions, and a personal ChatGPT plan must not run in CI. So every gate role uses a flash model.
- Three flash families make the family rule work. The builder's family never reviews its own PR (slice 4). Example: reviewer DeepSeek, builders GLM or MiMo, strong reviewer (round 1 and sensitive paths) the third family.
- MiMo is the third family that the owner decision on strong_reviewer_model asks for. With a DeepSeek reviewer, GLM and MiMo can still build, so one outage no longer stops all builds (slice 4 risk).
- No other model (Claude, Gemini or others) fills a factory role. Any name of another model in this plan describes the hackathon or the meta work only.

Assumption: this pool covers the factory's own roles. Building the factory's slices is meta work, which the owner does with Claude. The owner decision "Choose who builds the slices" keeps that meaning. Confirm before slice 3.

Check before slice 4:
- Which of the three flash models the gate can call through its API (OpenCode Go or Zen).
- MiMo's free-tier limits and data policy (rate limits; whether prompts may be used for training).

## Graph model

NODE. One GitHub issue is one WorkItem, one branch factory/&lt;n> and one PR (ADR-0002, unchanged). reconcile() stays the only fact-driven mover over the existing 15 states, with one action per tick under a lease. The node contract is the issue body, which code parses: the goal and a bench-style description (exact files, interface, rules, edge cases, one failure-path bullet), '## Acceptance' bullets 'claim — done when: check' plus one must-not-change bullet, 'Depends on #n' lines, 'Paths:' lines (the write set, slice 7), optional 'Worker:' lines and an optional '## Acceptance tests' section (slice 10). Inside a node each pass is one WorkOrder: implement, fix (the gate's findings with their fix and failing test, plus the marker's problems, all as data) or finish (an implement pass that starts from salvaged work on factory-wip/&lt;n>). Code checks each Result (schema, protected paths, size, Paths fence), pushes it and opens or updates the PR. pr-gatekeeper answers MERGE, CHANGES or HUMAN, at most 3 rounds per problem. It never merges a head behind base: it updates the branch and reviews the merged result again (SPEC.md:460).

EDGES. Hard edges are 'Depends on #n' lines that factory_start_plan writes from each item's `after` list (slice 1). Fan-in already works, because _wait_for checks every line. No `after` keeps today's chain, and [] means no edge. Soft edges are Paths overlaps that code computes at dispatch (slice 8). Two nodes that share files never run at once, and a node held by a person does not hold its files. Merged code on the default branch flows along an edge, and a dependent starts from the current default branch.

BUILD. Day Desk (sol) clones the repo in its terminal sandbox, reads the files, and drafts at most 10 nodes of about 150 to 250 changed lines, one concern each. It shows the owner the waves (which nodes run side by side), each node's acceptance and Paths, and a pre-flight list: credentials, gate secrets and verify tools (factory_repo_check). One yes calls factory_start_plan once. Code refuses the whole plan when a bullet has no check, a text names a protected path, an `after` is invalid (self, later, repeated) or, after slice 7, an item has no Paths line.

UPDATE. Every 60 s the tick dispatches nodes whose dependencies merged and whose Paths overlap no active node, up to FACTORY_PASSES_PER_REPO=2, with the builder outside the reviewer's family (slice 4). CHANGES becomes a fix pass. A timeout becomes a finish pass from the salvaged commit (slices 5, 6). Two failed attempts, or one problem in two gate rounds, opens an incident that Day Desk answers once (ADR-0081): retry with a decision line, widen a Paths line, re-point a dependency (factory_set_dependencies) or propose a split (factory_replace_issue, with the owner's yes, ADR-0067, inside a plan allowance, ADR-0069). Independent nodes keep running while one waits for a person. A feature plan ends with its runtime-check issue (ADR-0068).

## Build slices

Each slice is additive and ships alone, with tests. Stop after any slice if its gauges do not improve.

### Slice 1: Plans become DAGs: factory_start_plan takes `after` per issue

**Scope:** factory_mcp/foreman.py start_plan and _plan_problem: each plan item may carry `after`, the 1-based positions of earlier items it waits for. No `after` keeps today's chain. [] writes no Depends-on line. Each position must be an integer, earlier than its own item, and not repeated. Otherwise the whole plan is refused and no issue opens. The body gets one 'Depends on #n' line per position. _wait_for already waits on every line, so fan-in needs no change. factory_mcp/tools.py: optional `after` in the factory_start_plan item schema and description. tenant/hermes/skills/software-factory/SKILL.md, Plan work: set `after` only for real dependencies. Two issues that edit the same file, docs/decisions.md included, get an `after` edge. Tell the owner which issues run side by side. Before asking for the yes, list a pre-flight: credentials, gate secrets and verify tools (factory_repo_check). docs/decisions.md ADR-0082 amends ADR-0024. Tests in tests/test_plan.py.

**Done when:** Four new tests in tests/test_plan.py fail on origin/main dac66f2 and pass after the change. `after: []` opens an issue with no Depends-on line. `after: [1, 2]` writes two lines, and a tick holds that WorkItem until both merge. A self, later, repeated or non-integer position refuses the plan and opens no issue. Items without `after` keep the chain, and the existing chain test still passes. `python3 -m unittest discover -s tests -p 'test_*.py'` exits 0. After deploy, one owner-approved plan with two independent issues shows both `submitted` audit rows before the first `result` row.

**Est changed lines:** 90

**Risk:** Low. The default keeps the chain. A wrong `after` starts dependent work early. The gate updates a stale branch and reviews the merged result before it merges, and a conflict costs one redo pass (ADR-0032). FACTORY_PASSES_PER_REPO stays 2.

### Slice 2: First-pass gauges in the weekly report

**Scope:** factory_mcp/foreman.py tick_one: the reconcile audit row adds the accepted verdict's outcome and round. factory_mcp/metrics.py: first-verdict MERGE rate, clean one-shot rate (one pass, first verdict MERGE, merged, no failed attempt, no human state), rounds to merge, accepted-Result rate by pass kind and by worker family, timeout share of failed passes, and dependency-wait hours per plan. Old rows infer the outcome from the state move. metrics.weekly() and factory_report print each gauge with n, the IST window and the gate pin. ADR-0083. tests/test_weekly_report.py.

**Done when:** tests/test_weekly_report.py passes with a fixture history whose rates are counted by hand. Each gauge in the factory_report output names n and its window. A read-only run on a copy of the live store reproduces the recount baseline: 189 passes, 115 accepted, implement 77 of 142, fix 38 of 47, 35 timeouts. The first-verdict MERGE count is 35 of 74 or the gap is explained. The suite exits 0.

**Est changed lines:** 230

**Risk:** Low. Numbers only, no state change. Old rows can count a stuck-rule escalation as HUMAN, which is the conservative side.

### Slice 3: The gate's whole answer reaches the fix pass

**Scope:** factory_mcp/verdict.py: keep each finding's optional `test`, and when the outcome is CHANGES read the marker's optional `problems` list (kind, path, problem) as blocking findings with the usual fingerprint. factory_mcp/foreman.py act(): copy `fix` and `test` into findingsToAddress (line 531 drops them today). A CHANGES marker with problems and no findings then becomes a fix pass, not a person's stop (workitem.py:294-296). schemas/workorder.schema.json findingRef: optional fix and test. factory_mcp/documents.py worker_input: the fix is the reviewer's suggestion and is data. Resolve the problem with the smallest change, make a named test pass, and check the same pattern at sibling call sites in the files you touch. SKILL.md, Answer an escalation: before a retry on a stuck-round incident, read the PR diff and the gate findings and put one decision line in the note. ADR-0084. Tests in tests/test_gate_loop.py and tests/test_reconcile.py. One scripts/mutation_proof.py entry.

**Done when:** New tests fail on dac66f2 and pass after. A CHANGES finding with fix and test reaches the fix WorkOrder with both. A CHANGES marker with no findings and one problems entry of kind lint dispatches a fix that carries it. A marker without problems behaves as today. The same problem in 2 rounds still escalates. The mutation_proof entry that removes the copy makes a test fail. The suite exits 0.

**Est changed lines:** 200

**Risk:** Low to medium. A wrong fix suggestion can mislead, so the rule makes the problem, not the fix, the thing to resolve. The problems key needs the gate pin at or after 999ee2b. Until then only fix and test flow.

### Slice 4: Keep the builder outside the gate reviewer's model family

**Scope:** factory_mcp/routing.py: route() gains exclude_families, a hard filter applied after the Worker lines and before the ADR-0081 preference. family() also reads a model id (deepseek-v4.1-flash gives deepseek). factory_mcp/foreman.py _dispatch and _defer_block read reviewer_model and strong_reviewer_model from the default branch's .github/pr-gatekeeper.json with github.file. A missing key means pr-gatekeeper's default deepseek-v4.1-flash. When no worker is left, the WorkItem waits for a person with 'every allowed worker is in the reviewer family &lt;f>'. ADR-0085, a RUNBOOK note, tests in tests/test_workers.py and tests/test_autonomous_recovery.py.

**Done when:** New tests: with reviewer_model deepseek-v4.1-flash and FACTORY_WORKERS listing pi-deepseek-factory first, the dispatch goes to pi-glm-factory. A Worker line that names only the reviewer's family waits for a person with that reason. The ADR-0081 deferral picks only outside the family. A repo without a gate config routes as before. The suite exits 0. Live: the next software-factory dispatch goes to a GLM worker.

**Est changed lines:** 150

**Risk:** Low to medium. With a DeepSeek reviewer only GLM builders remain, so a GLM outage stops builds until the owner adds a third family (ADR-0076, ADR-0077) or changes the reviewer. The GLM verifier is not filtered: it only confirms or drops findings, and 43 of 44 blocking findings were confirmed (factory-outcomes).

### Slice 5: A timed-out pass hands back its work (runner)

**Scope:** worker/runner.py: on harness exit 124 (lines 579-580 raise today), run collect() without verify. collect() already commits uncommitted work and applies the size and file limits. Return status failed, the error 'the harness timed out', and the partial patch. result.schema.json already allows a patch on a failed Result, and today's Foreman ignores it, so this producer ships first (ADR-0052). A refused or empty partial returns no patch. Tests in tests/test_worker_runner.py and the timeout case in scripts/regression_docker.py.

**Done when:** A fake harness that writes a file and then times out returns status failed, the timeout error and a patch with that file. A timeout with no change returns no patch and today's error. The suite exits 0. The regression_docker timeout case passes against the rebuilt image.

**Est changed lines:** 120

**Risk:** Low. Producer only. Collect runs in the time left after the harness's 70% share, inside its existing 300 s limit.

### Slice 6: The Foreman keeps the partial work and sends a finish pass

**Scope:** factory_mcp/foreman.py poll(): a failed implement Result whose error matches OUT_OF_TIME (foreman.py:42) and whose patch passes the protected-path check is pushed with push_patch() to the scratch branch factory-wip/&lt;issue> and saved as WorkItem.wip {branch, sha, workOrder}. fail() then runs as today (strike, ADR-0081 family preference). _dispatch(): the next implement WorkOrder takes wip.sha as baseSha and adds a 'partial-work' finding: a worker ran out of time, its unfinished work is the commit you start from, check each acceptance bullet against the code and not against any report, keep what is right, fix or remove the rest, and finish. wip moves forward at most twice. A completed Result, a merge or an end deletes the branch and clears wip. schemas/workitem.schema.json wip, ADR-0086, RUNBOOK scratch branches. FACTORY_SALVAGE=off turns it off.

**Done when:** New tests with FakeGit: a timeout Result with a patch pushes factory-wip/7, and the next WorkOrder's baseSha is that commit with the partial-work finding. A third timeout from a wip does not move it. A completed Result deletes the scratch branch. A partial that touches a protected path is not kept. The suite exits 0. ADR-0055 pilot on one owner-approved issue: the audit shows a wip push, then a finish pass.

**Est changed lines:** 280

**Risk:** Medium. A confused partial can mislead the next worker, so the finding says to judge the code. The gauges compare finish passes with fresh passes, and FACTORY_SALVAGE=off reverts. Not verified that past timed-out passes left useful edits, because no patch was kept.

### Slice 7: Paths lines fence a node's source changes

**Scope:** factory_mcp/workitem.py: 'Paths: &lt;glob>, &lt;glob>' lines are parsed like Worker lines into WorkItem.paths. factory_mcp/documents.py: constraints.allowedPaths in the WorkOrder (absent means no fence) and a worker rule: read these files first, change source only inside them, tests and docs may change anywhere. factory_mcp/foreman.py check_pushed(): a source file (sizing.kind) outside allowedPaths fails the attempt and names each path, next to the protected-path check, renames included. start_plan's _plan_problem requires a Paths line on every item. SKILL.md Plan work: clone the repo, read the files, list each issue's source files in Paths. ADR-0087.

**Done when:** New tests: a pushed source change outside Paths fails the attempt and names the file. A test or doc file outside Paths is accepted. An issue without Paths runs unfenced. factory_start_plan refuses an item without a Paths line and opens nothing. worker_input lists the fence. The suite exits 0, and the installed Day Desk skill matches origin/main after `factory update`.

**Est changed lines:** 280

**Risk:** Low to medium. A list that is too narrow costs one pass, and Day Desk widens the issue's Paths line, which the next tick reads. Plans without Paths stop working once this lands, so the SKILL change ships in the same PR.

### Slice 8: Nodes with overlapping Paths never run at once

**Scope:** factory_mcp/foreman.py _wait_for(): after its dependencies merge, a ready WorkItem whose Paths overlap the Paths of an active WorkItem in the same repo (dispatching, implementing, pr_open, reviewing, changes_requested, merge_pending) waits, with an audit 'waiting' row that names the overlap. Overlap is a glob match in either direction or a directory prefix. A WorkItem without Paths has no overlap wait. A WorkItem held by a person does not hold its files. ADR-0088. Tests in tests/test_dependencies.py.

**Done when:** New tests: two ready WorkItems with disjoint Paths dispatch in one tick, up to FACTORY_PASSES_PER_REPO. Two with overlapping Paths run in turn, and the second dispatches on the tick after the first merges. A human_waiting WorkItem does not hold its Paths. The suite exits 0.

**Est changed lines:** 200

**Risk:** Medium. A holder that never ends would starve a waiter. The existing gate_silent and incident paths end it, and a test checks that the waiter starts after the holder merges.

### Slice 9: The gate proves each new test fails without the change (pr-gatekeeper)

**Scope:** pr-gatekeeper gatekeeper/gates.py revert_probe: for Python test files, find the test functions the PR adds (AST of head against base) and run each one alone on the reverted source, with bytecode cleared. Each one that passes there is weak, unless its docstring starts with 'Guard:' (a must-not-change test). Status weak_tests names each weak test in the marker's problems list. Above 20 new tests, or for other languages, keep today's suite-level probe. SPEC.md and tests in the gate repo. In software-factory, one worker_input rule: a new test that must also pass on the old code starts its docstring with 'Guard:'. The owner then moves the pins (ADR-0080).

**Done when:** Gate tests: a PR that adds one test that fails on reverted source and one that passes gets weak_tests naming the second. The same PR with 'Guard:' on the second passes. A PR whose only new test fails on reverted source is ok. A JavaScript-only PR keeps today's result. An equal-length edit in the same second is still caught. The gate repo's verify exits 0. In the factory, a weak-test CHANGES becomes a fix pass through slice 3.

**Est changed lines:** 300

**Risk:** Medium. Running new tests one by one costs gate time, so the cap falls back to today's probe. A worker could label a vacuous test 'Guard:', so the reviewer sees the label and the gauges count Guard tests per PR. Python only at first.

### Slice 10: Optional: sol pins acceptance tests on high-risk issues

**Scope:** factory_mcp/workitem.py intake(): an optional '## Acceptance tests' section of 'File: &lt;test path>' lines, each followed by a fenced block. Test paths only (sizing.kind), at most 3 files and 20 KB, frozen as WorkItem.acceptanceTests like acceptance. The WorkOrder carries them. worker_input: these files are fixed, make them pass, do not edit them, and if one is wrong print 'FAILED: acceptance test wrong: &lt;why>'. worker/runner.py writes them before the harness and again before collect(), so an edit is discarded and the tests ship in the PR. SKILL.md: on Risk: high issues (decision or safety logic, parsers, gates, untrusted input) Day Desk writes these tests after reading the code, with hostile inputs where input is untrusted. ADR-0089. Build only if the slice-2 gauges show Risk: high nodes below the first-verdict MERGE rate of the rest after about 30 new nodes.

**Done when:** A runner test: a harness that edits a pinned test still ships the issue's version, and verify runs it. Intake tests: the section is parsed, and a non-test path or an oversize file is refused with a reason. A 'FAILED: acceptance test wrong' Result is an ordinary failed attempt whose reason reaches the incident. The suite exits 0. ADR-0055 pilot on one Risk: high issue.

**Est changed lines:** 300

**Risk:** Medium. A wrong test from sol costs up to 2 passes before the incident returns to sol. Test text in an issue is code, so only trusted authors' issues reach a worker (ADR-0007), it runs only in the sandbox, and the gate reviews it in the PR.

## Owner decisions

- Approve slice 1 (ADR-0082): factory_start_plan takes an `after` list per issue, so plans become DAGs. A plan without `after` keeps today's chain.
- Stop same-family review now. FACTORY_WORKERS lists pi-deepseek-factory first (env changed 15 Oct 23:18 IST), its last 7 dispatches ran on software-factory, and that gate's reviewer is deepseek-v4.1-flash. Put pi-glm-factory first, or change reviewer_model, until slice 4 enforces the rule in code.
- Pick the round-1 strong reviewer: a third-family model that OpenCode Go serves (not verified which exist), set as strong_reviewer_model, with sensitive_paths for Foreman, runner and gate logic, in each repo's protected .github/pr-gatekeeper.json. Or keep flash and accept weaker review. Using GPT 6.1 sol there needs a second provider in pr-gatekeeper, and a personal subscription must not run in CI.
- Upgrade the gate pin from c9a6462 to pr-gatekeeper main with the ADR-0080 setup PR in each repo. Main adds the problems key (999ee2b), earlier-round minors, placeholder secrets (e08f2e9) and kept blockers (c828439).
- Lower FACTORY_SOURCE_CHANGED_LINES from 700 to about 150 to 200, so most PRs land under 300 changed lines. First-verdict MERGE was 58% to 64% below 300 lines and 33% at 300 to 599.
- Choose who builds the slices. Recommended: Sonnet subagents with a different-family reviewer for slices 3 to 8, which change decision logic. Alternative: the factory's own flash workers.
- Turn on lessons: set FACTORY_LESSONS_FILE. 0 of 189 WorkOrders ever carried a lesson.
- Drop opencode-glm-factory from FACTORY_WORKERS: 6 of 26 passes succeeded, and it made 8 of the 9 'no change' failures.
- Keep taste-bound UI work (look, layout) out of small-worker plans, or ask for a later preview-hold slice.
- After about 30 new nodes, use the gauges to decide whether salvage stays on and whether to build slice 10 (sol-pinned tests on high-risk issues).

## Reliability mechanisms

- DAG plans from an `after` list per plan item (slice 1) -> chain waits, and one failure stranding every later issue -> factory_start_plan writes 'Depends on #&lt;previous>' on every issue (foreman.py:1385 at dac66f2). factory_set_dependencies ran 2 times against 21 factory_start_plan calls (audit table of the store copy, 2026-09-24 22:40 to 2026-10-17 18:16 IST). 51 of 90 WorkItems waited, 443 h in all, and one end stranded 10 dependents (factory-outcomes). Hackathon: 5 workers on disjoint issues merged 14 PRs in 82 min (hackathon-session).
- Paths fence and overlap wait (slices 7, 8) -> scope creep and parallel merge conflicts -> hackathon issue orders carried allowed paths in 39 of 41 runs. 6 of 65 hand-backs edited outside them, each a test the change had to update, and 14 of 70 hit a merge conflict, 8 on one shared append-only file (subagent-runs). Factory nodes declare no write set today, and 3 WorkOrders were merge-conflict redos (workorders table, fingerprint merge-conflict, store copy).
- Small nodes: plan about 150 to 250 changed lines and lower the source target -> timeouts and weak first passes -> first-verdict MERGE was 7 of 11 under 100 lines, 19 of 33 at 100 to 299, 9 of 27 at 300 to 599 and 0 of 3 at 600+ (74 worker PRs joined 74 of 74, factory-outcomes). All 35 timeouts were implement passes and none were fix passes (store recount, attempt_failed joined to the last dispatch). Five oversized issues spent 16 passes with no merge, and all 12 replacements from four splits merged (factory-outcomes).
- Timeout salvage and a finish pass (slices 5, 6) -> a timed-out pass loses all its work -> the runner raises 'the harness timed out' before collect() (worker/runner.py:579-580). collect() commits uncommitted work, and each sandbox step is a fresh docker run on the host copy (runner.py:285-300), so the work survives the timeout. 35 of 74 failed passes timed out (store recount, reason contains 'timed out'). Hackathon: a finish agent given the stale order and the dirty diff closed the gap and found one more bug (workflow-scripts).
- The gate's whole answer reaches the fix pass (slice 3) -> partial fixes, and human stops on CHANGES without findings -> foreman.act keeps only fingerprint, path, line and problem (foreman.py:531), but the gate requires a fix and allows a failing test on every finding (pr-gatekeeper c9a6462 reviewer.py:100-102). reconcile sends CHANGES with no finding to a person (workitem.py:294-296). 4 CHANGES results had no blocking finding (2 PR-body lint, 1 size, 1 new verify failure), and 2 of those WorkItems stopped for a person (factory-outcomes). Fix passes succeed 38 of 47 against 77 of 142 implement passes (store recount). Hackathon fix orders with the failing input and the required fix were the small-model sweet spot (subagent-runs).
- Builder outside the reviewer's family, enforced in code (slice 4) -> same-family blind spots -> FACTORY_WORKERS lists pi-deepseek-factory first (~/.config/factory-mcp/env, changed 15 Oct 23:18 IST), and the last 7 dispatches in the store copy (15 Oct 23:44 to 16 Oct 01:18 IST) went to it. The software-factory gate reviewer has been deepseek-v4.1-flash since 24 Sep (.github/pr-gatekeeper.json history). route() only prefers another family after a timeout and never filters (routing.py). Hackathon: a same-family skeptic passed two changes that cross-family review sent back, one with 2 High and 2 Medium (workflow-scripts).
- Per-test proof at the gate (slice 9) -> vacuous tests passing review -> the gate's probe at c9a6462 is 'ok' when the test run exits nonzero on reverted source, and 'skipped' when no test file changed (gates.py revert_probe). One real test hides vacuous ones, and this repo's test_command runs the whole suite. 8 of 44 blocking findings were test gaps, and test-weakness wording hit 48 of 74 PRs (regex, indicative, factory-outcomes). Hackathon: 30 of 73 Agent work orders and 15 of 172 relay delegations asked for tests that fail without the change (regex over main_calls.json), and weak-test review findings fell from 5 of 15 to 0 of 125 after mechanical checks (subagent-runs).
- Strong third-family round-1 reviewer and sensitive paths (owner config) -> a flash reviewer that under-calls severity -> tiering.py at c9a6462 gives strong_reviewer_model to round 1 and to sensitive rounds, and the factory repo leaves it empty. 34 of 67 audited auto-merges carried a defect labelled minor or a dropped finding (secondary source, one spot check, factory-outcomes). Hackathon: Codex at high effort reviewed, and decision-logic PRs drew a High at first review in 7 of 11 (subagent-runs).
- Gate pin upgrade (owner setup PR, ADR-0080) -> HUMAN stops on fixture secrets, and minors lost between rounds -> HUMAN on software-factory#219 and #255 came from secret-shaped test fixtures (factory-outcomes). pr-gatekeeper e08f2e9 ignores placeholder shapes, 999ee2b keeps earlier-round minors and adds a machine-readable problems key, and c828439 keeps confirmed blockers until answered fixed (git log c9a6462..origin/main).
- Gauges per role slot (slice 2) -> changes that do not help stay unseen -> autonomy counts merges, not correctness (metrics.py). First-pass quality showed no trend in three weeks while the gate pin, wall budget and task mix changed (factory-outcomes). The hackathon foreman tracked review rounds per PR (workflow-scripts).
- Pre-flight list at plan approval (slice 1, SKILL) -> idle hours on a missing credential or tool -> the hackathon lost 9.5 h (9 Oct 01:45 to 11:17 IST) to a missing credential (hackathon-session). ideadump ended 0 of 10 autonomous on a missing tool, and python was missing in 6 passes (factory-outcomes).
- Kept as is: reconcile as the only mover, leases, idempotency keys, 3 gate rounds per problem, the same-problem-twice stop, one Day Desk answer per incident (ADR-0081), update-branch before merge -> double dispatch, endless loops, lost state, two PRs that pass alone and break together -> 72 of 74 worker PRs merged, and 32 of 34 first-CHANGES PRs converged (factory-outcomes). The gate re-reviews the merged result when a head is behind base (SPEC.md:460 at c9a6462).
- Each new guard gets a scripts/mutation_proof.py entry, and runner changes pilot on one owner-approved issue (ADR-0055) -> a guard that does nothing, or one producer change that breaks every WorkItem -> hackathon: a shell-function grep let a PR merge with a failed check, and a pipe hid a lint exit code (hackathon-session). ADR-0055 records an unknown-field failure on pi-glm-factory.

## Quality mechanisms

- Bench-style node specs: sol clones the repo and names the exact files, interface, rules, edge cases, one failure-path bullet and one must-not-change bullet. The 5 bench tasks written this way merged after one pass each and pass 29 of 29 hidden tests (factory-outcomes).
- Verify equals CI: the runner reads the gate's verify from the base commit (runner.verify_command), so the hackathon gap where the local check skipped lint and CI ran it cannot occur.
- Each new test must fail without the change, checked per test by the gate (slice 9). A test that must also pass on the old code starts its docstring with 'Guard:', as 3 of 21 tests in one hackathon PR needed no source change.
- Cross-family review: a strong third-family model on round 1 and on sensitive paths, flash delta rounds after it, at most 3 rounds per problem.
- Finding-shaped fix orders: the problem, file and line, the reviewer's fix, the failing test, the gate's problems, a check of sibling call sites, and a decision line from sol on stuck incidents. Small workers do well when the control plane makes the decision and gives the failing input (subagent-runs).
- Small nodes: about 150 to 250 changed lines, one concern, a Paths fence.
- Behind-base re-review: the gate updates a stale branch and reviews the merged result before it merges, which covers two PRs that pass alone and break together.
- A feature plan ends with its runtime check on the deployed feature (ADR-0068), with real screens and real data where the feature has them, as the hackathon checked in a real browser.
- Lessons on: repeated gate fingerprints become owner-approved lessons (ADR-0020). An important lesson later becomes a pinned or guard test, because small workers follow a failing test better than a sentence (subagent-runs).
- Pinned acceptance tests from sol for Risk: high nodes, with hostile inputs where input is untrusted (slice 10, optional, built only if the gauges ask for it).
- Gauges per role slot: first-verdict MERGE, clean one-shot, rounds to merge, and pass success by worker family and gate pin, so models can be compared per role.
- Taste-bound work (look, layout, product trade-offs) goes to sol and the owner's preview, not to small workers.

## State and memory

The durable layer stays. GitHub issues hold the node contracts, branches and PRs hold the code and the gate verdicts, and SQLite holds WorkItems, WorkOrders and the 9,690-row audit. A lost store rebuilds from GitHub (ADR-0009). This is stronger than the hackathon's STATE file, which still let work resume within one or two tool calls after each of 12 compactions. New fields are optional, so stored rows stay valid: WorkItem.paths (slice 7), WorkItem.wip {branch, sha, workOrder} (slice 6), WorkItem.acceptanceTests (slice 10), WorkOrder constraints.allowedPaths, findingRef fix and test (slice 3), and outcome and round on reconcile audit rows (slice 2). Partial work lives on the scratch branch factory-wip/&lt;n> on GitHub, not in a container, so a restart loses nothing. Day Desk keeps no chat state it depends on. Each turn starts from factory_status and factory_report, the resume pack the hackathon foreman rebuilt by hand after each compaction. Memory across nodes is executable where possible: pinned and guard tests stay in the repo, owner-approved lessons carry repeated gate findings (at most 10 per WorkOrder), and an owner correction becomes config or a SKILL rule, not a chat memory. Times are stored in UTC and shown in IST.

## Human touchpoints

Five, all policy or taste. (1) One yes per plan in Day Desk chat, with the waves, each node's acceptance and Paths, and the pre-flight list (credentials, gate secrets, tools in the worker image), so nothing waits on the owner at night. The hackathon lost 9.5 h (9 Oct 01:45 to 11:17 IST) to a missing credential. (2) Protected paths the owner merges: each repo's .github/pr-gatekeeper.json (strong_reviewer_model, sensitive_paths) and the gate pin upgrade (ADR-0080). (3) Config switches in ~/.config/factory-mcp/env: worker order, source-line target, lessons file and the salvage switch. (4) Second answers: Day Desk answers each incident once (ADR-0081). A repeat incident, a fresh pass budget (ADR-0035, ADR-0043) or a replacement plan (ADR-0067) goes to the owner as one question. Day Desk made 31 of 40 human_resolved answers in the store window. (5) Taste: UI look and product trade-offs stay with sol and the owner's preview, because the hackathon owner rejected three UI rounds that passed every check. Everything else is code or one Day Desk answer.

## Changes to the factory, file by file

- Slice 1: factory_mcp/foreman.py start_plan and _plan_problem take an `after` list per item. factory_mcp/tools.py adds it to the factory_start_plan item schema. SKILL.md Plan work: real-dependency edges, the same-file rule, the pre-flight list. ADR-0082 amends ADR-0024. tests/test_plan.py.
- Slice 2: foreman.tick_one adds outcome and round to reconcile audit rows. factory_mcp/metrics.py adds first-pass, pass-kind, worker-family and dependency-wait gauges to metrics.weekly() and factory_report. ADR-0083. tests/test_weekly_report.py.
- Slice 3: factory_mcp/verdict.py keeps a finding's test and reads the marker's problems. foreman.act copies fix and test. schemas/workorder.schema.json findingRef gains optional fix and test. documents.worker_input gains the fix, test and sibling rules. SKILL.md asks for a decision line on stuck incidents. ADR-0084. One mutation_proof entry.
- Slice 4: factory_mcp/routing.py route(exclude_families) and a model-id family reader. foreman._dispatch and _defer_block read the repo's gate config. ADR-0085.
- Slices 5 and 6: worker/runner.py collects after exit 124. foreman.poll pushes the partial to factory-wip/&lt;n> through push_patch's scratch branch. WorkItem.wip. A finish pass starts from wip.sha. FACTORY_SALVAGE switch. ADR-0086. RUNBOOK scratch branches. regression_docker timeout case.
- Slices 7 and 8: workitem.py parses Paths lines. WorkOrder constraints.allowedPaths. foreman.check_pushed fences source files (sizing.kind). _plan_problem requires Paths. _wait_for adds the overlap wait. SKILL.md: read the code and list Paths. ADR-0087, ADR-0088.
- Slice 9 (pr-gatekeeper repo): gates.revert_probe checks each new Python test alone on reverted source, with the 'Guard:' exemption, and lists weak tests in the marker's problems. SPEC.md. One worker_input rule in software-factory. It complements the deleted-or-skipped-test static check on branch mimo/b-weakened-tests.
- Slice 10 (optional): workitem.intake reads '## Acceptance tests'. The runner writes pinned tests before the harness and before collect(). ADR-0089.
- Owner config, no PR: FACTORY_WORKERS with pi-glm-factory first and opencode-glm-factory dropped, FACTORY_SOURCE_CHANGED_LINES lowered from 700, FACTORY_LESSONS_FILE set.
- Owner-merged setup PRs on protected paths: the gate pin to pr-gatekeeper main (ADR-0080), and strong_reviewer_model plus sensitive_paths in each repo's .github/pr-gatekeeper.json.
- Unchanged on purpose: store.py tables, leases, workitem.reconcile as the only fact mover, relay.py, the Docker sandbox, patches.apply, the gate's two-job split, the ADR-0068 runtime-check issue, factory_replace_issue and plan allowances.
- Left out for lack of evidence that it pays here: a separate test-author pass, an attacker per node, held-out tests, typed interface edges, a headless sol decide agent, and judge panels or audit loops inside the factory.
- Branch every slice from software-factory origin/main dac66f2. The local checkout is on day-desk-owner-decisions, whose upstream is gone.

## Risks

- Memory on the 16 GB Mac: each worker container may take 6 GB (worker/runner.py:287), and DAG plans run more passes at once. Keep FACTORY_PASSES_PER_REPO at 2, run one live plan at a time during rollout, and watch hw-watch. Add a cross-repo cap only if memory pressure shows.
- Conflicts on shared append-only files: every slice adds an ADR to docs/decisions.md. Same-file issues get an `after` edge (slice 1) and later a Paths overlap wait (slice 8). A conflict that slips through costs one redo pass (ADR-0032).
- Salvaged partial work can carry a wrong approach forward. The finish finding says to judge the code, wip moves at most twice, and FACTORY_SALVAGE=off reverts. Not verified that past timed-out passes left useful edits.
- The per-test proof covers Python first. Agentspace (JavaScript) and ideadump (Swift) keep the suite-level probe until a parser exists.
- A worker can mark a vacuous test 'Guard:' to pass the probe. The reviewer sees the label, and the gauges count Guard tests per PR.
- A third-family strong reviewer on OpenCode Go is not verified. Without one the gate stays flash-tier, and the gains come from small nodes, code-run checks and the fix loop. GPT 6.1 sol cannot review there without a second provider in pr-gatekeeper's llm.py.
- The family filter leaves only GLM builders while the reviewer is DeepSeek. A GLM outage then stops builds until the owner adds a third family or changes the reviewer.
- Self-modification: slices 3 to 8 change Foreman and runner decision logic, the class with the most High findings in the hackathon (7 of 11). Build them with a strong implementer and a different-family reviewer, pilot runner changes on one issue (ADR-0055), and run scripts/rehearsal.py and scripts/regression_docker.py before deploy. A merged change deploys itself (ADR-0031).
- Codex quota is shared by Day Desk's planning and incident answers. The hackathon hit a usage limit and a weekly limit. Sol works only at plan time and at forks.
- Small, confounded samples: 74 worker PRs in three weeks while the gate pin, wall budget and task mix changed. Record the gate pin, worker and wall budget with each gauge window, and judge on the next 30 nodes.
- The local software-factory checkout is on day-desk-owner-decisions, whose upstream is gone. Branch every slice from origin/main dac66f2, or slices will conflict with ADR-0045 to ADR-0081 work.
- Taste-bound UI work still exceeds small workers. The hackathon owner rejected three UI rounds that passed every check. Keep it with sol and the owner's preview.
- Sol-written pinned tests (slice 10) can encode a misread spec. Each wrong test costs up to 2 passes before the incident returns to sol.

## How the three designs scored

- **reliability-first:** reliability with small workers 4/5, code and system quality 4/5, fidelity to the hackathon 3/5, incremental buildability 2/5, owner attention 3/5 (5 = least attention). Total 16/25. Strongest proof chain. Code re-runs every check, tests are frozen, timed-out work is kept, and a breaker stops outage cascades. It is also the heaviest. A medium node gets two extra passes (tests, attack), and 74 of 189 passes already fail. Slices 2 to 5 are 400 to 480 lines of decision logic. Tests-first from another family and an attacker per node go beyond the hackathon. There, the implementer wrote its own tests and proved them red (30 of 73 Agent work orders ask for it, regex over main_calls.json), and an attacker ran once. Its rule that every new test is red at base would also flag must-not-change tests. Grafted: per-test proof run by code, timeout salvage, decision lines from sol, a hard family filter.
- **graph-first:** reliability with small workers 4/5, code and system quality 4/5, fidelity to the hackathon 3/5, incremental buildability 3/5, owner attention 4/5 (5 = least attention). Total 18/25. Best graph model and the best first slice. An `after` list per plan item is about 70 lines, and fan-in already works because _wait_for checks every Depends-on line (foreman.py:557 at dac66f2). It then adds eight node kinds and a headless sol agent that edits the graph unattended. Held-out tests can leak, because the runner fetches every origin branch into the worker copy (worker/runner.py:221). Typed interface edges guard a failure that the gate's behind-base re-review already catches (pr-gatekeeper SPEC.md:460 at c9a6462). Panels and loop-until-dry audits come from 8 workflows, about 15% of hackathon delegations. Grafted: `after` as slice 1, finish orders that trust the code over the dead worker's report, the pre-flight list, gauges per worker family.
- **minimal-change:** reliability with small workers 4/5, code and system quality 3/5, fidelity to the hackathon 4/5, incremental buildability 5/5, owner attention 4/5 (5 = least attention). Total 20/25. Winner. Winner and base of the merge. It is the closest map of what made the hackathon work: issue work orders with paths, a Depends-on graph with a file-overlap check, finding-shaped fix orders, a finish pass, a cross-family rule, and a foreman that decides at forks. Every slice is additive, tested and piloted on the existing state machine. Two gaps. Its runner revert probe repeats the suite-level probe that pr-gatekeeper has run since 2026-09-23 21:15 IST (commit 5617dcc, gates.py revert_probe), though it called the rule text only. The DAG came last (slice 7). The merge moves the DAG to slice 1, makes the gate's probe per test, and routes the gate's problems into fix passes.
