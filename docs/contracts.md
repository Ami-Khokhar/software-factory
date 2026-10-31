# Factory contracts

The interfaces of the software factory. Cells exchange **documents, never
vendor SDKs** — that is what keeps the line harness- and model-agnostic. These
four documents are the only things that cross a boundary.

Status: **v1**. The controller is `factory-mcp` in this repo (ADR-0012). A field
or state is in a schema only if the factory writes it and something acts on it.

See also: [`architecture.md`](architecture.md) · [`decisions.md`](decisions.md).

## Memory evidence feed

`factory_memory_events` is a read tool on both owner and driving tokens. It requires an enabled
`repo`, accepts `after` (audit sequence, default 0), `limit` (1–100, default 50) and optional
`through` (snapshot boundary, at least `after`). Unknown arguments and invalid types are refused
by the handler, not only its schema. It changes no WorkItem and invokes no model.

The response is `{repo, events, through, nextCursor, hasMore}`. Each event carries `seq`, `at`,
`action`, `workItemId` and selected `detail` fields. Pass `nextCursor` as `after` and reuse
`through` until `hasMore` is false; then omit `through` to collect new evidence. Sequences survive
restart but belong to this SQLite database; they are not portable through store loss/rebuild.

Events include dispatches, failed attempts, accepted Results, reconciliations, human resolutions
and question/answer events already in the audit. They are joined to existing WorkItems in the
requested repo. Global events, other repos and tool-argument records are excluded. Nested payloads
and arbitrary keys are omitted; selected strings use diagnostic redaction and a 300-character cap.
Reconcile events also project up to three blocking gate problem strings as `detail.gateProblems`,
each capped at 100 characters and 300 characters total, with the same redaction. Other finding
fields and nested data remain omitted.
This is bounded internal evidence, not full transcripts, full findings or a guarantee that every
kind of private text is removed. Question/answer events may include selected `questionText` and
`answerText`, each sanitized and capped at 300 characters before the live WorkItem field clears.
They remain selected text, not raw transcripts. No memory graph, learned preferences or scheduled
dreaming is enabled by this tool.

## Worker health operations

`factory_check_worker` accepts only a registered worker id and the caller name. It reads the relay
registry, and admits only a Python 3 invocation of this deployment's exact `worker/runner.py`, one
of the `pi`, `codex` or `opencode` harnesses, a pinned model, existing supported runner flags and
copied credential files. Secret-bearing `--env` keys are refused before a child starts. The
operation runs one synthetic probe through `scripts/provider_health.py`, closes child stdin, uses
a fixed deadline, and tags its Docker container with a unique probe label for targeted cleanup.
Only `factory.health/v1` status and allowlisted event fields are returned and stored. Raw child
stdout, stderr, provider text, credential contents and environment values are discarded.

SQLite keeps one latest health or quarantine record per worker. Its binding is a digest over the
registered worker configuration, pinned model, credential paths and file metadata, and Docker
image identity. No credential file contents are read. A record expires after 15 minutes; a later
configuration change yields a different binding. A successful probe creates no WorkOrder and
does not add or change the worker in `FACTORY_WORKERS`.
Credential-file readability is not provider authentication; only a returned `factory.health/v1`
status of `ok` proves the synthetic response was received.

`factory_reload_workers` accepts only the caller name and invokes agent-relay's fixed
`POST /v1/admin/reload` operation. It does not edit the relay registry, change `FACTORY_WORKERS`,
start a task or retry work. Both operations are driving-token writes. Worker activation and
dispatch-preflight consumption of these records are separate operations.

## The four documents

| Document | Direction | Producer | Consumer | Schema |
|---|---|---|---|---|
| **WorkItem** | internal (durable) | controller | controller | [`schemas/workitem.schema.json`](../schemas/workitem.schema.json) |
| **WorkOrder** | controller → worker | controller | worker (via agent-relay) | [`schemas/workorder.schema.json`](../schemas/workorder.schema.json) |
| **Result** | worker → controller | worker | controller | [`schemas/result.schema.json`](../schemas/result.schema.json) |
| **Verdict** | gate → controller | pr-gatekeeper | controller | [`schemas/verdict.schema.json`](../schemas/verdict.schema.json) |

```
            ┌───────────────┐
            │   WorkItem    │  one durable record per deliverable issue
            │   (durable)   │  controller-owned, reconciled from GitHub facts
            └───────┬───────┘
        WorkOrder   │   ▲   Result
        (to worker) │   │   (patch handoff)
                    ▼   │
            ┌───────────────┐
            │    Worker     │  any harness behind the agent-relay adapter
            └───────────────┘
                    ▲
        Verdict     │   (normalized gate decision)
        (from gate) │
            ┌───────────────┐
            │ pr-gatekeeper │
            └───────────────┘
```

## Design rules baked into the schemas

- **The patch is the handoff (ADR-0017).** A `Result` with `status: completed`
  **must** carry `patch` (format + diff, which `worker/runner.py` returns) and a
  passing test in its evidence. factory-mcp pushes the patch itself, after it
  refuses one that touches a protected path or takes the PR over its size limits
  (the line count warns instead: advisory, ADR-0070; the file count binds);
  a worker never pushes. The runner checks an oversized pull request first, before the
  verify runs: the size is the branch's own diff against its merge base with the
  default branch, including the commits a fix follows (ADR-0040), and an exceedance of
  the line target warns while an exceedance of the file count refuses. The Result is
  the last JSON line of the relay task's output; the Foreman reads nothing else
  the worker prints. This is what makes any harness that can commit a
  first-class worker.
- **`Verdict.headSha` is mandatory.** A verdict only applies to the exact commit
  it names. Acting on an older SHA is the stale-review bug, so it is
  structurally rejectable.
- **`WorkItem.wallMsOverride` is an optional persisted per-item wall budget (ADR-0089).**
  Null or absent uses `FACTORY_WALL_MS`. New items and migration 8 initialize it to null.
  `factory_set_timeout` accepts integers from max(60000, the configured default) to 3600000,
  or null, while no worker is live and before a terminal state. The relay's live timeout cap
  must support a newly set value. Clearing needs no relay access. Its `timeout_changed` audit records `was`, `now`,
  `effectiveWallMs`, and `by`. Setting it neither retries nor changes pass accounting or grants.
  New implement and fix WorkOrders copy the effective value into `budget.wallMs`; relay submit
  already uses that same scalar explicitly. A changed relay cap blocks an overridden dispatch
  before saving a WorkOrder or spending a pass and concludes any open recovery. A transport
  failure instead leaves the item pending for the next tick. Existing orders and the global default stay fixed.
- **`WorkItem.pendingRetry` is one durable Day Desk retry intent (ADR-0095).** Null or
  absent means none. `factory_queue_retry` writes it with its request-journal row in one
  transaction, sanitized and bounded, spending nothing and starting nothing: a replay of the
  same `requestId` with the same inputs returns the stored request, changed inputs conflict, and
  a lease held by another tick answers `retryable`/`workitem_busy`. New items and migration 10
  initialize it to null. A human resolution cancels a queued intent and its journal row in the
  same write; spending the allowance and starting the WorkOrder for a queued intent belongs to
  the follow-up consumer, not to this phase.
- **`WorkItem.projectId` names the Project the WorkItem is a member of (ADR-0099, ADR-0100).**
  Null when the WorkItem belongs to no Project, which is what every WorkItem carried before the
  Projects, and what new items and migration 11 initialize it to. `factory_start_project` sets
  it on each plan issue it opens, and a WorkItem the factory takes in later joins the Project
  that already lists it, written with the `project_joined` audit action under the WorkItem's
  own lease. WorkItems without a `projectId` behave exactly as before.
- **`WorkItem.stateVersion` is optimistic concurrency.** Every durable write
  increments it; a transition that writes on a stale version loses.
- **`human_waiting` is not terminal.** `closed`, `cancelled` and `exhausted`
  are; `merged` waits for the issue to close. HUMAN is an action queue with a
  path back.
- **The size target counts source (ADR-0079, over ADR-0070).** `constraints.maxSourceChangedLines`
  (optional, default 700) is the WorkOrder's small-PR target for source additions and deletions
  only; `constraints.maxSourceChangedLinesCeiling` (optional, default 1000) is the reviewable
  ceiling. Test and doc lines are reported separately and never count against the target. Over the
  target warns and continues (unless `maxSourceChangedLinesAdvisory` is false); over the ceiling is
  a size-only refusal the Foreman routes to a bounded, acceptance-preserving decomposition, never a
  fabricated success and never a fresh budget. A WorkOrder without the source fields (older, or
  `maxChangedLinesAdvisory` false) keeps the all-lines `maxChangedLines` meaning, so stored
  documents stay valid. `maxChangedFiles` and the worker's patch-byte ceiling bind in every mode.
- **Acceptance is checkable.** Each `acceptance` item has a `claim` and a
  `doneWhen`: a check read back from the produced artifact, never from effort.
- **`blocked` carries evidence.** A WorkItem in `blocked` must have `blockedOn`
  and at least two `attempts`, so the escalation shows what was tried. A
  WorkItem leaves `blockedOn` behind when the wait ends: a WorkItem that is
  terminal or not waiting has `blockedOn` `null`. WorkItems stored before #110
  could keep a stale `blockedOn`; the store's migration 2 drops it from any
  WorkItem whose state is not `human_waiting` or `blocked`.
- **Budgets have two ceilings.** `WorkOrder.budget` requires `maxPasses` and
  `wallMs`. Reaching `maxPasses` moves the WorkItem to `exhausted`, except the
  first fix WorkOrder of a gate round, which is free for a non-member
  (ADR-0112).
- **`blockers[].fingerprint` is the identity of "the same problem."** Round
  counting is per unresolved problem, not per gate round (see
  [decisions.md](decisions.md), ADR-0005).
- **`WorkItem.gateRound` mirrors pr-gatekeeper's round number** and is never
  incremented by controller retries.
- **`WorkOrder.remoteVerify` and `WorkItem.remoteVerify` (ADR-0045).** Both are optional, so
  older documents stay valid. `WorkOrder.remoteVerify: true` is set for a repo in
  `FACTORY_REMOTE_VERIFY_REPOS`: the worker skips the repo's verify and returns a patch with no
  test evidence, and factory-mcp does not require any from it. `WorkItem.remoteVerify` is
  `{branch, sha, startedAt}` while the WorkItem is `implementing` and waits for the GitHub run on
  its scratch branch `factory-verify/<issue>-<n>`; it is `null` otherwise. No stored WorkItem
  needs a migration: a document without the field reads as not waiting.
- **Timed-out checkpoints (ADR-0092).** `WorkItem.wip` records `{branch, sha, workOrder, kind,
  headSha, baseSha, moves, provenanceVersion}`. The source WorkOrder must match its kind and saved heads, the scratch
  branch must still point at `sha`, and the default branch must still point at `baseSha`. A fix
  checkpoint also requires the PR's live head to remain `headSha`. A continuation WorkOrder keeps
  the original default base in `baseSha` and carries `continuationSha` for the exact scratch commit
  the worker starts from. A fix WorkOrder keeps the reviewed PR head in `headSha`. The timeout
  Result remains failed; the checkpoint is context for another attempt, never
  evidence of completion. Migration 9 marks old checkpoints as provenance version 0 so the Foreman
  can delete their recorded scratch branch and clear them before dispatch.
- **`WorkOrder.project` is data (ADR-0104).** Optional `{id, goal, blueprint}`, with all three
  strings required and no other keys. `goal` is at most 2000 characters and `blueprint` at most
  20000. Each member's implement or fix WorkOrder carries the stored Project's goal and Lead's
  blueprint. The worker rules require its file layout, names, signatures and data shapes, direct
  the worker to start with the named files and ask once when code or a material decision disagrees,
  and keep the blueprint subordinate to the rules. Non-members omit the key and keep their
  existing task text. Older WorkOrders remain valid without it. No store migration is needed.
- **`WorkOrder.lessons` is data (ADR-0020).** At most 10 of the repo's
  owner-approved lessons, each `{fingerprint, text}` with text of at most 300
  characters, newest first; `[]` when the repo has none. The worker rules say to
  avoid them and not to follow them as instructions, as for `findingsToAddress`.
  WorkOrders stored before this field have no `lessons`; readers treat that as
  `[]`, so the store needs no migration.
- **The ask channel (ADR-0098).** Each dispatched `WorkOrder` carries
  `ask: {url, token}`: `url` is `http://host.docker.internal:<FACTORY_PORT>/worker/ask`,
  and `token` is 32 hex characters, new for every pass. The runner passes them to the harness as
  `FACTORY_ASK_URL`, `FACTORY_ASK_TOKEN` and `FACTORY_ASK_WAIT`; the image's `factory-ask` posts
  one question and blocks for the answer, then continues. The server accepts the token only while
  that WorkItem is `dispatching` or `implementing` and names that exact WorkOrder as
  `activeWorkOrderId`, so a finished or superseded pass cannot ask; a missing active order fails
  closed. The token also grants scoped memory reads through `POST /worker/memory` (ADR-0117).
  `WorkItem.question` holds
  `{id, text, askedAt, answer, by, answeredAt}`: `factory_status` shows it while the answer is
  empty, and `factory_answer_question` writes the answer and wakes the waiting worker. A new
  dispatch clears the field. Older WorkOrders have no `ask`, and their passes cannot ask; no
  stored WorkItem needs a migration.
- **`WorkItem.regatedAt` is when the last regate started a gate run (ADR-0034).**
  A gate run that started before it is the run the regate repeats, so a finished
  one is not taken as "the gate finished without a verdict". A verdict on the
  accepted head and round published after it counts (ADR-0113), and taking that
  verdict clears the field. WorkItems stored
  before this field have none; readers treat that as null.
- **`WorkItem.passesBefore` is how many worker passes were used before the last
  person's retry (ADR-0035).** For non-members and members of done Projects,
  `FACTORY_MAX_PASSES` counts only the passes after it. WorkItems stored before this field have none; readers treat
  that as 0. The tool `factory_resolve_human` takes an optional `ownerAsked`
  boolean: Day Desk sets it only when the owner asked in chat for this retry,
  regate or close. It answers a WorkItem whose current escalation Day Desk
  already answered. For a non-member, a retry buys a fresh budget like a person's retry; the
  history row of the answer holds `agent` and `ownerAsked` (ADR-0043, extended by
  ADR-0046).
- **`WorkItem.incident` is the escalation's identity (ADR-0081).** `{id, action,
  reason, since, agentAnswered}` plus the action's own facts: `causes`
  (attempt_failed), `dependency` and `end` (dependency_ended) or `gateRound`
  (reconcile). A write that escalates computes the identity; the same identity
  keeps the record and its `agentAnswered` flag, a materially changed one becomes
  `id + 1` with `agentAnswered` false. For non-members and members of done Projects, Day Desk may answer each incident once
  (`agentMayResolve`, `factory_resolve_human`); a WorkItem that escalated before
  this field falls back to the once-ever rule over its history, so the store needs
  no migration.
- **`WorkItem.recovery` is the open autonomous-recovery decision (ADR-0081).**
  `{id, action, category, fromWorker, toWorker, at}` with `action` one of
  `worker_swap`, `block_deferred` or `relay_lost`, or `null` when none is open. The
  audit holds the decision (`recovery`) and its outcome (`recovery_result`) under
  the same id; the field links the two across ticks. `WorkItem.infraRetries` counts
  the relay losses re-queued for the WorkItem against `RELAY_RETRIES_MAX`, and a
  successful Result resets it. Every write that starts a fresh budget of passes (a
  person's retry, an allowance retry) resets it too, so the bound is per budget,
  not per lifetime. A relay call that fails mid-answer (a restart) surfaces as one
  `RelayError`: the tick is a clean no-op that retries, never a crash. Documents
  without either field read as no recovery and no re-queue.
- **`WorkItem.description` is the issue's text outside its Acceptance section
  (ADR-0038).** Intake freezes it with `goal` and `acceptance`, and each
  WorkOrder carries it to the worker as data. WorkItems stored before this field
  have none; their WorkOrders carry `""`, so the store needs no migration.
- **`WorkItem.waitingOn` is the issue a `ready` WorkItem waits for (ADR-0024).**
  Its issue has a `Depends on #n` line and #n has not merged yet, so nothing is
  dispatched. The Foreman sets it to null when the wait ends. WorkItems stored
  before this field have no `waitingOn`; readers treat that as null, so the
  store needs no migration. `factory_set_dependencies` rewrites the issue's
  `Depends on #n` lines (ADR-0042) and clears `waitingOn`; it also accepts a
  `human_waiting` WorkItem with no worker pass (ADR-0047). The schema does not
  change.
- **An issue's `Worker: <id>` lines choose its worker (ADR-0029).** They are read
  from the issue on each tick, like `Depends on #n`, and are not stored in the
  WorkItem. `WorkItem.worker` still records the agent of the current pass. A name
  outside `FACTORY_WORKERS` is dropped and routing uses the allowed names, or
  every allowed worker when none remains; when a worker runs, the
  `unknown_worker_rerouted` audit row records it, and when none can run, the
  `no_worker` row carries the dropped names as `workers` and `blockedOn` names
  them (ADR-0110).
- **`Result.diagnostics` is a bounded, optional timing report (consumer first).**
  A harness that sends one gets its timings logged; a Result without it — every
  Result today, and any Result the schema change predates — stays valid. The
  shape: `elapsedMs` (integer, >= 0, the only required field), optionally
  `phases` (each `{phase, elapsedMs}` plus an optional `exitCode` and optional
  `tail`) and an optional `tail`. `phase` is one of checkout, preparation,
  environment setup, harness execution, patch collection, verification — the
  boundaries the runner's phases can report. The producer is
  `worker/runner.py` (#192): it emits this on every Result — `elapsedMs` for the whole pass, one
  entry per phase with how long it took, the exit code where there is one, and a tail of its
  output — and it redacts each tail: copied credentials, configured secret values, environment
  values and the WorkOrder's command payload never reach the relay, and a harness timeout keeps
  the output it had printed. The exit code rides where the command has one: a failing checkout
  names its git exit, a setup phase its docker exit, and patch collection the sandbox's exit.
  A setup-phase failure of the runner's `preflight` (#193) is also the environment to the pause
  consumer: the worker checks the Docker daemon, the configured worker image, the harness inside
  it, the configured credential-copy and the base commit's verify command before the coding
  harness runs, and each failing check raises its own code — 69 daemon, 72 image, 71 harness,
  66 credential, 78 verify — so none of the pass's remaining time is spent where the pass starts
  over against the same broken environment.
  Tails are at most 4000 characters so a diagnostic dump cannot
  leak unbounded output or a secret past the relay. Both the schema and the
  Foreman's Result check (which must stay identical) enforce these bounds.
  **Consumed for the attempt audit (ADR-0050).** When an attempt fails, the Foreman names a
  cause only from evidence: a setup or verification phase that failed in the diagnostics, a
  harness execution phase that exited 124 (the runner's own kill for time — the runner spends
  less than the consumer's 90% threshold on the harness, so the exit code is the only evidence a
  real timeout can carry), a missing
  patch on a completed Result, or a test evidence entry that did not pass. Whatever else the
  reason strings say — in particular a timeout — is no cause without such evidence, and the
  cause rides with a sanitized, bounded context (at most 300 characters, credentials, environment
  values and the WorkOrder command payload redacted at the controller) into the audit
  `attempt_failed` entry, the escalation, and the next WorkOrder's `previous-attempt`
  `findingsToAddress` problem text. No schema changes: attempts stay strings and
  `findingRef` is unchanged. The diagnosis also runs on the Results the Result
  check rejected, so it names a cause only from the shapes that check would
  have accepted — it never raises on one — and a test evidence entry without an
  integer non-zero exit is not a failing test.
  **Deployment prerequisite (the hold point, ADR-0052):** the consumer
  deploys first. Do not rebuild the worker image (the producer) until the consumer —
  `factory-mcp` with the Result check, the attempt diagnosis and the environment pause
  (#189–#191) — is already deployed and running. The same hold point holds for every later
  producer change, including the phase exit codes and the timeout-exit rule above: verify the
  deployed `factory-mcp` accepts the new Results before the image is rebuilt. The producer's
  tests were red against the
  runner before the fix, and the gate passed with legacy behavior preserved: a Result without
  `diagnostics` stays valid, checked and merged.
  **A confirmed environment fault pauses (ADR-0051).** When the diagnosis names the
  environment (`checkout or setup`), the WorkItem stops on the first failed attempt:
  it goes to `human_waiting` with the repair reason in `blockedOn`, and the Foreman
  dispatches no worker — a second pass, on whichever worker routing picks, would meet
  the same broken checkout. A `failing test`, an `empty patch` and a `harness timeout`
  are not environment faults and keep the normal recovery (back to `ready`, next pass
  after this one). A person repairs the environment and calls `factory_resolve_human`
  with `retry`, which buys the next pass as before. No schema changes: the pause rides
  the states and fields the WorkItem already has.
- **Unknown properties are rejected** (`additionalProperties: false`) so a cell
  cannot smuggle vendor-specific fields across a boundary.

## WorkItem status

`factory_status` list rows expose `agentMayResolve` for waiting WorkItems. It is true when the
per-item incident rule allows an answer or the Lead may act on an active Project member.
For active members, waiting or exhausted rows also expose `leadBlockedBy`: the safety stop or
environment fault or exhausted lifetime pass cap blocking Lead authority, or `null` when it may
act. A blocked or human_waiting member at the cap may still be closed or regated. An exhausted
member at the cap may not be retried, closed or regated by Day Desk without the owner.
Done Projects use the plain non-member computation.
Rows for paused, stopped, missing and done Projects omit `leadBlockedBy`.

## Runtime status and tool discovery

`factory_runtime_status` is a read-only MCP operation, not a fifth worker
document. It accepts an optional `id` naming a stored WorkItem. Without one it
reports the running factory revision, configured worker order and registered
sandboxed factory-runner status and timeout facts. It reports model, image and
credential-file readability only for a `python3` entry whose first argument is
an absolute `worker/runner.py` path with a harness after `--`. Such registered
workers are enumerated even before they are in `FACTORY_WORKERS`. A configured
worker that does not match this rule is marked `sandboxed: false` with no image
claim; malformed argument lists are marked `invalid_args` with `sandboxed: null`.
Credential paths use the
runner's `expanduser` behavior. The report also includes the relay health and
timeout cap and a comparison of the currently exposed tool
schemas with Hermes' disk cache. With an item id, it also reports the stored
and live PR head and whether comments, check runs and gate workflow runs can be
read for that exact live head. A missing PR has no head-level diagnostics.

Credential availability means only that the configured file is readable; the
report never attempts provider authentication. Credential and environment
values, raw API error bodies, and cache contents are not returned. GitHub
permission failures are reduced to `forbidden`, and other probe failures to
`unavailable`. Cache findings describe the readable disk cache only and do not
claim to describe the active chat's schema. The response advises `/reload-mcp`
when the active chat may have stale tools.

MCP `tools/list` includes `ttlMs: 0` and `cacheScope: "private"` to signal that
tool schemas should not be retained as shared cache entries. These hints do not
refresh an already running client. The caller must reconnect or use
`/reload-mcp` to rebuild its schemas. Runtime status only reads local files,
Docker image metadata and GitHub/relay read endpoints; it does not start a
worker, provider request, retry, allowance spend or host setting change.

## State machine

```
issued → planned → ready → dispatching → implementing → pr_open
                            ▲                                │
                            │                                ▼
                     changes_requested ◀────────────── reviewing
                            │                                │
                            │                                ▼
                            │                          merge_pending
                            │                                │
                            │                                ▼
                 human_waiting ──► (ready | closed)     merged → closed
```

Fault state: `blocked`. Budget ceiling: `exhausted`. Terminal states: `closed`,
`cancelled`, `exhausted`. **`human_waiting` is not terminal.**

## Controller obligations (the guards)

These are correctness rules, not features. They come directly from the review and
must hold before the controller is allowed to write:

1. **Reconcile, don't trust events.** GitHub is authoritative for facts;
   `repository_dispatch` is a wake-up hint. A periodic reconcile is the truth.
2. **Guard every transition.** Fetch the PR and confirm
   `repo + prNumber + headSha + round` match the WorkItem. Ignore stale or
   duplicate verdicts.
3. **Single writer.** Acquire a per-WorkItem lease before dispatching; at most
   one active WorkOrder per branch.
4. **Idempotent dispatch.** Every WorkOrder has a stable `idempotencyKey`. On a
   relay timeout, inspect the branch/commit/PR before retrying.
5. **`merge_pending` before `merged`.** Only observe `merged` after GitHub
   confirms the merge on the reviewed SHA; running a merge and observing a merge
   are different facts.
6. **Acceptance travels with the work.** The WorkOrder and the PR body carry
   the issue's acceptance criteria, and the gate reviews the PR against them.
   The Foreman does not re-check them after the merge: `closed` means the PR
   merged and the issue closed, and a merge without the gate's MERGE verdict is
   recorded in `terminalReason`.
7. **A replacement request is journalled before it runs.** A durable operation is keyed by
   its stable request ID with the digest of its approved inputs in canonical form. Opening
   it again with the same inputs resumes the journalled operation at its last phase; the
   same ID with different inputs is a conflicting reuse and fails, so a retry can never
   masquerade as a different operation. Its phases (`started`, `pushing`, `done`) only move
   forward, one at a time, and the store commits each with `synchronous = FULL` in
   rollback-journal mode, so a restart after an interruption finds the last phase and a copy
   of `factory.sqlite3` alone is the whole store.
8. **A replacement is planned and validated before it writes.** `factory_replace_issue` (ADR-0059)
   runs without the owner when it is a split: the replacements together carry the original
   issue's `## Acceptance` bullets, with every replacement carrying at least one (ADR-0097). Any
   other replacement changes what the work is and carries the owner's approval.
   The call reads the whole dependency graph of the repo's WorkItems and computes every edge change before
   anything is written: the original must be waiting on recovery with no live worker pass (an
   active WorkOrder, a live relay task or a state that would dispatch one; failed attempts with
   nothing running on it are history, not a live worker), a dependent
   that had a pass is refused, a replacement that had a pass is refused, a replacement that waits
   for the original is refused, and the planned graph must be cycle-free. A refusal makes no GitHub call that changes anything. The
   plan itself is a pure function (`workitem.replacement_plan`), not a tool. A replacement may also be a title-and-body plan item the
   tool opens itself, as a `factory:proposed` issue that carries the durable marker
   `<!-- factory replace <requestId> <position> -->` (ADR-0064): before it creates, the call verifies that marker, the title and the
   wording against the journalled request and adopts the exact issue an earlier attempt or a lost create response already opened, so
   a replay never opens a duplicate, and an issue whose words no longer match what the request named refuses instead of being
   overwritten. Two issues with the same marker refuse. Nothing labels a created proposal
   `factory:ready`: readiness only comes from factory_approve_issue as usual.
   Each dependent the plan re-points is written and then verified by exact readback (ADR-0065):
   the body GitHub holds after the edit must be exactly the body the call computed — the moved
   edge, the dependent's other `Depends on` lines and its text — or the write was lost or drifted
   and the call refuses, leaving the barrier and the pushing journal exactly as a mid-run kill
   leaves them; the replay recomputes the same plan from the paused graph, re-reads each body and
   sets the whole `Depends on` list again, so a replayed edge is never doubled and an unrelated
   edge is never lost.
9. **A pause barrier holds until an explicit release.** A recovery operation may record a
   durable pause barrier in the store: the graph it holds (`original`, `replacement`,
   `dependent` roles, each one WorkItem id or a list of them (so a plan may hold several
   replacements or dependents) tied to its journalled request. The barrier survives a restart and
   the journal decides whether it can clear: releasing it only succeeds once the operation has
   reached the `done` phase, and a failed or incomplete attempt cannot silently release it. The
   barrier's rows, and the release with its audit row, are written in one SQLite transaction, so
   an interrupted write leaves no partial barrier behind and a corrected retry can finish it.
   The Foreman enforces the barrier (ADR-0061): a WorkItem named in an active barrier dispatches
   no worker pass — the dispatch decision refuses, and the check is repeated immediately before
   the relay call, so a barrier recorded after the decision still holds. Work the barrier does
   not name dispatches as usual; releasing the barrier lets the held WorkOrder submit on the next
   tick, with no new pass spent. A replacement operation records its barrier before any GitHub
   write, over the original, the replacements and the dependents its plan re-points, so a tick,
   a rebuild or a restart during the operation takes the ready-labelled graph members in without
   dispatching them (it also holds the numbers a planned replacement was created under, whose
   creation is safe before the barrier because a proposal can never dispatch, ADR-0064); and a
   call that finds its journal at the done phase with the barrier still
   active completes the release, so a kill between the two writes leaves nothing held. A repeat of
   the same replace call is the way out of any other interrupted run too: a partly run journal is
   replayed, or — when a person moved a member meanwhile — ends the operation where it stands and
   releases what it held (ADR-0063).
10. **A plan allowance recovers only its approved members, and only before the budget is
   spent** (ADR-0069, extended by ADR-0075). One approval per plan id names the repo the plan
   covers and, optionally, the exact WorkItem members it allows; that membership is checked at
   the spend itself, inside the count's transaction, so a use of one WorkItem can never pay for
   recovery of an unrelated repo or an unapproved member, and a stored record without a scope —
   one the older factory wrote — refuses every spend until the owner approves a new scoped plan
   id. The approval is durable and survives a restart. A use of `factory_use_allowance` also
   refuses an item that already used its per-item pass budget before any count moves or any
   state does: the allowance recovers an attempt, never buys fresh budget, and an exhausted item
   goes back to the owner.

## Project

A Project (ADR-0099) is one named goal with a shared budget of worker passes, stored beside the
WorkItems in the `projects` table as its whole JSON document, with the same optimistic
`stateVersion`. `schemas/project.schema.json` is the contract; `factory_mcp/projects.py` holds the
limits and the read tool `factory_project_status` is in `tools.AGENT_TOOLS`. The owner's one
quoted yes (`ownerWords`) starts one, with `factory_start_project`, also in `tools.AGENT_TOOLS`:
it opens at most 9 plan issues in order through `factory_start_plan`'s checks, then one acceptance
issue last, depending on every plan issue (ADR-0105). It stores the record, makes each opened
WorkItem a member with its `projectId` set (ADR-0100), and sets `acceptanceItem` to the last member.
Re-running the same request — the same resolved repo, title, goal, blueprint, doneWhen checks,
budget and plan —
resumes the joins that died, answers the stored record and adds no second `project_started`
audit row; any other call for a taken id is refused: creating a second with the same id
conflicts. Dispatch charges `passesUsed` and pauses a spent budget; the owner can stop or
extend the Project (ADR-0101). At the end of a full tick, `_advance_projects` writes `state`
and `pausedReason`: a closed acceptance WorkItem makes the Project `done` and clears the
reason. A missing, cancelled or exhausted acceptance item, or a passed deadline, makes it
`paused` with a reason (ADR-0105). A legacy null `acceptanceItem` with members adopts the last
member on resume or advancement, without opening another issue. The keys:

| Key | What it means |
|---|---|
| `schemaVersion` | Always `factory.project/v1`. |
| `id` | 3 to 41 characters of lowercase letters, digits and dashes, starting with a letter or digit (pattern `^[a-z0-9][a-z0-9-]{2,40}$`). One Project per id; creating a second with the same id conflicts. |
| `repo` | The GitHub repo the Project works on, `owner/repo`. |
| `title` | 1 to 100 characters: the goal's name in the owner's notices. |
| `goal` | 1 to 2000 characters: what done means, in the owner's words. |
| `blueprint` | 1 to 20000 characters of Markdown: the Lead's design (modules, files, function names, data shapes, order of work). It rides in every member's WorkOrder (WO-06), so a worker does not itself have to plan. |
| `doneWhen` | 1 to 5 `{claim, command}` checks; the acceptance issue runs them (WO-07). |
| `budget` | `{passes, deadline}`: the whole Project's budget of worker passes. Starting budgets are 1 to 60. Owner extensions add 1 to 60 passes and may take the total above 60 (ADR-0101). A supplied replacement deadline is an ISO date-time with timezone. |
| `passesUsed` | The passes charged so far. Starts at 0. |
| `state` | `active`, `paused`, `done` or `stopped`. Start writes `active`; dispatch pauses a
spent budget; the owner stops a Project; full ticks write `done` or `paused` (ADR-0105). |
| `pausedReason` | Why the Project is `paused` or `stopped`: how it stalled. An extension
clears it. |
| `members` | The WorkItem ids of the Project, unique. The Lead adds them as it starts issues (WO-02); a split's replacements inherit the Project (WO-04). |
| `acceptanceItem` | The one member that carries the final acceptance checks; the Project ends when its issue merges (WO-07). |
| `approval` | `{by, words, at}`: who said go and what they said. |
| `stateVersion` | Optimistic-concurrency version; the store writes version + 1. |
| `createdAt`, `updatedAt` | When the Project was written. `projects()` reads in `createdAt` order. |

Active Project members charge this shared budget just before a WorkOrder is saved, after worker
routing and timeout checks. The charge commits in one transaction. A spent budget commits a
pause and refuses the next pass, exhausting that member. A paused or stopped Project holds its
members without dispatch writes. Each member also has a lifetime cap of 8 passes, regardless of
`passesBefore`. Non-members, missing Projects and done Projects keep `FACTORY_MAX_PASSES`.
`factory_extend_project` and `factory_stop_project` are owner-only tools. An extension resumes
paused and stopped Projects without resetting `passesUsed` or any member's lifetime count.
A done Project refuses extension with a ToolError directing the owner to
`factory_project_status`. The stored Project schema permits totals above 60. The start limit
lives in `factory_start_project` and `projects.problem` (ADR-0101).

Day Desk's budget checks in `queue_retry` and `use_allowance`, and the audit budget in
`_recovery_budget`, remain per-item: they use `FACTORY_MAX_PASSES` and `passesBefore` for members.
`resolve_human` instead uses Lead authority for active Project members, allowing repeated answers
within the Project budget except safety stops, environment faults and the lifetime pass cap.
An owner retry does not replenish a member's shared Project budget or lifetime cap (ADR-0101).

## Versioning

- The `schemaVersion` const is the version. Every producer and consumer of
  these documents is in this repo and ships with it, so a change keeps `v1`
  when every producer here meets it; the tests prove they do. A breaking change
  to a document produced outside this repo would become `v2`.
- `factory_mcp/store.py` migrates stored WorkItems when the store opens, so a
  removed field does not linger in them.
- A consumer must reject a document with an unknown `schemaVersion` rather than
  guess.

## Versioned context graph (ADR-0116)

`factory_memory_put` is owner-only. Required fields: `repo`, stable `key`, `kind`, `text`
(1–1000 characters) and `source` (1–500). Optional `projectId` must name a Project in that repo.
Kinds: preference, decision, learning, blocker, fix, evidence. Status defaults to `candidate`;
`approved`, `contradicted` and `retired` are explicit owner decisions. Source records provenance,
not proof of correctness. Private-key/token shapes, credential assignments and authenticated URLs
are refused; ordinary security prose and nonsecret configuration examples are allowed. This is not
exhaustive PII scanning. Assignment-shaped credential labels are conservatively refused even when
used as ordinary prose; rephrase guidance as sentences.
The audit omits memory content/source; the graph itself retains them and needs private storage.

`version` is 0 for creation, or the current version for an update; stale writes fail atomically.
Identity is derived from repo/project/key. Kind and scope cannot change. Up to 10 `{id, relation}`
links may point to records in the same project or repo scope; repo records cannot link to private
project records. Versions and previous sources remain readable after an update.

`factory_memory_search` is available to owner and driving tokens: `repo`, optional `projectId`,
`query` (up to 200 characters), `limit` (1–20), `approvedOnly`, or `id` and historical `version`.
Search requires all case-folded words, treats wildcards literally, and prefers preferences.
The response is `{memories, hasMore}`; each memory contains `id`, `repo`, `projectId`, `key`,
`kind`, `status`, `text`, `source`, `version`, `links` and `at`. `limit` defaults to 10.
Contradicted measurements remain inspectable with history; `approvedOnly` and initial worker
packets exclude them. Outcome reporting preserves the contrary evidence (ADR-0121).
Repo-only queries omit project records. Project queries include shared repo records. Follow a
returned link by ID with the same scope. Empty results do not distinguish hidden from absent IDs.
These tools manage knowledge and provenance. Worker delivery is described below.

## Worker memory retrieval (ADR-0117)

New WorkOrders may carry `memory`: up to six approved `{id, version, kind, text, source}`
records, with at most 4000 characters across encoded records. Retrieval uses repo/project scope,
preferences and task-word matches; it makes no model call. Stored WorkOrders preserve what the
worker received even when the current memory changes. Old WorkOrders omit this optional field.

`POST /worker/memory` accepts the live pass token plus `query`, `id` and `limit` only. The server
selects the pass's repo/project; a worker cannot choose another scope or historical version.
Candidate, retired and contradicted records remain labeled as such when explicitly searched;
they are data, not permission. Reads log returned memory IDs/versions, not query text or content.
A completed or superseded pass loses access, even while a later pass for that WorkItem is running.
This extends the per-pass capability beyond the ask route to scoped context reads; it still grants
no owner tools or writes.
The HTTP reader returns 400 for invalid arguments, 403 for an inactive capability and a generic
500 for an internal failure, without exposing stored context.

The image's `factory-memory 'query words'` and `factory-memory --id ID` use the same environment
capability as factory-ask. Failed retrieval is explicit and exits nonzero; it does not invent
context or block the worker from using its original WorkOrder. Rebuild the worker image to
install this command; updating factory-mcp alone does not update running worker images.

## Dream eligibility (ADR-0118)

`factory_dream_status` reads eligibility and the next eligibility timestamp without a model call.
The internal claimant writes a run and reserves exactly seven days atomically, across connections.
A failure or crashed claimant keeps that cooldown. No evidence means no claim. A planned run remains
held until the publication consumer records completion; it does not age out or allow another model
request. Timestamps are UTC epoch seconds.

Each claim contains at most 20 selected events per repo and 12,000 encoded event bytes across
repos. Collection takes turns across repos and rotates the starting repo between runs so a busy
repo cannot monopolize the window. Each repo keeps a contiguous prefix. Only incorporated sequences advance after successful planning/completion; failures leave
all evidence pending. This is an evidence window, not a sliding seven-day cutoff. Run history and
cursors are additive SQLite tables; no WorkItem document changes. Claims do not create issues.

## Bounded reflection (ADR-0119)

Owner-only `factory_dream_run` uses `FACTORY_DREAM_URL` (full OpenAI-compatible chat-completions
URL), `FACTORY_DREAM_MODEL`, and optional securely configured `FACTORY_DREAM_API_KEY`. Without
endpoint/model it returns not_configured and consumes no cooldown. Only HTTPS is accepted except
loopback test servers; redirects are refused. The request has at most 22,000 bytes and 2048 output
tokens, one attempt, a 60-second socket timeout and a 64-KiB response bound. No raw provider error
body is logged. The nightly no-agent script calls this tool; skipped checks make no model request.

Output must contain at most three improvements and five learnings. Every candidate must cite real
same-repo audit IDs. Improvements require bounded causes, file paths and acceptance checks; inferred
preferences remain candidates. Evidence, blockers and learnings become linked graph records with
source IDs. Prior repo context is included only within the input budget. Invalid output keeps the
cooldown and unprocessed evidence. A valid plan is saved before later issue publication.
Question/answer audit events now keep sanitized bounded text before the live question field clears.
This is selected context, not a transcript archive or exhaustive private-data classification.

## Improvement publication (ADR-0120)

factory_dream_run now publishes a saved plan before starting any new reflection. Each issue has
acceptance, source issue links, path fences and a deterministic marker. Publication intents are
saved before GitHub creation. A retry searches all issue states, adopts a trusted matching issue,
and never blindly repeats an ambiguous create. A marker that never becomes visible leaves
publication pending for operator diagnosis; no model call is made to retry publication.

FACTORY_DREAM_AUTO_REPOS is a comma-separated opt-in subset of enabled repos. By default all
improvements remain factory:proposed. At most one repair per run may be admitted automatically,
with no concurrent automatically admitted repair. Protected/credential paths, complex acceptance
commands, edited/closed issues, untrusted authors and descendants of failed improvements are not
automatically admitted. Eligible work passes through approve_issue and normal worker/gate budgets;
dreaming never merges code or replenishes exhausted budgets. Ambiguous admission stays reserved.
An exact related gate follow-up is reused without editing its body and needs owner review.

## Improvement outcomes (ADR-0121)

`factory_improvement_status` is a read tool for one enabled repo, bounded to its latest 100
publication records. It separates WorkItem implementation state from runtime effect. It reports
worker passes, failed passes, submitted/read memory exposure and failures later in the same audit
sequence. Exposure is not proof the agent used advice, and a later failure is not necessarily
same-cause recurrence. A quiet period or zero exposure leaves effect unknown.

Nightly maintenance links merged repairs to graph fix records, preserving that deployment and
runtime effects remain unverified. Owner-only `factory_improvement_validate` attaches a full
reported deployed revision, source, baseline opportunity/recurrence counts and after counts.
Both denominators must be positive. It produces a versioned learning and labels its origin
owner_reported; this tool does not execute acceptance commands or prove deployment ancestry.
A later measurement can contradict the prior claim; history remains. Pass version for updates.

For tracked measured repairs, nightly maintenance checks relevant target paths against the
current default-branch comparison. Changed/renamed paths, a comparison at GitHub's file cap,
missing path metadata or unavailable comparison require revalidation and demote approved measured
advice to candidate. Merge facts remain facts. Checks are bounded to the status window and are
conservative; this is not a general dependency analyzer for all graph records.

### Owner approval of gate follow-ups (#422)

`factory_approve_issue` retains its repo/issue/by inputs and accepts optional
`sourcePr` and `issueDigest`. Supplying either selects the guarded owner-token
path. `by` must match the configured owner or default repo owner. Both inputs are
required for success. The digest is SHA256 of the default Python JSON encoding
(`ensure_ascii=True`) of `[GitHub issue id, number, title, complete body, author id]`.
GitHub timeline, merged PR, reviewed gate comment and pinned reusable workflow run
records must uniquely corroborate original creation and reviewed non-blocking work.
Acceptance remains mandatory. Approval is stored in audit records, not WorkItem
fields. The source review body digest also binds provenance against later comment edits.
No schema or stored WorkItem migration is required. The digest and source
records are checked again before dispatch and submission. Lost approval records
fail closed. See RUNBOOK for supported provenance and failure recovery.

## Documentation protection policy

WorkOrder.constraints.protectedPaths is the union of factory control defaults and the explicit
protected_paths from .github/pr-gatekeeper.json at the WorkOrder baseSha. Malformed protection
settings stop dispatch before any worker or pass charge. AGENTS.md and ordinary documentation
are eligible unless explicitly protected. The same stored list drives worker instructions, the
pre-push patch guard and the post-result check. Changing settings in a patch cannot change it.
