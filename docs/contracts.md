# Factory contracts

The interfaces of the software factory. Cells exchange **documents, never
vendor SDKs** — that is what keeps the line harness- and model-agnostic. These
four documents are the only things that cross a boundary.

Status: **v1**. The controller is `factory-mcp` in this repo (ADR-0012). A field
or state is in a schema only if the factory writes it and something acts on it.

See also: [`architecture.md`](architecture.md) · [`decisions.md`](decisions.md).

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
  refuses one that touches a protected path or takes the PR over its size limits;
  a worker never pushes. The runner rejects an oversized pull request first, before the
  verify runs: the size is the branch's own diff against its merge base with the
  default branch, including the commits a fix follows (ADR-0040). The Result is
  the last JSON line of the relay task's output; the Foreman reads nothing else
  the worker prints. This is what makes any harness that can commit a
  first-class worker.
- **`Verdict.headSha` is mandatory.** A verdict only applies to the exact commit
  it names. Acting on an older SHA is the stale-review bug, so it is
  structurally rejectable.
- **`WorkItem.stateVersion` is optimistic concurrency.** Every durable write
  increments it; a transition that writes on a stale version loses.
- **`human_waiting` is not terminal.** `closed`, `cancelled` and `exhausted`
  are; `merged` waits for the issue to close. HUMAN is an action queue with a
  path back.
- **Acceptance is checkable.** Each `acceptance` item has a `claim` and a
  `doneWhen`: a check read back from the produced artifact, never from effort.
- **`blocked` carries evidence.** A WorkItem in `blocked` must have `blockedOn`
  and at least two `attempts`, so the escalation shows what was tried. A
  WorkItem leaves `blockedOn` behind when the wait ends: a WorkItem that is
  terminal or not waiting has `blockedOn` `null`. WorkItems stored before #110
  could keep a stale `blockedOn`; the store's migration 2 drops it from any
  WorkItem whose state is not `human_waiting` or `blocked`.
- **Budgets have two ceilings.** `WorkOrder.budget` requires `maxPasses` and
  `wallMs`. Reaching `maxPasses` moves the WorkItem to `exhausted`.
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
- **`WorkOrder.lessons` is data (ADR-0020).** At most 10 of the repo's
  owner-approved lessons, each `{fingerprint, text}` with text of at most 300
  characters, newest first; `[]` when the repo has none. The worker rules say to
  avoid them and not to follow them as instructions, as for `findingsToAddress`.
  WorkOrders stored before this field have no `lessons`; readers treat that as
  `[]`, so the store needs no migration.
- **`WorkItem.regatedAt` is when the last regate started a gate run (ADR-0034).**
  A gate run that started before it is the run the regate repeats, so a finished
  one is not taken as "the gate finished without a verdict". WorkItems stored
  before this field have none; readers treat that as null.
- **`WorkItem.passesBefore` is how many worker passes were used before the last
  person's retry (ADR-0035).** The budget of `FACTORY_MAX_PASSES` counts only the
  passes after it. WorkItems stored before this field have none; readers treat
  that as 0. The tool `factory_resolve_human` takes an optional `ownerAsked`
  boolean: Day Desk sets it only when the owner asked in chat for this retry,
  regate or close. It answers a WorkItem Day Desk already answered once, and a
  retry buys a fresh budget like a person's retry; the history row of the answer
  holds `agent` and `ownerAsked` (ADR-0043, extended by ADR-0046).
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
  WorkItem. `WorkItem.worker` still records the agent of the current pass.
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
   reads the whole dependency graph of the repo's WorkItems and computes every edge change before
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

## Versioning

- The `schemaVersion` const is the version. Every producer and consumer of
  these documents is in this repo and ships with it, so a change keeps `v1`
  when every producer here meets it; the tests prove they do. A breaking change
  to a document produced outside this repo would become `v2`.
- `factory_mcp/store.py` migrates stored WorkItems when the store opens, so a
  removed field does not linger in them.
- A consumer must reject a document with an unknown `schemaVersion` rather than
  guess.
