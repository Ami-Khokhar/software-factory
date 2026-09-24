# Software Factory — Implementation Handoff (rev 2)

**Objective (unchanged):** turn the existing review/merge cell (pr-gatekeeper) and multi-agent
execution bus (agent-relay) into a working software factory whose **control plane is
OpenBot**, so that a human states a goal and the system drives it to a reviewed,
merged PR, escalating to a human only on true exceptions.

**Status:** design frozen through Phase 0; OpenBot selected as the control plane;
plan revised 2026-09-24 (rev 2, see §0). Offline implementation in progress: see §13.

**Audience:** the implementing agent (or human) who picks this up next. Any harness
(Claude, Codex, pi, OpenCode) must be able to execute this document alone.

---

## 0. What changed in rev 2 and why

Rev 1 was checked against the agent-relay repo and the local OpenBot clone. Five facts
forced changes. The goal, D1–D12 and the non-negotiables are unchanged.

| # | Fact found | Rev 2 change |
|---|---|---|
| F1 | OpenBot's MCP client speaks **Streamable HTTP only** (`openbot/server/src/plugins/mcp.ts`). agent-relay's MCP server is **stdio only**. Rev 1 step 3 cannot work as written. | The Foreman becomes **`factory-mcp`**, a Streamable-HTTP MCP server in this repo. It calls agent-relay's **HTTP API** and GitHub directly. OpenBot registers one custom MCP server: `factory-mcp`. (ADR-0012) |
| F2 | OpenBot treats custom-server tools as **writes** unless classified as reads, and it **ignores `readOnlyHint`** (`mcp.ts` `declaredEffect`: only `destructiveHint: true` is read). A custom server URL must be **`https`** with a **dotted, non-internal hostname**: IP literals, `localhost`, `.local`, `.internal`, `.localdomain`, `.svc` and single-label names are refused (`catalogue.ts` `customUrlRefusal`). `AGENT_COMPUTER_ALLOW_PRIVATE_HOSTS` does **not** relax this. OpenBot always sends a bearer token to a custom server. | `factory-mcp` exposes few tools and publishes `readOnlyHint` for its reads (standard, harmless), but policy must treat every `factory-mcp` tool as a write. `factory-mcp` requires a bearer token (`FACTORY_MCP_TOKEN`) and can serve TLS (`FACTORY_TLS_CERT`/`FACTORY_TLS_KEY`). Live setup needs an https name OpenBot accepts, e.g. an `/etc/hosts` name like `factory.lan.example` with an `mkcert` certificate (`NODE_EXTRA_CA_CERTS` for OpenBot), or a tunnel. Bots get **no** direct relay or GitHub write tool. |
| F3 | The OpenBot clone is **12 commits ahead of upstream** (Jobs, Telegram, plan shim are local only) and has uncommitted edits. Putting WorkItem fields into OpenBot's Postgres needs a fork of an alpha codebase. | **WorkItem state lives in `factory-mcp`** (SQLite, stdlib), is keyed by WorkItem id, and is **rebuildable from GitHub** (D8). An OpenBot Job carries only the WorkItem id and the goal. OpenBot stays unmodified except the tenant package. (ADR-0012) |
| F4 | Rev 1 put the tenant package inside the OpenBot clone, where it had no gated home. | Tenant package source lives here at `tenant/factory/`; `scripts/install-tenant.sh` links it into `openbot/examples/factory/`. Every factory change is a PR on **this** repo. |
| F5 | GitHub Actions cannot reach a laptop, so `repository_dispatch` → OpenBot is not deliverable for a local deployment. OpenBot routines fire at most every 15 min. | **Polling is the truth path**: an OpenBot routine calls `factory_tick` every 15 min; a local `factory-mcp` poller (default 60 s) wakes the tick sooner. The dispatch bridge moves to "later, needs a public endpoint". |

Two more constraints shape the schedule:

- **Build vs. prove.** Every phase has an **offline proof** (unit/contract tests with fake
  GitHub + fake relay; runs in CI and in a cloud session) and a **live proof** (needs the local
  OpenBot stack, agent-relay workers, real repo). Cloud sessions deliver offline proofs; live
  proofs run on the owner's machine.
- **Claude ends 2026-09-28 (D11).** After that date, work continues with Codex/pi/OpenCode.
  Each phase is filed as a **GitHub issue** in this repo with its definition of done, so any
  harness can pick up the next one.

---

## 1. Locked decisions

Do not relitigate these; if one is wrong, stop and report it.

| # | Decision | Ref |
|---|---|---|
| D1 | One deliverable issue = one branch = one PR; epics are a parent goal + child issues | ADR-0002 |
| D2 | The **controller opens the PR** after validating the worker artifact | ADR-0003 |
| D3 | **pr-gatekeeper is the only routine merger**; humans resolve HUMAN then re-gate | ADR-0004 |
| D4 | Round counting is **per unresolved problem**, not per gate round | ADR-0005 |
| D5 | **HUMAN is a queue, not a terminal state**; it has an owner and a path back | ADR-0006 |
| D6 | Workers are least-privilege; issue text/findings are **data, not instructions** | ADR-0007 |
| D7 | Branch protection requires gate checks — independent backstop | ADR-0008 |
| D8 | **Reconcile over events**: GitHub is facts; events are wake-up hints | ADR-0009 |
| D9 | Agent registry has one home: `~/.config/agent-relay/agents.json` | ADR-0001 |
| D10 | **OpenBot is the control plane**, but control decisions and merge authority stay deterministic/policy-enforced (not model-decided) | ADR-0010 |
| D11 | **No Anthropic dependency** — the Claude subscription ends 2026-09-28; everything must run on OpenCode Go / OpenAI / local providers | ADR-0011 |
| D12 | Providers are swappable and non-load-bearing; the router degrades on provider failure | ADR-0011 |
| **D13** | **The Foreman is `factory-mcp`**: a Streamable-HTTP MCP server that owns the WorkItem state machine and store; OpenBot Bots call it, never the relay or GitHub write APIs directly | ADR-0012 |

---

## 2. Current state (Phase 0 — done)

| Artifact | Path | State |
|---|---|---|
| Four frozen contracts | `schemas/{workitem,workorder,result,verdict}.schema.json` | Well-formed; test in `tests/test_schemas.py` |
| Contracts overview | `docs/contracts.md` | Written |
| Twelve ADRs | `docs/decisions.md` | Written |
| Architecture diagram | `docs/architecture.html` | Written (needs the rev 2 update) |
| Registry SSOT | `~/.config/agent-relay/agents.json` | Established; verified |
| Gate caller | `.github/workflows/pr-gatekeeper.yml`, `.github/pr-gatekeeper.json` | Installed; **needs repo secrets** (§10 step 0) |

**Registry (canonical):** `codex`, `opencode`, `pi`, `deepseek-harness`,
`deepseek-harness-flowy`, `pi-glm`, `pi-deepseek`. No `claude`. No Anthropic.

**Found during review (not yet fixed):**

- agent-relay `default_config_path()` (`src/server.py:108`) silently falls back to the checkout
  registry. Make it warn/fail loud. First dogfood PR, on agent-relay.
- agent-relay `main` has **no branch protection**, so D7 is not met there yet.

### Contract edits (done in Phase 0′, #1)

1. `WorkItem.acceptance` → each item carries a checkable **`doneWhen`**, read back from the
   produced artifact, not from effort.
2. `WorkItem` blocked → require **`blockedOn`** plus **`attempts[]` (≥2)**, mirroring OpenBot's
   `job_blocked`.
3. Add terminal state **`exhausted`** (budget hit, neither done nor blocked).
4. `WorkOrder.budget` → two ceilings: **`maxPasses`** and **`wallMs`**.
5. `Result` handoff → allow **`artifact {branch, commitSha}` OR `patch {format, diff}`**
   (hosted providers like Codex Cloud return a diff).
6. Generalize `WorkItem.relayTaskId` → **`provider` + `providerTaskId` + `sessionHandle` +
   `sessionNative` + `sessionUrl`**.

Each edit has a positive and a negative instance in `tests/fixtures/edit*.json`.

---

## 3. Target architecture

```
                              HUMAN
                                │ intent · approvals (OpenBot app / Telegram)
                                ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  OpenBot control plane (unmodified + tenant/factory)          │
        │   Jobs (goal, budget)   Foreman Bot    Planner Bot             │
        │   Governance + audit    Routines       Channels / HITL         │
        └──────────────────────────────┬───────────────────────────────┘
                     Streamable HTTP MCP│ (governed, audited)
                                        ▼
        ┌──────────────────────────────────────────────────────────────┐
        │  factory-mcp  (this repo)                                     │
        │   state machine + guards   WorkItem store (SQLite)             │
        │   reconcile/tick           poller (wake-up only)               │
        └──────┬──────────────────────────┬──────────────────────┬─────┘
       HTTP    │                   REST/gh│               read   │
               ▼                          ▼                      ▼
        agent-relay :43124         GitHub issues · PRs     pr-gatekeeper verdict
               │                    (facts)                marker + check-run
               ▼
        workers pi/codex/opencode …
```

- **OpenBot** = durability of the *goal*, governance/audit, HITL, routines, UI.
- **factory-mcp** = the deterministic Foreman: every state transition and every side effect.
- **agent-relay** = worker bus, reached over HTTP (`/v1/agents`, tasks API).
- **pr-gatekeeper** = external gate + merge authority; unchanged.
- **GitHub** = source of truth for facts.

---

## 4. OpenBot mapping

| OpenBot | Factory |
|---|---|
| **Job** (goal, passes/hours budget, `exhausted`) | Wraps one WorkItem; the job goal names the WorkItem id |
| `job_done` | `factory_tick` reports the WorkItem `merged` + issue closed |
| `job_blocked` (what you must supply + ≥2 attempts) | WorkItem `human_waiting` or `blocked` (D5); the attempts come from `factory_tick` history |
| `job_checkpoint` | Records the last tick summary; real state is in `factory-mcp` |
| **Governance gateway** (CEL, audit, fail-closed) | Allows only `factory-mcp` tools; denies everything else for factory Bots |
| **Coworkers** | **Foreman** Bot (calls `factory_*`), **Planner** Bot (goal → issue proposal) |
| **Channels** (app, Telegram) | Escalation + ops console |
| **Routines** | `factory_tick` every 15 min (OpenBot floor) |
| **MCP servers** | `factory-mcp` only |

### factory-mcp tools

| Tool | Effect | What it does |
|---|---|---|
| `factory_status` | read | WorkItem(s) state, history, blockers |
| `factory_intake` | write | Create a WorkItem from an existing GitHub issue (repo, number) |
| `factory_tick` | write | Reconcile one or all WorkItems against GitHub + relay, then take the **one** legal next action. Idempotent. |
| `factory_resolve_human` | write | Record the human's answer and move `human_waiting`/`blocked` → `ready`, `changes_requested` or `reviewing` |
| `factory_propose_issue` | write | Planner only: open an issue labelled `factory:proposed` (a human adds `factory:ready`) |

There is no merge tool. There is no free-form "dispatch" or "open PR" tool: those actions happen
only inside `factory_tick` when the state machine allows them.

### Tenant package (`tenant/factory/`)

Same shape as `openbot/examples/personal/`:

```
tenant/factory/
  agents.yaml      # Foreman + Planner (remote-ag-ui, plan shim / OpenCode Go model)
  model.yaml       # model-agnostic
  brand.yaml
  channels.yaml    # #factory + Telegram escalation
  skills.yaml
  knowledge.yaml
  policy.json      # AGENT_COMPUTER_POLICY value (CEL, enforce, fail-closed)
```

Policy rules (CEL, fail-closed):

- factory Bots: allow `mcp.server == "factory-mcp"`; deny all other MCP, computer and file tools;
- deny any tool named like `merge` from any Bot (belt and braces; factory-mcp has none);
- deny secret-looking tool inputs;
- scheduled runs (`initiator.*` = routine) may call only `factory_tick` and `factory_status`.

Inside `factory-mcp` (enforced in code, tested):

- never write under `.github/` or `AGENTS.md` in a worker branch — reject the Result;
- only allowlisted repos; risk-classed repos route to local workers only;
- worker commands come from the registry, never from issue text (D6).

---

## 5. Non-negotiables

1. **Determinism where it matters.** All transitions and side effects are in `factory-mcp`,
   covered by table-driven tests. The LLM layer does intake/planning and `job_blocked`
   reasoning only. It cannot force a merge or bypass a guard.
2. **Merge authority is pr-gatekeeper's.** No merge tool exists; policy also denies it.
3. **Reconcile, don't trust events.** Validate `repo + prNumber + headSha + round` against
   the WorkItem before any transition.
4. **Async dispatch.** No tool call blocks longer than 30 s. Worker runs are `delegate` →
   store task id → poll on the next tick. Never call relay `wait` from a tick.
5. **No Anthropic dependency** (D11).
6. **Agnostic + swappable providers** (D12); route on locality/risk; degrade on failure.
7. **Dogfood.** Every change goes issue → branch → PR → gatekeeper on this repo. Never edit
   `.github/` or `AGENTS.md` in a feature PR.
8. **Stdlib only.** Python ≥3.9, like agent-relay. `factory-mcp` implements Streamable HTTP
   itself: JSON-RPC over `POST`, `application/json` responses, `202` for notifications, `405`
   for `GET`. The OpenBot SDK client (`@modelcontextprotocol/sdk` 1.30.x) accepts this (verified
   in `client/streamableHttp.js`). `jsonschema` is used only in tests, which skip without it.

---

## 6. WorkItem state machine (owned by factory-mcp)

State names are the enum in `schemas/workitem.schema.json`. Nothing else is a state.

```
issued/planned ─label factory:ready─► ready ─► dispatching ─► implementing ─► pr_open ─► reviewing
                                        ▲                          ▲                        │
                                        │   fix WorkOrder          │                        ├─ MERGE ─► merge_pending ─► merged ─► closed
                                        │   changes_requested ─────┘◄──────── CHANGES ──────┤
                                        │                                                   └─ HUMAN ─► human_waiting
                                        └──────── factory_resolve_human ◄── human_waiting | blocked
   passes ≥ budget.maxPasses anywhere ─► exhausted        issue closed without merge ─► cancelled
```

| From | Fact or action | To |
|---|---|---|
| (none) | `factory_intake` on an open issue | `issued`; `planned` if labelled `factory:proposed`; `ready` if labelled `factory:ready` |
| `issued`, `planned` | issue gains `factory:ready` | `ready` |
| `ready` | tick dispatches an implement WorkOrder (state written before the relay call) | `dispatching` → `implementing` once the relay accepts |
| `implementing` | relay task completed, Result valid, guards pass; controller opens or adopts the PR | `pr_open` (`reviewing` for a fix on an existing PR) |
| `implementing` | task failed / timed out / Result invalid / guard failed | `ready` (or `changes_requested` for a fix) with an `attempts` entry; `blocked` at 2 attempts |
| `pr_open` | `pr-gatekeeper` check-run exists on the head | `reviewing` |
| `reviewing` | verdict for the current head: MERGE / CHANGES / HUMAN | `merge_pending` / `changes_requested` / `human_waiting` |
| `reviewing` | gate check-run completed on the head with no verdict | `human_waiting` |
| `changes_requested` | a blocking finding's fingerprint persists for 2 gate rounds (D4) | `human_waiting` |
| `changes_requested` | tick dispatches a fix WorkOrder (findings as data) | `dispatching` → `implementing` |
| `merge_pending` | PR merged on GitHub | `merged` |
| `merged` | issue closed | `closed` |
| any non-terminal | PR closed unmerged | `human_waiting` |
| any non-terminal | issue closed, PR not merged | `cancelled` |
| any non-terminal | dispatched WorkOrders ≥ `budget.maxPasses` | `exhausted` |
| `human_waiting`, `blocked` | `factory_resolve_human`: `retry` / `regate` / `close` | `ready` or `changes_requested` / `reviewing` / `cancelled` |
| any | unrecoverable fault (repo left the allowlist, issue deleted) | `failed` |

`intent` is a Planner goal that has no issue yet; `factory-mcp` never stores it. Terminal:
`merged` (until the issue closes), `closed`, `cancelled`, `exhausted`, `failed`.

Guards (each one a test): lease per WorkItem; idempotency key per side effect
(`<workitem>:r<round>:<kind>:a<attempt>`, also the relay `requestId`); stale verdict (wrong
`headSha` or older `round`) ignored; per-problem round count (D4); worker `Result` validated
against `schemas/result.schema.json` rules before a PR opens; changed paths read from GitHub,
never from the Result, and protected paths rejected.

---

## 7. Plan

Each phase is one GitHub issue (or a few) in this repo. Offline proofs run in CI; live proofs
run on the owner's machine.

| Phase | Build | Offline proof (CI / cloud) | Live proof (local) |
|---|---|---|---|
| **0′ · Contracts** | Six contract edits, fixtures, ADR-0010/0011/0012 | Schemas validate; positive + negative fixtures pass | — |
| **A · factory-mcp skeleton** | Streamable-HTTP MCP server, `factory_status`, SQLite store, audit log, config (repo allowlist, relay URL, GitHub token env) | MCP client test lists tools with effects; store survives process restart | OpenBot registers `factory-mcp`; a Bot calls `factory_status`; the call is in the OpenBot audit |
| **B · WorkItem ⇄ Job** | `factory_intake`, GitHub reader (issue, labels, PRs, verdict marker, check-run), `factory_tick` reconcile only (no side effects) | Fake-GitHub tests: intake, rebuild-from-GitHub after store loss | A real issue becomes a WorkItem and survives a restart of both OpenBot and factory-mcp |
| **C · Foreman actions** | State machine + guards; dispatch via relay HTTP; Result validation; open PR (controller-authored body in the AGENTS.md format) | Table tests for every transition; duplicate tick = no-op; stale verdict ignored | One issue goes `ready → pr_open` with no human |
| **D · Workers** | Routing by locality/risk; `artifact` and `patch` handling; provider fields | Two fake providers (branch vs. patch) complete the same WorkOrder | Two real harnesses (e.g. `pi-glm`, `codex`) each complete one WorkOrder |
| **E · Gate loop** | Verdict ingestion from marker + check-run; `CHANGES → changes_requested` → fix WorkOrder with findings as data; per-problem rounds; `MERGE → merge_pending → merged → closed` | Fixture verdict comments drive CHANGES→fix→MERGE | One real PR reaches `MERGED` after one auto-fix round |
| **F · HITL + ops** | `human_waiting`/`blocked` → OpenBot `job_blocked` → Telegram; `factory_resolve_human`; cost ledger; autonomy-rate metric; tenant package + policy + install script | Policy file parses; metric computed from fixture history | A blocked job is answered from Telegram and resumes |

**Project done test (unchanged):** on a real repo, ≥1 issue completes `ready → merged` with
zero human intervention, including one automatic CHANGES→fix round, and the autonomy rate is
logged. First target: the agent-relay `default_config_path()` issue.

**Autonomy rate** = WorkItems that reached `merged` with **no** `human_waiting` or `blocked` state and no human
commit on the branch ÷ all WorkItems that reached a terminal state. Merges by pr-gatekeeper do
not count as human touches.

---

## 8. Risks and mitigations

| Risk | Mitigation |
|---|---|
| OpenBot is **alpha** | OpenBot holds only the goal; decisions and state are in factory-mcp, so a platform failure stalls a job, never causes a wrong merge |
| OpenBot fork drifts from upstream | Keep OpenBot unmodified; the private fork `Ami-Khokhar/openbot` carries Jobs, Telegram and the plan shim |
| Heavy footprint (Docker/Postgres/Intelligence) | Accept for the control plane; factory-mcp and workers stay lightweight |
| 15-min pass and routine floor | Async dispatch; local poller for latency |
| LLM Foreman drift / prompt injection | No free-form action tools; policy allows only factory-mcp; issue text is data |
| Claude subscription ends 2026-09-28 | D11; phases filed as issues any harness can run |
| Timeout → duplicate workers | Lease + idempotency key; reconcile relay tasks and branches before retry |
| Stale verdict acted on | Guard on `repo+pr+headSha+round` |
| factory-mcp store lost | Rebuild from GitHub (issue labels, branch name `factory/<issue>`, PR, verdict marker) |
| Missing branch protection (D7) | Owner enables required `pr-gatekeeper` check on target repos before Phase E live proof |

---

## 9. Open questions

1. Planner: propose issues for a human to mark `factory:ready`, or open ready issues directly?
   **Default:** propose (`factory:proposed`); revisit after the done test.
2. Risk classes that force HUMAN on day one. **Default:** secrets, payments, security, data
   deletion, `.github/`, `AGENTS.md`.
3. GitHub access from factory-mcp: REST with a fine-grained token from env, or `gh`.
   **Default:** REST via `urllib`, token from `FACTORY_GITHUB_TOKEN`; `gh` only in scripts.
4. ~~Push the local OpenBot clone to a private fork?~~ Done: `github.com/Ami-Khokhar/openbot`.

---

## 10. Immediate next steps

0. **Owner:** add repo secrets `OPENCODE_GO_API_KEY` and `GATEKEEPER_READ_TOKEN` to this repo,
   and enable branch protection on `main` requiring the `pr-gatekeeper` check.
1. File the phase issues (0′, A–F) in this repo.
2. Phase 0′: contract edits + ADR-0010/0011/0012 → PR → gatekeeper.
3. Phase A–C offline in order, one PR per issue, each ≤600 changed lines (gate limit).
4. On the owner's machine: live proofs A–C, then D–F.
5. Run the done test on the agent-relay `default_config_path()` issue.

---

## 11. Artifact index

| What | Where |
|---|---|
| Contracts | `schemas/*.schema.json` |
| Contracts overview | `docs/contracts.md` |
| ADRs | `docs/decisions.md` |
| Architecture | `docs/architecture.html` |
| Rev 1 of this plan | git history of `docs/HANDOFF.md` |
| Agent registry | `~/.config/agent-relay/agents.json` |
| Relay | `github.com/Ami-Khokhar/agent-relay` — `src/server.py` (HTTP), `src/mcp_server.py` (stdio) |
| Gate | `github.com/Ami-Khokhar/pr-gatekeeper` (private), caller `pr-gatekeeper-workflow@v1` |
| OpenBot | private fork `github.com/Ami-Khokhar/openbot` (upstream `CopilotKit/openbot`); local clone `~/Documents/Coding projects/openbot` |
| OpenBot docs | `openbot/docs/{architecture,coworkers,routines,configuration}.md`; Jobs has no doc on the fork's `main`, read `server/src/jobs/` |
| OpenBot policy | `openbot/server/src/computer/policy.ts` (CEL context), `server/src/plugins/store.ts` (MCP call context) |
| OpenBot MCP client | `openbot/server/src/plugins/mcp.ts` (Streamable HTTP) |
| Local launcher | `~/.local/bin/openbot` (ChatGPT-plan shim on port 4300) |

**Useful commands**

```bash
# tests (offline proofs)
python3 -m unittest discover -s tests -p 'test_*.py'

# relay health + agents (local only)
curl -sS http://127.0.0.1:43124/healthz
curl -sS http://127.0.0.1:43124/v1/agents
```

---

## 12. Glossary

- **WorkItem** — one deliverable issue's durable orchestration record (stored in factory-mcp).
- **WorkOrder** — the instruction handed to a worker for one execution.
- **Result** — a worker's artifact/patch + evidence handoff.
- **Verdict** — a normalized, forge-neutral gate decision (MERGE/CHANGES/HUMAN).
- **Foreman** — `factory-mcp` plus the OpenBot Bot that calls it.
- **Tick** — one idempotent reconcile-then-act pass of `factory_tick`.
- **Job** — OpenBot's goal-until-finished primitive; wraps one WorkItem.
- **Pass** — one OpenBot job iteration (≤15 min), ended by done/blocked/exhausted.
- **Blocked** — HUMAN escalation with evidence (≥2 attempts).
- **Exhausted** — budget ceiling reached; distinct from done and blocked.
- **Offline / live proof** — test with fakes (CI, cloud) / run on the real local stack.
- **Autonomy rate** — defined in §7.

---

## 13. Progress (keep current)

Offline work runs in cloud sessions; live proofs are issue #8 (owner machine). Branches are
stacked, each based on the previous one; nothing is merged yet. The gate runs fail until the
owner adds `OPENCODE_GO_API_KEY` and `GATEKEEPER_READ_TOKEN` (§10 step 0).

| Phase | Issue | Branch | State |
|---|---|---|---|
| 0′ Contracts | #1 | `phase-0-contracts` | PR open |
| A Skeleton | #2 | `phase-a-skeleton` | PR open |
| B Intake + reconcile | #3 | `phase-b-reconcile` | not started |
| C Foreman actions | #4 | `phase-c-actions` | not started |
| D Workers | #5 | `phase-d-workers` | not started |
| E Gate loop | #6 | `phase-e-gate-loop` | not started |
| F HITL + ops | #7 | `phase-f-hitl` | not started |
