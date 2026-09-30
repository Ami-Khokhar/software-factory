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
  refuses one that touches a protected path; a worker never pushes. The Result is
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
  and at least two `attempts`, so the escalation shows what was tried.
- **Budgets have two ceilings.** `WorkOrder.budget` requires `maxPasses` and
  `wallMs`. Reaching `maxPasses` moves the WorkItem to `exhausted`.
- **`blockers[].fingerprint` is the identity of "the same problem."** Round
  counting is per unresolved problem, not per gate round (see
  [decisions.md](decisions.md), ADR-0005).
- **`WorkItem.gateRound` mirrors pr-gatekeeper's round number** and is never
  incremented by controller retries.
- **`WorkOrder.lessons` is data (ADR-0020).** At most 10 of the repo's
  owner-approved lessons, each `{fingerprint, text}` with text of at most 300
  characters, newest first; `[]` when the repo has none. The worker rules say to
  avoid them and not to follow them as instructions, as for `findingsToAddress`.
  WorkOrders stored before this field have no `lessons`; readers treat that as
  `[]`, so the store needs no migration.
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

## Versioning

- The `schemaVersion` const is the version. Every producer and consumer of
  these documents is in this repo and ships with it, so a change keeps `v1`
  when every producer here meets it; the tests prove they do. A breaking change
  to a document produced outside this repo would become `v2`.
- `factory_mcp/store.py` migrates stored WorkItems when the store opens, so a
  removed field does not linger in them.
- A consumer must reject a document with an unknown `schemaVersion` rather than
  guess.
