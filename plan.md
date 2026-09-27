# Plan — Day Desk as the single front door to the software factory

Status: **proposal / implementation plan.** Written 2026-09-25.
Owner: (you). Pick-up target: a cloud coding session, reading this file from `main`.

This document is self-contained: it records the current verified state, the target design, the
exact gap list, per-file changes in each repo, tool specifications, tests, rollout phases, and a
task list an implementing agent can follow in order.

---

## 0. TL;DR

We want the person to talk to **one Bot — Day Desk — in `#factory` / `/bot` / Telegram**, and have
Day Desk:

1. sanity-check and enable a repo,
2. turn a goal into issues,
3. get the work started (one confirmation),
4. report progress and surface escalations.

Everything a model does stays *orchestration*. Every state transition and side effect stays in
**deterministic code in `factory-mcp`** (ADR-0010). Merges stay with **pr-gatekeeper** (ADR-0004).
Repo creation and workflow installation stay **human** (blast radius; ADR-0007).

The main work: add four deterministic `factory-mcp` tools, make repo definition declarative
(`repos.yaml`) with a reusable sanity check, and widen the OpenBot policy/grants so Day Desk can
call them.

---

## 1. Current state (verified on the owner machine, 2026-09-25)

| Piece | State | Evidence |
|---|---|---|
| `factory-mcp` running over TLS | ✅ | `127.0.0.1:43125`, mkcert cert for `factory.lan.example` |
| `agent-relay` running | ✅ | `127.0.0.1:43124`, 8 agents incl. `pi-glm-factory` |
| OpenBot registered `factory-mcp` MCP server | ✅ | id `factory-mcp`, 5 tools, `lastError: null` |
| Grants | ✅ | Foreman: all 5 `factory_*` + `routines/create_routine`; Planner: `factory_propose_issue` + `factory_status` |
| OpenBot policy | ✅ default-deny; allows `factory-mcp` only to `factory-foreman`/`factory-planner` (+ `routines` to Foreman) |
| Foreman model | ✅ | `gpt-5.6-terra` answers via local plan-shim `:4300` |
| GitHub credential for factory-mcp | ✅ fine-grained PAT, repo access limited to `Ami-Khokhar/agent-relay` (public) |
| Day Desk Bot | ✅ exists, but **read-only** GitHub tools + handoff grants to `scout`/`workshop`; **no** factory tools |
| Telegram front door | ✅ `TELEGRAM_AGENT_ID=day-desk` already routes to Day Desk |
| Repo definition | ❌ env vars (`FACTORY_REPOS`, `FACTORY_LOCAL_ONLY_REPOS`, `FACTORY_WORKERS`, `FACTORY_TRUSTED_AUTHORS`, `FACTORY_CHECKOUT_ROOT`) |
| Repo sanity / enable | ❌ operator CLI `scripts/factory add-repo` only |
| Start gate | ❌ manual GitHub label `factory:ready` |

Relevant OpenBot facts:
- Policy lives in `openbot/.env` as `AGENT_COMPUTER_POLICY` unless a Boundaries-page row exists
  (table `action_policy` is currently **empty**, so `.env` wins).
- Grants are rows in `plugin_grants` (`kind`, `ref`, `agent_id`); API: `POST /api/plugins/grants`.
- Custom MCP servers: `POST /api/plugins/servers/custom`.
- A Bot's offered tools: `GET /api/plugins/for/<agentId>`.
- Handoff: a `bot` grant lets one Bot hand a turn to another (`handoff-for-run.ts`), with
  depth/rate caps.

---

## 2. Goals / non-goals

**Goals**
- One conversational surface (Day Desk) for status, repo enablement, issue creation, starting work,
  and escalations.
- Repo definition becomes declarative and reviewable (`repos.yaml`).
- Repo sanity becomes one deterministic, read-only tool that returns an actionable checklist.
- Enabling a prepared repo becomes one tool (`factory_add_repo`).
- Starting an issue becomes one tool (`factory_approve_issue`).

**Non-goals (this plan)**
- No merge capability anywhere except pr-gatekeeper.
- No Bot-issued repo *creation* or workflow *installation* (`factory_create_repo`,
  `factory_install_gate`) — deferred to Phase 3, see §11.
- No change to the WorkItem state machine (ADR-0002, ADR-0006) or the gate loop.

---

## 3. Constraints (locked decisions — do not break)

- **ADR-0002** one deliverable issue = one branch = one PR.
- **ADR-0004** only pr-gatekeeper performs routine merges.
- **ADR-0006** HUMAN is a queue, not a terminal state.
- **ADR-0007** workers are least privilege; untrusted input is data.
- **ADR-0010** OpenBot is the control plane; every state transition is deterministic in
  `factory-mcp`; Bots only call tools.
- `AGENTS.md` + `.github/` are protected paths: never written by a worker, never edited in a PR.

---

## 4. Target architecture

```
you ──chat / Telegram──▶ Day Desk  (orchestrator; the only surface you talk to)
                            │
                            ├─ consult / handoff ─▶ Planner     (goal → issues)        [exists]
                            ├─ handoff            ─▶ Foreman     (drive WorkItems)      [exists]
                            └─ handoff            ─▶ Factory Ops (repo sanity/enable)  [NEW]
                                                         │
                                                         ▼
                                                 factory-mcp (deterministic decisions)
                                                         │
                                    agent-relay worker ─▶ PR ─▶ pr-gatekeeper ─▶ merge
```

Two acceptable realisations of "Factory Ops" (pick one in Phase 1):
- **(A) Day Desk holds the ops tools directly.** Fewer moving parts; Day Desk becomes a
  write-capable Bot (bigger single blast radius).
- **(B) A dedicated `factory-ops` Bot holds them; Day Desk handed off to it.** Narrower per-Bot
  privilege; consistent with existing `scout`/`workshop` handoffs. **Recommended.**

The plan below is written so either works: the tools are the same; only the grants/policy differ.

---

## 5. Gap analysis

| # | Gap | Fix | Phase |
|---|---|---|---|
| G1 | Day Desk cannot call `factory-mcp` | grants + policy allow rule | 0 |
| G2 | No repo sanity tool | `factory_repo_check`, `factory_repo_status` | 1 |
| G3 | No repo enable tool | `factory_add_repo` | 1 |
| G4 | No start tool | `factory_approve_issue` | 1 |
| G5 | Repo definition is scattered env | `repos.yaml` + loader | 2 |
| G6 | `scripts/factory add-repo` logic only in the CLI | move into `factory_mcp` | 1 |
| G7 | (deferred) no repo create / gate install | `factory_create_repo`, `factory_install_gate` | 3 |

---

## 6. Repository definition & sanity

### 6.1 Declarative manifest

New file, `~/.config/factory-mcp/repos.yaml` (path overridable with `FACTORY_REPOS_FILE`):

```yaml
version: 1
defaults:
  workers: [pi-glm-factory:local]     # preferred first; "agentId:local|hosted"
  protected_paths: [".github/", "AGENTS.md"]
  trusted_authors: [Ami-Khokhar]
repos:
  - name: Ami-Khokhar/agent-relay
    enabled: true
    risk: normal                      # normal | local-only
    default_branch: main
    workers: [pi-glm-factory:local]   # optional per-repo override
    trusted_authors: [Ami-Khokhar]    # optional per-repo override
    protected_paths: [".github/", "AGENTS.md"]
    gate:
      config_path: .github/pr-gatekeeper.json
      workflow_path: .github/workflows/pr-gatekeeper.yml
      verify: "python3 -m unittest discover -s test -p 'test_*.py'"   # copied from the file at enable time
    added_by: Ami-Khokhar
    added_at: "2026-09-25"
```

Precedence when both env and manifest exist (migration safety):
`per-repo manifest > defaults > env var`. Until Phase 2 lands, absence of the file means "behave
exactly as today (parse env)".

### 6.2 Loader — new module `factory_mcp/repos.py`

```python
@dataclass(frozen=True)
class Repo:      # one enabled repo
    name: str
    risk: str                     # "normal" | "local-only"
    default_branch: str
    workers: tuple                # ((agentId, "local"|"hosted"), ...)
    trusted_authors: frozenset
    protected_paths: tuple
    verify: str
    enabled: bool

@dataclass(frozen=True)
class Repos:
    repos: dict[str, Repo]        # by name
    def names(self) -> frozenset; def get(self, name) -> Repo | None
    def local_only(self) -> frozenset; def workers_for(self, name) -> tuple

def load(path: str | None) -> Repos
def save(path, repos: Repos) -> None         # write manifest atomically (tmp + rename)
def upsert(path, repo: Repo) -> None
```

`Config.from_env` (§8.1) reads `FACTORY_REPOS_FILE` (default `~/.config/factory-mcp/repos.yaml`).
If the file exists, `Config.repos`, `Config.local_only_repos`, `Config.trusted_authors`,
`Config.workers`, `Config.protected_paths` are **derived** from it (env still honoured as an
override for one release).

### 6.3 Sanity checks — `factory_repo_check(repo)`

Read-only. Each check returns `{id, ok, detail, fix}`. Verdict is `ok` iff every `required:true`
check passes.

| id | required | What it checks | GitHub/API |
|---|---|---|---|
| `repo_exists` | yes | repo readable; capture `default_branch`, `private`, `archived` | `GET /repos/{repo}` |
| `gate_config` | yes | `.github/pr-gatekeeper.json` exists, base64-decodes, JSON-parses, has non-empty `verify` | `GET /repos/{repo}/contents/.github/pr-gatekeeper.json` |
| `gate_workflow` | no | `.github/workflows/pr-gatekeeper.yml` present | `GET .../contents/...` |
| `verify_tool` | yes | first tool of the `verify` command exists in `factory-worker:latest` | `docker run --rm factory-worker:latest sh -c "command -v <tool>"` (reuse `scripts/factory` logic: `verify.split("&&")[-1].split()[0]`) |
| `labels` | no | `factory:ready` and `factory:proposed` exist (creatable if not) | `GET /repos/{repo}/labels` |
| `token_checks` | if `private` | check-runs readable (needs Checks: Read on a private repo) | `GET /repos/{repo}/commits/{branch}/check-runs` → expect 200 |
| `branch_protection` | no | default branch protected | `GET /repos/{repo}/branches/{branch}/protection` (tolerate 404) |
| `checkout_root` | yes | `FACTORY_CHECKOUT_ROOT` set | local config |
| `trusted_authors` | yes | at least one trusted author | config/manifest |
| `not_archived` | yes | repo is not archived | from `repo_exists` |

`fix` is a human-readable sentence, e.g. "add `factory:ready` label" or "grant the PAT Checks: Read
for this private repo".

### 6.4 The trust boundary (what stays human)

| Act | Who | Rationale |
|---|---|---|
| Decide to trust/enable a repo | **human** (confirms) | trust boundary |
| Create the GitHub repo | **human** | Phase 3 only, deliberately deferred |
| Install pr-gatekeeper workflow | **human** | a pushed workflow runs on `push` with repo secrets |
| Add the repo to the fine-grained PAT | **human** | fine-grained PATs are per-repo |
| Run sanity checks | **Day Desk / Ops** | mechanical |
| Create labels, clone worker checkout, write manifest | **Day Desk / Ops** | mechanical |
| File issues | **Day Desk → Planner** | mechanical |
| Approve start (`factory:ready`) | **human**, one confirmation | ADR-0010 "what starts" |
| Drive / review / merge | factory-mcp / pr-gatekeeper | deterministic |

---

## 7. New `factory-mcp` tool specifications

All tools are registered in `factory_mcp/tools.py` (`build(store, foreman, ops)`), use the existing
`Tool(name, description, input_schema, effect, fn)` shape, and raise `ToolError` for refusals
(returned to the model as an `isError` result).

### 7.1 `factory_repo_check` — effect `read`

- **Input:** `{ "repo": "owner/repo" }` (`additionalProperties: false`).
- **Output:** `{ "repo", "ok": bool, "defaultBranch", "private": bool, "risk", "checks": [ {id, required, ok, detail, fix} ] }`
- **Behaviour:** runs §6.3. Never mutates anything. Safe to call repeatedly.
- **Errors:** `ToolError("owner/repo is not a github repo reference")` for malformed input only.

### 7.2 `factory_repo_status` — effect `read`

- **Input:** `{ "repo"?: "owner/repo", "live"?: bool (default false) }`.
- **Output:** `{ "repos": [ {name, enabled, risk, workers, trustedAuthors, workItems: {total, byState}, lastError} ] }`
  When `live` is true and a `repo` is given, include the §6.3 checks inline.
- **Behaviour:** lists configured repos and WorkItem counts from the store. `live` is explicit
  because it hits GitHub.

### 7.3 `factory_add_repo` — effect `write`

- **Input:**
  ```json
  { "repo": "owner/repo", "risk": "normal|local-only" (optional, default normal),
    "trusted_authors": ["login", ...] (optional), "workers": ["agent:local", ...] (optional),
    "confirm": true }
  ```
- **Behaviour:**
  1. run `factory_repo_check(repo)`; if not ok, **refuse** with the failing checklist
     (`ToolError`) — nothing is changed;
  2. clone the worker checkout to `FACTORY_CHECKOUT_ROOT/<owner>/<repo>` if missing;
  3. create the `factory:ready` / `factory:proposed` labels if missing;
  4. write/merge the repo entry into `repos.yaml` (read the gate `verify` from the file);
  5. reload in-process config; log `repo:added`.
- **Idempotent:** re-running updates the manifest entry and skips the clone/labels if present.
- **Errors:** checklist refusals; `ToolError("FACTORY_CHECKOUT_ROOT is not set")`.
- **Note:** `confirm:true` is required so the model cannot enable a repo as a side effect of a
  different request. (Policy cannot express "the person confirmed"; this is a speed bump plus the
  requirement that the person asked.)

### 7.4 `factory_approve_issue` — effect `write`

- **Input:** `{ "repo": "owner/repo", "issue": <int> }`.
- **Behaviour:** verify repo is configured; fetch the issue; verify author is trusted; verify the
  body parses as `## Acceptance` (`workitem.parse_acceptance`); add the `factory:ready` label.
- **Idempotent:** if already labelled, return the current state without error.
- **Errors:** untrusted author, missing acceptance, unknown repo, unknown issue.
- **Not exposed to the Planner** (it only *proposes*).

### 7.5 Optional convenience: `factory_repos` — effect `read`

- **Output:** `{ "repos": ["owner/repo", ...] }`. Handy for the model before it calls the others.

---

## 8. Code changes — file by file

### 8.1 `software-factory/factory_mcp/config.py`

- Add fields: `repos_file: str`, and keep existing fields but allow them to be derived.
- In `from_env`: after building the env-based defaults, if `repos_file` exists, call
  `repos.load()` and override `repos`, `local_only_repos`, `trusted_authors`, `workers`,
  `protected_paths`.
- Keep `checkout(repo)` and `trusts(repo, author)`. Add `trusts` to consult the per-repo manifest
  first, then the global set, then the repo owner.
- `protected_paths` becomes per-repo aware (add `protected_for(repo)`).

### 8.2 `software-factory/factory_mcp/github.py` (new methods)

```python
def contents(self, repo, path):        # GET /repos/{repo}/contents/{path}; None on 404
def contents_text(self, repo, path):   # decode base64 → str; None on 404
def labels(self, repo):                # GET /repos/{repo}/labels
def create_label(self, repo, name, color="0E8A16", description=""):   # POST; tolerate 422 "already exists"
def add_labels(self, repo, number, labels):   # POST /repos/{repo}/issues/{number}/labels
def default_branch(self, repo):        # repository(repo)["default_branch"]
def branch_protection(self, repo, branch):    # GET .../protection; None on 404
```

`_request` already raises `GitHubError(status, message)`; callers translate to `ToolError`.

### 8.3 `software-factory/factory_mcp/repos.py` (new)

Implements §6.2. Pure functions + dataclasses; unit-testable without network. Writing the manifest
is atomic (`NamedTemporaryFile` in the same dir + `os.replace`). No third-party YAML dependency is
assumed — **use `json`** if `yaml` is unavailable (the repo's `requirements.txt` is stdlib-only by
design); if YAML is required, add `pyyaml` to `requirements.txt` and the worker image. Decision:
**store the manifest as `repos.yaml` but parse with a minimal YAML subset is fragile — prefer
`repos.json` if `pyyaml` is not already a dependency.** Confirm in Phase 1 and pick one.

### 8.4 `software-factory/factory_mcp/ops.py` (new)

```python
class Ops:
    def __init__(self, store, github, config, relay=None): ...
    def repo_check(self, repo) -> dict            # §6.3
    def repo_status(self, repo=None, live=False) -> dict
    def add_repo(self, repo, risk="normal", trusted_authors=None, workers=None) -> dict
    def approve_issue(self, repo, number) -> dict
```

Keep GitHub/docker calls here; keep `Foreman` focused on the state machine.

### 8.5 `software-factory/factory_mcp/foreman.py`

- No state-machine changes required.
- `propose_issue` already enforces the allowlist; leave as is.
- Optionally have `Foreman` delegate `repos`/`trusts` to the manifest-backed `Config`.

### 8.6 `software-factory/factory_mcp/tools.py`

- `build(store, foreman, ops)` (default `ops=None` for backward-compatible tests).
- Append the five tools from §7.
- Descriptions must state the effect and the refusal conditions (the model reads these).

### 8.7 `software-factory/factory_mcp/server.py`

- `make_server(...)`: construct `ops = Ops(store, github, config, relay)` and
  `server.tools = tools.build(store, foreman, ops)`.

### 8.8 Tests (`software-factory/tests/`)

- `tests/fakes.py`: extend the fake GitHub with `contents_text`, `labels`, `create_label`,
  `add_labels`, `default_branch`, `branch_protection`.
- `tests/test_repos.py` (new): manifest load/save/upsert; env fallback; precedence.
- `tests/test_repo_check.py` (new): one test per check in §6.3, including the private-repo Checks
  case and the `verify_tool` docker stub.
- `tests/test_ops_tools.py` (new): `factory_repo_check` refusal path, `factory_add_repo`
  idempotency and refusal-on-failing-check, `factory_approve_issue` idempotency and untrusted
  author, `factory_repo_status` shapes.
- `tests/test_mcp_server.py`: add the new tools to the `tools/list` assertion.
- Run: `python3 -m unittest discover -s tests -p 'test_*.py'`.

### 8.9 `software-factory/scripts/factory`

- `cmd_add_repo` should shell out to the new tool (`factory call factory_add_repo '{...}'`) or be
  kept as a thin wrapper that prints the same checklist. Avoid two implementations of the sanity
  logic (G6).

### 8.10 `software-factory/tenant/factory/skills.yaml`

- Add a `factory-ops` skill for Day Desk, naming the tools and the required order:
  1. `factory_repo_check` → show the checklist, refuse to proceed if not ok;
  2. `factory_add_repo` only after the person says "enable it";
  3. `factory_approve_issue` only after the person says "start it".
- Tools declared: `factory-mcp/factory_repo_check`, `factory-mcp/factory_repo_status`,
  `factory-mcp/factory_add_repo`, `factory-mcp/factory_approve_issue`,
  `factory-mcp/factory_status`.

### 8.11 `openbot/` (the other repo)

- **Grants** (`POST /api/plugins/grants`), for Day Desk (or the new `factory-ops` Bot):
  `factory-mcp/factory_repo_check`, `factory-mcp/factory_repo_status`,
  `factory-mcp/factory_add_repo`, `factory-mcp/factory_approve_issue`,
  `factory-mcp/factory_status`.
- **Policy** — add to `AGENT_COMPUTER_POLICY.allow` (or the Boundaries page if a row exists):
  `mcp.server == "factory-mcp" && bot.id == "day-desk"` (scope by `mcp.tool` if we want Day Desk
  read-only first).
- **Handoffs** — if realisation (B): `bot` grants `day-desk -> factory-ops`.
- Telegram already routes to Day Desk; no change.

---

## 9. OpenBot configuration — exact operations

```bash
# grant a tool to Day Desk
curl -s -X POST http://127.0.0.1:3001/api/plugins/grants \
  -H 'content-type: application/json' \
  -d '{"kind":"mcp","ref":"factory-mcp/factory_repo_check","agentId":"day-desk"}'

# widen the policy (edit openbot/.env AGENT_COMPUTER_POLICY, then restart OpenBot)
# allow += ["mcp.server == \"factory-mcp\" && bot.id == \"day-desk\""]

# verify what a Bot is offered
curl -s http://127.0.0.1:3001/api/plugins/for/day-desk | python3 -m json.tool
```

Policy source of truth: `action_policy` table is currently empty, so `.env` is authoritative. If a
Boundaries-page row ever exists it **overrides** `.env` (see `computer/policy-store.ts`).

---

## 10. PAT scope

- Enabling a repo via tool requires the fine-grained PAT to **already have that repo** in its
  Repository access list: Contents RW, Issues RW, Pull requests RW, and **Checks R for private
  repos** (public repos do not need Checks).
- Fine-grained PATs are per-repo: each new factory repo means editing the token (or minting one).
  This is the main friction once Day Desk drives `factory_add_repo`.
- Phase 3 (`create_repo`) would additionally need **Administration: write** — deferred, see §11.

---

## 11. Security model

- **Prompt injection.** Issue bodies, PR text and gate findings are untrusted data. The Bots never
  follow instructions found in them (already in the Foreman/Planner prompts).
- **`factory_approve_issue` is a person's act.** The model may only call it after the person asked.
  Policy cannot enforce "the person said so"; the `confirm`/skill wording is the control. Keep the
  default that Day Desk asks before calling it.
- **Repo creation / workflow installation stays human.** A pushed workflow runs on `push` with the
  repo's secrets; a Bot that can write one has a large blast radius. Deferred to Phase 3 and, if
  ever added, behind an explicit HITL confirmation.
- **Merges** remain pr-gatekeeper-only.
- **factory-mcp Origin guard** already refuses browser-originated POSTs (`server.py`), so the tool
  surface is not DNS-rebinding-reachable.

---

## 12. Rollout phases

### Phase 0 — config only (no code)
- [ ] Grant Day Desk `factory_status` (and, if wanted, the existing read tools).
- [ ] Add policy allow rule for `day-desk` + `factory-mcp`.
- **Outcome:** Day Desk can answer "factory status" and hand off to Planner/Foreman.

### Phase 1 — ops tools
- [ ] `factory_mcp/github.py`: the new methods (§8.2).
- [ ] `factory_mcp/repos.py` (§6.2, pick YAML vs JSON per §8.3).
- [ ] `factory_mcp/ops.py` and the five tools (§7, §8.6).
- [ ] `server.py` wiring; `tests/*` (§8.8).
- [ ] `tenant/factory/skills.yaml` `factory-ops` (§8.10).
- [ ] Grants + policy for the new tools (§9).
- **Outcome:** Day Desk can check, enable a prepared repo, and start work with one confirmation.

### Phase 2 — manifests
- [ ] Migrate `agent-relay` into `repos.yaml`.
- [ ] Derive `Config` fields from it; keep env as override for one release.
- [ ] `scripts/factory add-repo` delegates to the tool.
- **Outcome:** repo definition is declarative and reviewable.

### Phase 3 — optional autonomy
- [ ] `factory_create_repo`, `factory_install_gate`, and (optional) auto-approve.
- Gated by: PAT with Administration: write; explicit HITL confirmation; a written decision ADR
  update (this contradicts ADR-0007 as-is).
- **Default: off.**

---

## 13. Acceptance criteria

1. "add repo `owner/x`" → Day Desk returns the §6.3 checklist. If everything required passes and
   the person confirms, the repo appears in `factory_repo_status` and `factory status`; otherwise
   Day Desk names exactly what is missing.
2. "build me `<feature>` in `owner/x`" → Day Desk (via Planner) returns issues with `## Acceptance`
   checks; after one confirmation it calls `factory_approve_issue` and the factory starts.
3. "what needs me?" → Day Desk lists `human_waiting`/`blocked` WorkItems with the escalation text.
4. `factory_repo_check` never mutates anything (assert no GitHub writes).
5. Every side effect writes an audit row; no tool exists that merges.
6. `python3 -m unittest discover -s tests -p 'test_*.py'` is green.

---

## 14. Open questions

1. **Holder of the ops tools:** Day Desk directly (A) or a dedicated `factory-ops` Bot reached by
   handoff (B)? Recommendation: (B).
2. **Manifest format:** `repos.yaml` (needs PyYAML) vs `repos.json` (stdlib). Recommendation:
   match the repo's stdlib-only posture → `repos.json` unless PyYAML is already shipped.
3. **Auto-approve:** should `factory_approve_issue` ever be called without a person? Default: no.
4. **Per-repo workers:** is the global `FACTORY_WORKERS` enough, or does each repo need its own?
   The schema allows both; confirm the default.
5. **Day Desk scope:** read-only first (status/check) and write later, or all at once?

---

## 15. Task list for the implementing session (in order)

1. Read `factory_mcp/config.py`, `tools.py`, `foreman.py`, `github.py`, `server.py`, `workitem.py`,
   `scripts/factory`, and `tests/` to match style.
2. Decide §14.1 and §14.2; write the decision into this file.
3. Implement `github.py` methods (§8.2) + `tests/fakes.py`.
4. Implement `repos.py` (§6.2) + `tests/test_repos.py`.
5. Implement `Ops` and `repo_check` (§6.3) + `tests/test_repo_check.py`.
6. Implement `add_repo` and `approve_issue` (§7.3, §7.4) + `tests/test_ops_tools.py`.
7. Register tools in `tools.py`; wire `server.py`; update `tests/test_mcp_server.py`.
8. Add the `factory-ops` skill (§8.10).
9. Run the full test suite.
10. Deploy: `factory update` (or restart the service) and run the OpenBot-side grants/policy (§9).
11. Verify with §13 acceptance criteria and record the evidence here.
12. Open a PR (see `AGENTS.md`) OR, if pushing to `main` directly, keep the change list in the
    commit body identical to the files touched.

---

## Appendix A — verified commands used to establish §1

```bash
$ lsof -nP -iTCP:43125 -sTCP:LISTEN            # factory-mcp up
$ curl -s http://127.0.0.1:3001/api/plugins | python3 -m json.tool   # factory-mcp: 5 tools, lastError null
$ curl -s http://127.0.0.1:3001/api/plugins/for/factory-foreman      # offered tools
$ curl -s -X POST http://127.0.0.1:3001/api/plugins/call \
    -H 'content-type: application/json' \
    -d '{"ref":"factory-mcp/factory_status","args":{},"agentId":"factory-foreman"}'   # 200, real WorkItems
$ python3 -m unittest discover -s tests -p 'test_*.py'               # baseline green
```
