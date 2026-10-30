# Running the factory

The factory turns a GitHub issue into a merged pull request. You write what you want and how to
check that it is done. A sandboxed AI worker writes the code, factory-mcp checks it and opens the
PR, and pr-gatekeeper reviews and merges it. The factory asks you only when it is stuck.

Example: [agent-relay#22](https://github.com/Ami-Khokhar/agent-relay/issues/22) asked for the
registry path in `/healthz`. It was opened with `factory new`, and nothing else was typed.

## Everyday use

1. **Write an issue** in a factory repo, and label it `factory:ready`. Either use the GitHub web or
   mobile app, or run:

   ```bash
   factory new Ami-Khokhar/agent-relay "Report the registry path in /healthz" \
     "GET /healthz includes the registry path | a test in test/test_relay.py asserts it" \
     "Nothing else changes | python3 -m unittest discover -s test -p 'test_*.py' passes" \
     -d "Why you want it, in a sentence or two."
   ```

   The body must have an `## Acceptance` heading with bullets of the form
   `- <claim> — done when: <check>`. The factory refuses an issue without one.
2. **Wait.** Within a minute the factory takes the issue. A worker pass takes about 5 minutes,
   and the gate takes about 5 more. GitHub notifies you when the PR opens, when the gate comments,
   and when it merges.
3. **Answer when asked.** When the factory needs a person, it comments on the issue with
   "software-factory needs a person" and mentions you, and Day Desk messages you in Telegram
   (below). Read the reason, then decide, in a reply to Day Desk or here:

   ```bash
   factory retry  Ami-Khokhar/agent-relay#22 "what to do differently"   # try again
   factory regate Ami-Khokhar/agent-relay#22 "pushed a fix myself"       # re-review your push
   factory close  Ami-Khokhar/agent-relay#22 "not worth it"             # give up
   ```

4. **Done** is a PR merged by pr-gatekeeper and the issue closed. factory-mcp closes the issue,
   because GitHub does not act on `Closes #n` when the gate's workflow token merges.

`factory call factory_runtime_status '{"id":"Ami-Khokhar/software-factory#302"}'` reads a
sanitized runtime snapshot: deployed revision, worker order and registered model/image pins,
credential-file readability (not authentication), relay health/cap, and effective per-item versus
global timeout. With an item id it probes the live PR head and GitHub comments/checks/workflows
through the factory's own credential, recording permission results without raw error bodies.
It starts no provider request, worker, retry, allowance or host change. Day Desk's driving token
exposes this read tool. A cache mismatch is an observation of the profile's disk cache, not proof
of what an active chat sees. `tools/list` advertises `ttlMs: 0` and `cacheScope: private`; use the
supported `/reload-mcp` command in an older chat to reconnect and rebuild its tool schemas.

`factory status` lists every WorkItem. `factory status <owner/repo#n>` shows one WorkItem, its
last steps, and what it needs from you.

### Writing issues the factory can finish

- One issue is one PR: about 700 changed SOURCE lines (advisory, ADR-0079: over the target warns,
  over the 1000-line reviewable ceiling becomes a decomposition proposal; it never rejects on source
  count alone) and at most 20 files. Test and doc lines are reported separately and never count
  against the source target. Split larger work into issues that
  each make sense alone, and aim for about half the size: a worker finishes a small issue in one
  pass, and an issue near the limit can run out of time.
- Make each check readable from the result: a test that must pass, a command that must print
  something, a file that must contain something. "Works well" is not a check.
- Say what must not change. The worker reads the issue literally: its title, its text and its
  acceptance list, as they were when the factory took it in. An edit after that does not reach
  the worker; close the issue and open a new one instead.
- To choose the worker, add a `Worker: <relay agent id>` line (or `factory new ... -w <id>`).
  Several lines give an order, and a failed attempt moves only to the next one named. The agent
  must be in `FACTORY_WORKERS`; a name outside it is dropped and an allowed worker runs the issue
  (audit row `unknown_worker_rerouted`, ADR-0110). It waits for you only when no allowed worker can
  run it. Without the line, the factory uses `FACTORY_WORKERS` in order (ADR-0029).
- Only you (`FACTORY_TRUSTED_AUTHORS`) can open issues the factory acts on. Issues from other
  people are skipped, even with the label.

### Rollout: source-line sizing (ADR-0079)

The size metric counts changed source lines: 700 is the target, 1000 the reviewable ceiling, and
`tests/`/`test_*` and markdown/docs lines are reported separately and never count against the
target. A source total over the ceiling becomes a proposed decomposition, not a failed attempt and
not a fabricated success.

Deploy the pieces together, and do not call the source merge a live rollout until all of them are
verified on the host:

- `worker/runner.py` in the worker image: the image must be rebuilt (`docker build -t
  factory-worker:latest worker/`) and its `collect` re-read before any pass can see the ceiling.
- `factory_mcp/sizing.py` plus `factory_mcp/patches.py` and `factory_mcp/foreman.py` in the running
  factory-mcp service (restart the service, not only merge the branch).
- `templates/AGENTS.md`: the standard block is the only place the shared guidance is edited, and
  this branch does not edit it. The owner ships that revision (700 source lines, 1000 ceiling, tests
  and docs separate) through the normal template-copy route, then copies it to this repo's protected
  root `AGENTS.md`; both are protected-path changes, so until that separate change lands the shipped
  standard still says 600 and the code here is ahead of the guidance.
- The gate's own policy: `.github/pr-gatekeeper.json` still carries `max_changed_lines: 600` on
  this branch. Aligning pr-gatekeeper to the same source metric is a separate `.github/` change
  the owner ships to that repository; until it is deployed, the gate's line policy and the
  factory's source metric disagree and the gate's number is the review standard.

Verify with the size command the worker's rules name, from the host checkout:
`git diff --shortstat $(git merge-base source/HEAD HEAD) HEAD`, and with a source total that
crosses 1001 (a `human_waiting` WorkItem whose `blockedOn` names the decomposition, an empty
`attempts` list and no PR).

## Commands

| Command | What it does |
|---|---|
| `factory new <repo> <title> "<claim> \| <check>" ... [-d <text>] [-w <worker>]` | Opens a `factory:ready` issue; each `-w` adds a `Worker:` line |
| `factory project <id>` | Six lines: title, state and pause reason, used/total passes, deadline in IST, members and states, acceptance item |
| `factory extend <id> <passes> [<deadline>]` | Owner adds 1 to 60 passes and makes the Project active, even if stopped; optional ISO date-time with timezone replaces the deadline |
| `factory stop-project <id> <note>` | Owner stops the Project and holds its members, recording the note |
| `factory status [<repo#n>]` | What the factory is doing, and what it needs from you |
| `factory retry\|regate\|close <repo#n> <note>` | Answers an escalation |
| `factory add-repo <owner/repo>` | Makes a repo a factory target (it needs pr-gatekeeper) |
| `factory bench <repo> [-w <worker>] ...` | Files the benchmark's tasks as `factory:ready` issues in a sandbox repo (see "Measuring workers") |
| `factory bench report [<run>]` | Grades a run, the latest by default |
| `factory stop` | Kill switch: cancels worker tasks, kills sandboxes, stops factory-mcp |
| `factory start` | Starts Docker (OrbStack), the relay and factory-mcp |
| `factory logs` | Follows the factory-mcp log |
| `factory update` | Deploys the deployed checkout's branch, or `main` once that branch is merged, if the tests pass there |

## What it can and cannot do

| Part | It can | It cannot |
|---|---|---|
| Worker (any relay agent: pi, Codex, OpenCode, ...) | Edit and commit in a throwaway container that holds a fresh copy of one repo and a copy of its harness's login files | Push, open PRs, read your files, or use your GitHub account: it has no GitHub credential, and its copy has no remote |
| worker/runner.py | Run the repo's gate `verify` command from the base commit, in the same sandbox | Accept a patch whose tests fail |
| factory-mcp | Push `factory/<issue>` and open the PR. It refuses a patch that touches a protected path (`.github/`, `AGENTS.md`) or takes the PR over the file-count limit before the push (a lines exceedance warns instead, ADR-0070), then checks the pushed branch and the size again | Merge. There is no merge tool |
| pr-gatekeeper | Review and merge, or ask for changes, or send the PR to a person (HUMAN) | Be bypassed by the factory: a HUMAN verdict waits for you |

Limits: a WorkItem gets at most 4 worker passes (then `exhausted`); your `retry` gives it 4 more, also from
`exhausted` (ADR-0035). One exception (ADR-0112): a PR the gate returned CHANGES on keeps one fix pass per gate
round past that count. You may also ask Day Desk in Telegram to retry it (ADR-0043). Two failed attempts in a
row block it. Each pass has 20 minutes. Two passes of one repo run at once (`FACTORY_PASSES_PER_REPO`). A PR
with no gate verdict for 2 hours goes to you. For a Project member, the Project's shared budget and the 8-pass
lifetime cap apply instead (see [the Project contract](contracts.md#project), ADR-0101).

What the design does not protect against:

- **A gate that approves bad code.** The reviewer models are small and can be wrong or fooled.
  Keep acceptance checks concrete, and read what merges.
- **Network access from the sandbox.** The worker can reach the internet, so it could leak the
  repo's code or its model key. Do not point the factory at a repo you would not show the model
  provider.
- **An unprotected `main`.** Nothing in the factory pushes to `main`, but branch protection is the
  independent backstop (ADR-0008). Turn it on where your plan allows it.

### Rollout: the plan recovery allowance (ADR-0070)

By default nothing changes: with no plan allowance approved, Day Desk answers each escalation
once and the owner decides everything else, as before. To let Day Desk recover a whole plan's
waiting issues on its own judgment, approve one bounded allowance on your token:

```bash
# via the factory CLI's MCP tools, or the owner's Day Desk buttons: a write tool, once per plan id
factory_approve_plan_allowance planId="plan/<plan>" actions=["retry","replace"] remaining=<n> by=you
```

That buys Day Desk at most `remaining` recovery uses: queued evidence-based retries of waiting issues,
and replacements that name open issues already in the repo. Every completed use is counted and
audited with its evidence; `factory_status` shows each plan's remaining allowance and the latest use. These
still need you, whatever the allowance says: a HUMAN gate verdict, a protected path, access
trouble, a confirmed environment fault not yet repaired, a replacement that opens new
title-and-body issues, and an exhausted plan or per-item allowance. Make the first representative
use explicit: pick one waiting WorkItem, ask Day Desk to recover it through the allowance, and
read the audit entry and the remaining count before asking for more.

`factory-drive` reads the owner's approved allowance through factory_status's `planAllowances` and
records a request with factory_queue_retry. This phase only queues the request; it does not spend
the allowance or start a worker. The automatic tick consumer is a follow-up issue. After deploying
this profile change, verify the runtime job, not only the source, and the discovered tools:

```bash
hermes -p day-desk cron list                 # factory-drive still exists, its prompt unchanged
hermes -p day-desk mcp test factory_drive    # factory_queue_retry is among the discovered tools
hermes -p day-desk config get mcp_servers.factory_drive --json  # no include list hides the tool
```

Read back the deployed skill itself, from the profile, not the checkout you merged:
`~/.hermes/profiles/day-desk/skills/software-factory/SKILL.md` must carry the same 'Drive the
factory' section — `planAllowances`, the factory_queue_retry route and the factory_status readback
of the queued request.

A green scheduler is not success. In this phase, readback must show a `pendingRetry` request in
`queued` status and the allowance count unchanged; that is a saved request, not a recovered WorkItem.
The follow-up consumer must be deployed before the representative live retry check, which must
show one `allowance_retry` line, an `allowanceUse` naming the planId with a remaining one lower,
one started WorkOrder
and no `ownerAsked`. Read that history from the host, never from a mock, a replay or another checkout. When the job is not
reachable — the gateway is down, `FACTORY_AGENT_TOKEN` is missing, the tool is undiscovered, no
in-scope WorkItem is waiting — name that exact blocker; the rollback is the same as before this
feature: with no allowance approved, driving answers once via factory_resolve_human only.

## Workers

Any agent registered in agent-relay can be a factory worker. Each one runs `worker/runner.py`,
which gives its harness a sandbox (ADR-0028). It names one harness only to add `--auto` to an
OpenCode run (ADR-0111). The agent's args say
which harness runs, with which model, and what it logs in with:

- `--credential HOST_FILE:CONTAINER_DIR` copies one login file into a fresh 0700 directory, mounts
  it at `CONTAINER_DIR` in the harness container only, and fails the pass when the file is missing.
  Repeat it for each file. The runner never copies a file back, so a harness that rotates its
  login in the sandbox leaves the host's copy as it was.
- `--env KEY=VALUE` sets env in the harness container only.
- `--image IMAGE` picks another image; the default `factory-worker:latest` has pinned pi, Codex
  and OpenCode.

The harness, patch collection, and verification run as the same host UID with a writable
container-only HOME (`/tmp/factory-home`). The image sets no `PIP_USER` (worker #266): a forced
pip user mode reaches pip inside a virtualenv too, where an approved chained install like
`python3 -m venv .venv && .venv/bin/python -m pip install -e . pytest` dies, so the image leaves
pip to fall back to a user install for the ordinary non-venv non-root case. Do not re-add the
flag (ADR-0073). Build the image with
`docker build -t factory-worker:latest worker/`; `factory update` rebuilds it when
`worker/Dockerfile` changes, and tags the new build `latest` only after `python`, `python3` and
`git` run in it. Hold point (ADR-0052, amended by ADR-0055): rebuild it — or enable it at all —
only once `factory-mcp` with the `Result.diagnostics` consumer (#189–#191) is deployed and running;
an old image keeps producing legacy Results, and a rebuilt one produces diagnostics only a deployed
consumer reads. Roll out one change at a time, consumers before their producers, and pilot each
producer change on one owner-approved live issue before a broader rollout.
The image also carries `factory-ask` (ADR-0098): a running pass posts one question to Day Desk and
waits briefly for the answer. `factory-ask -h` and `factory-ask --help` print the usage themselves
and never reach the channel (ADR-0111). The runner sets `FACTORY_ASK_URL`
(`http://host.docker.internal:<FACTORY_PORT>/worker/ask`), `FACTORY_ASK_TOKEN` (that pass's own
token) and `FACTORY_ASK_WAIT`; the route is the same loopback listener as MCP, so nothing new is
exposed. It needs Docker Desktop, whose `host.docker.internal` reaches the host loopback: on a
Linux Docker host a container cannot reach that listener, so `factory-ask` prints NO_ANSWER and the
pass carries on without an answer. A deploy that changes `worker/Dockerfile` rebuilds and retags the image, which is how a
pass gets the command. `factory-ask` lives in the worker image, and a change to `worker/factory-ask`
alone does not change `worker/Dockerfile`, so `factory update` does not rebuild the image; rebuild
it with `docker build -t factory-worker:latest worker/` after such a change.

### Runner startup directory failures (#423)

The factory submits `Config.checkout(repo)` as the relay task's `cwd` when
`FACTORY_CHECKOUT_ROOT` is configured (`Foreman.submit`, `Relay.submit`). The relay
selects task cwd, else registered agent cwd, else its inherited process directory.
The service installer also supplies a launchd `WorkingDirectory`. These are source
contracts, not evidence of the failed Mac process's actual selection. WorkOrder v1
and runner arguments supply no explicit checkout directory. The runner uses the
process cwd selected by the relay and never guesses a fallback. An invalid relay
selection may prevent process launch entirely, before the runner can report a Result.

After parsing task identity, the runner resolves and checks its cwd inside its
handler. EPERM or ENOENT produces one failed Result with the original WorkOrder ID,
a `preparation` phase, errno and a bounded explanation. Paths, exception messages,
environment values and task command payload are omitted. No git checkout, Docker
or coding process starts on this failure. The runner does not retry or change any
budget. Retain the relay task IDs and factory attempt history for recovery.

For agent-relay#128, the reported live revision f8e36c4 called `os.getcwd()` before
Result initialization and its handler. This explains the unstructured startup
failure. Subsequent read-only checks on the local Mac found supporting policy-denial
evidence. Kernel logs for 31 October 2026, 00:30–00:40 IST, filtered to System Policy
file-read denials mentioning the configured factory-work checkout, contain four
records: Python denied checkout/parent reads at 00:31:47 and 00:34:17 IST. Factory
audit records for exact WorkItem agent-relay#128 in that window (six dispatch,
submitted and attempt_failed rows, no joins) place its submissions at 00:31:46 and
00:34:17 IST. This is time correlation, not an established task-to-PID join.
TCC attribution for the first denied Python PID links by msgID to AllFiles and
DocumentsFolder authorization requests. The policy responsible and application
attribution requiring repair remain unresolved. No privacy grant or permission
change follows from this evidence alone.

The current launchd configuration and cwd records agree on the relay's service
directory. Registry and factory settings select a checkout under factory-work.
The checkout and ancestors exist and are searchable from Codex under the service's
user account. This does not prove access under the service's responsible-process
context. Isolated tests do not reproduce or repair the host problem. No #128 retry
or live job is part of this change. Before any separately approved host repair,
collect the remaining read-only evidence:

- The selected launch directory and whether it came from task, agent or service inheritance.
- Directory and ancestor existence/search access under the actual service identity.
- Relevant OS denial logs if needed to distinguish access policy from filesystem faults.
- After an approved repair, verify runner startup under that same identity and directory.

Do not infer privacy denial, ACL trouble or deletion from EPERM alone. Do not
retrieve credentials, full environments, command arguments or WorkOrder payloads.

### Worker memory

After deploying the memory reader, rebuild the worker image with the normal pinned worker build
(`docker build -t factory-worker:latest worker/`) and verify that `factory-memory --help` succeeds
inside it. Restart factory-mcp for the new route. The worker's existing pass environment supplies
the capability; do not copy owner tokens into the image. From a running pass, search with
`factory-memory 'previous timeout'` and follow a record with `factory-memory --id ID`.
The command makes no model call; network/model and actual image deployment remain separate checks.

### Worker registration

An agent registered before #98 must add its `--credential` and
`--env` flags: without them the runner mounts no login and the harness fails on its first call.
Register each worker in `~/.config/agent-relay/agents.json`
(replace the host paths), list it in `FACTORY_WORKERS` (preferred first, `:local` or `:hosted`),
restart factory-mcp, and reload agent-relay's registry if it is already running. Two that passed a
live pass on 2026-10-01, both GLM 5.3 Flash through OpenCode Go:

```json
{
  "id": "pi-glm-factory",
  "name": "pi (GLM 5.3 Flash), sandboxed factory worker",
  "command": "python3",
  "args": ["/absolute/path/to/software-factory/worker/runner.py",
           "--credential", "/Users/you/.pi/agent/auth.json:/home/node/.pi/agent",
           "--env", "PI_CODING_AGENT_DIR=/home/node/.pi/agent", "--",
           "pi", "--print", "--provider", "opencode-go", "--model", "glm-5.3-flash",
           "--models", "opencode-go/glm-5.3-flash"],
  "cwd": "/absolute/path/to/factory-work",
  "allowedRoots": ["/absolute/path/to/factory-work"],
  "timeoutMs": 1500000,
  "inheritEnv": ["HOME", "PATH"]
}
```

DeepSeek V4.1 Flash joins through the same restricted path, as an explicitly sandboxed factory
worker — never through relay's generic, unsandboxed `pi-deepseek` agent, and no production
WorkOrder is dispatched to DeepSeek until its pilot (below) is green:

```json
{
  "id": "pi-deepseek-factory",
  "name": "pi (DeepSeek V4.1 Flash), sandboxed factory worker",
  "command": "python3",
  "args": ["/absolute/path/to/software-factory/worker/runner.py",
           "--credential", "/Users/you/.pi/agent/auth.json:/home/node/.pi/agent",
           "--env", "PI_CODING_AGENT_DIR=/home/node/.pi/agent", "--",
           "pi", "--print", "--provider", "deepseek", "--model", "deepseek-flash",
           "--models", "deepseek/deepseek-flash"],
  "cwd": "/absolute/path/to/factory-work",
  "allowedRoots": ["/absolute/path/to/factory-work"],
  "timeoutMs": 1500000,
  "inheritEnv": ["HOME", "PATH"]
}
```

The model ID is the provider-verified one: `deepseek-flash`, pi's official direct alias for
DeepSeek V4.1 Flash. Do not reuse the same ID through another provider — verify each one's exact
name with its own provider first.

After registering an agent in `agents.json` (and only once its pilot has passed), add the agent
before its more proven peers in `FACTORY_WORKERS`, for example
`pi-deepseek-factory:local,pi-glm-factory:local`, restart factory-mcp, and reload agent-relay's
registry if it is already running. Deploying this configuration is a step on the host, separate
from the implementation that lands in this repository; the existing GLM workers stay, so a
DeepSeek outage falls back to them.

The same restricted path registers a sandboxed OpenCode worker, whose template pins its free model
in one `--model provider/name` argument (opencode has no separate `--models` flag). Add the
template to `~/.config/agent-relay/agents.json` once, by hand as above (replace the host paths):

```json
{
  "id": "opencode-mimo-factory",
  "name": "opencode (mimo-v2.6-flash-free), sandboxed factory worker",
  "command": "python3",
  "args": ["/absolute/path/to/software-factory/worker/runner.py",
           "--credential", "/Users/you/.local/share/opencode/auth.json:/tmp/factory-home/.local/share/opencode",
           "--", "opencode", "run", "--model", "opencode/mimo-v2.6-flash-free"],
  "cwd": "/absolute/path/to/factory-work",
  "allowedRoots": ["/absolute/path/to/factory-work"],
  "timeoutMs": 1500000,
  "inheritEnv": ["HOME", "PATH"],
  "image": "factory-worker:latest"
}
```

The runner inserts `--auto` after `run` in an OpenCode command itself (ADR-0111): the pass runs
non-interactively, so without it every permission OpenCode would ask for is auto-rejected.

Further OpenCode free-model pins then register through `factory_register_worker` with
`template: "opencode-mimo-factory"` (below): only the model changes, and only within the `opencode`
provider, until a separately approved template says otherwise.

Routing hard wall (ADR-0085): no pass ever runs in the gate reviewer's model family. `_dispatch`
and the deferred attempt read `reviewer_model` and `strong_reviewer_model` from the repo's
`.github/pr-gatekeeper.json` on the default branch (the gate's config — the factory reads that
file and never writes it) and exclude those families from `route()` outright, after any `Worker:`
line and before the timeout preference (ADR-0081). A missing `reviewer_model` key means
pr-gatekeeper's default, `deepseek-v4.1-flash`; a repo with no gate config at all is routed as
before, because the gate does not review it. So with the default config, `pi-deepseek-factory`
never takes a pass on a gated repo — it is the reviewer — and an issue whose `Worker:` line names
only excluded workers waits for a person with `every allowed worker is in the reviewer family <f>`.

### Choosing workers and timeouts from Day Desk (ADR-0077, ADR-0089)

Day Desk steers worker and timeout decisions within the same allowlist and credential boundary:

- `factory_set_timeout` (`id`, `wallMs`, `by`) sets a durable wall-budget override for one
  WorkItem. Supply an integer at least the current `FACTORY_WALL_MS` (and 60000), at most
  3600000, or `null` to clear it. The live relay's `maxTimeoutMs` must also support the effective
  budget when setting an override; an unreadable or exceeded cap refuses that change. Clearing
  always restores the default, even during a relay outage. Live workers and terminal items
  are refused. Setting it only stores the value and an audit event: it never retries, dispatches,
  resets passes, or grants an allowance. The agent token exposes this write tool too.
  For an approved longer run, call it while the item is blocked, read back
  `factory_status.workItem.wallMsOverride`, then answer it with a retry. Day Desk sets it from the
  evidence it already has (a verify that runs long, a pass that timed out with work in progress)
  and reports the value; it needs no separate approval. Verify the next
  stored WorkOrder's `budget.wallMs`, relay task's `timeoutMs`, and actual Result phases before
  calling the experiment successful. A global env change is unnecessary. Old WorkOrders retain
  their frozen budget. The runner still reserves 60 seconds and subtracts checkout-lock wait time,
  then keeps a flat 3-minute reserve (never more than a quarter of the pass, and none when the verify
  runs on a GitHub runner) for the repo's verify command, keeps 5% of the pass (capped at 60 seconds)
  for collecting the patch and printing the Result, and gives the rest to the coding harness.
- `factory_set_worker` (`id`, `workers`, `by`) replaces a WorkItem's issue `Worker:` lines with the
  agent ids given, in order; `[]` falls back to `FACTORY_WORKERS` order. Only agents in
  `FACTORY_WORKERS` may be named — an unknown id is refused before the edit. It is refused while a
  worker is running and on a closed, cancelled or exhausted WorkItem. The next tick reads the new
  lines, so this is how a retry is steered to another model.
- `factory_register_worker` (`worker`, `model`, `by`, optional `template`) adds a worker to the
  relay's `agents.json` by copying an approved sandboxed template (`template`, default
  `FACTORY_WORKER_TEMPLATE`, currently `pi-glm-factory`; the sandboxed OpenCode worker
  `opencode-mimo-factory` is another) and pinning only `model`: the command, credential mount, env,
  cwd, allowedRoots, image and timeout are the template's, and the provider stays the template's
  too, so `model` may be bare or `provider/model` under that provider and a change of provider is
  refused. The template must itself run `python3` `worker/runner.py` with a harness after `--` —
  a discovered, unsandboxed relay agent, an unknown template id, a command or path injected into a
  template, an unsafe model name, and every attempt to change the credential, image or env
  boundary are refused before the registry is written; the write stays duplicate-checked and
  atomic. It needs `FACTORY_AGENTS_FILE` set to the relay's registry path, and it never adds the
  worker to `FACTORY_WORKERS` — the steps stay separate: this tool only registers, the deploy
  above puts the file on the host, the pilot (below) is the health check, listing the id in
  `FACTORY_WORKERS` is the promotion, and only a real pilot line proves execution — the tests run
  this against fixture registries and claim no live pass. Worker-management tools are write tools
  on Day Desk's driving token, so Hermes asks you before each call.

- `factory_check_worker` (`worker`, `by`) runs one fresh synthetic probe for a registered agent.
  It accepts only Python 3 on this deployment's exact `worker/runner.py`, a pinned model, a
  supported harness, existing credential mounts and non-secret environment keys. The check has
  fixed deadlines, starts no WorkOrder, and stores only the sanitized `factory.health/v1` report
  plus a binding to the worker config, credential-file metadata and image id. Success is valid for
  15 minutes; any later configuration or file metadata change needs another check. A failed check
  is recorded as quarantined. File readability alone is not authentication; only an `ok` probe
  confirms the synthetic response. The probe does not start OrbStack when Docker is stopped.
- `factory_reload_workers` (`by`) calls agent-relay's fixed registry reload endpoint. It edits no
  file and does not change `FACTORY_WORKERS`, start a task or retry an issue. These two driving
  writes let Day Desk check and reload a registered worker; activation remains a separate owner
  operation.

The timeout check reads `GET /healthz` -> `limits.maxTimeoutMs` from agent-relay (verified
against relay revision `3ecf6b72973a5d9dbebb92bb8bfeb9a90f41802c` in the real rehearsal).
A positive integer is the global cap, `null` means no global cap. Missing or malformed values
require a relay upgrade/repair. A transport outage leaves an overridden dispatch pending for the
next tick, without spending a pass. A readable incompatible cap concludes any open recovery
and escalates before a WorkOrder is saved. The registered agent's `timeoutMs` is its default,
not a cap: the factory explicitly submits the effective WorkOrder budget. Run
`python3 scripts/rehearsal.py /absolute/path/to/agent-relay` before deployment to check this
contract against the actual relay server, including its configured cap.

### Provider health pilot (ADR-0076)

Before any production WorkOrder goes to a new model, send one short synthetic, non-WorkOrder
request through the same restricted path the factory workers run — the runner's preflight and a
`docker run` sandbox, whose only mounts are a scratch copy and whatever `--credential` names — and
record that it answers:

```bash
python3 scripts/provider_health.py \\
  --credential /Users/you/.pi/agent/auth.json:/home/node/.pi/agent \\
  --env PI_CODING_AGENT_DIR=/home/node/.pi/agent \\
  -- "pi" "--print" "--provider" "deepseek" "--model" "deepseek-flash" \\
  "--models" "deepseek/deepseek-flash"
```

It prints one `factory.health/v1` line. `status: ok` means the intended model answered the probe
(`--expect`, default `ok`) with its latency and byte counts recorded; `unavailable` names the access
boundary honestly at its own exit code and is never restyled into a pass; `harness_deadline` is a
`timeout` boundary with an unexpected empty stdout — pi print mode buffers the final response until
completion, so empty stdout is not inactivity, and it says nothing about the provider at all.
Quota/auth/rate errors are named (`provider_quota`, `provider_auth`, `provider_rate_limit`)
without the harness's own text, because the report carries only allowlisted fields: for each event
a timestamp, event type, tool name, status, exit code, retry count and byte counts. The prompt, the
response, the WorkOrder payload (the pilot sends none) and any output text are never printed or
archived. The pilot makes one attempt and no retry; it never resets a blocked item and never edits
budgets or passes.

For OpenCode, the same with `"id": "opencode-glm-factory"` and these args:

```json
["/absolute/path/to/software-factory/worker/runner.py",
 "--credential", "/Users/you/.local/share/opencode/auth.json:/tmp/factory-home/.local/share/opencode", "--",
 "opencode", "run", "--model", "opencode-go/glm-5.3-flash"]
```

For Codex, log it in with an API key (`codex login --with-api-key`), not a ChatGPT login: a
ChatGPT login refreshes its tokens, and a refresh in the sandbox can leave the host's copy stale.
Use `--credential ~/.codex/auth.json:/tmp/factory-home/.codex`, `--env CODEX_HOME=/tmp/factory-home/.codex`
and `codex exec --skip-git-repo-check`. A harness ends a pass with its exit code; a non-zero exit
fails it, and the WorkItem follows the usual retry/block path. The runner still requires a patch
and passes the repo's base-commit `verify` command before factory-mcp pushes anything.

### Measuring workers: the benchmark

`factory bench <owner/repo> [-w <worker>] ...` files one `factory:ready` issue per task in
`bench/`, in a repo the factory may change, such as Ami-Khokhar/pr-gatekeeper-sandbox. Each task
builds a new module under `bench/<run>/`, so no two tasks, in one run or in two, touch the same
file. The factory takes them like any issue: a worker, the gate, the merge.

When they end, `factory bench report [<run>]` prints one line per task: its end state, whether a
person was needed (the autonomy rule of `factory report`), the worker passes, the PR's changed
lines, the minutes from the run's start to the merge, and whether the task's hidden tests pass on
the merged code. The hidden tests live only in this repo, so no worker sees them, and they run in
the worker image with no network. Run it once for each worker you want to compare, with `-w`. A
run spends real worker passes and gate reviews, so start one when you want a measure.

## Adding a repo

The repo needs pr-gatekeeper with working secrets, a `.github/pr-gatekeeper.json` with a `verify`
command, and an `AGENTS.md` that carries the factory standard (ADR-0015). For manual setup, copy
`templates/AGENTS.md` into the target repo and merge that PR yourself; `AGENTS.md` is protected.

Day Desk can prepare that one-time setup PR. Ask it to check the repo, then to prepare the
bootstrap PR; approve the `factory_bootstrap_repo` tool call in Telegram. If the gate config is
missing, give it the command that verifies that repo. The PR contains only missing gate files and
the factory-standard block; review and merge it yourself. An empty repo (no commits) needs no
README first: the tool pushes one initial commit with only a `README.md` to the default branch,
says so as `initialCommit` in its result, and then opens the setup PR. A repo that has refs but no
commit on its default branch is refused: push a first commit there yourself. A repo has one setup
PR: when one is open, asking again adopts it, and if the planned files changed (new templates, say),
the tool adds one commit with them to that PR and returns it as `updatedSha`. Whenever the files
or the secret statuses change, it rewrites the PR body to match (ADR-0023). It never force-pushes,
and it refuses a repo with several open setup PRs: close all but one.

A repo the factory already set up can still pin an older gate. When the installed caller names
a `uses: .../gatekeeper.yml@<sha>` and a `gatekeeper_ref: <sha>` older than the revision this
checkout ships in `.github/workflows/pr-gatekeeper.yml`, `factory_repo_check` reports the caller
as stale and the same bootstrap PR rewrites only those two pins, leaving the repo's `verify`
command, permissions, secrets, runner and every other line exactly as they are (ADR-0080). A
workflow the factory did not write, or one already at the current revision, is left alone. The
update merges only by your hand or an explicit setup-merge grant, like any setup PR, and it can
move a repo only to the revision a person has merged into this checkout; correcting that revision
to the 700/1000 policy is a protected `.github/` change you own.

The merge stays manual unless you grant it. To let Day Desk merge this repo's setup PR itself,
say so to the owner's tool `factory_authorize_setup_merge` (with the verify command when the
PR is made in place of `.github/pr-gatekeeper.json`): it pins the digest of exactly what you
review and Day Desk may merge it unattended, with GitHub's checks reviewed first, and tells
whether the repo's own checks pass once merged; it merges no other repository, path, or PR, and
`factory_revoke_setup_merge` takes the authorization back. Without a grant, nothing merges
without you (ADR-0074). The guarded merge takes the setup PR the factory itself created or
adopted, as its audit journal records: a branch name, even one carrying the authorized digest,
never authorizes a PR the factory did not create (ADR-0090), so a refresh that rewrites the PR's
files after creation — the branch keeps its original name — still merges under a new grant for
the refreshed digest. Tell Day Desk to call `factory_repo_check` and only then to go on, after
the merge:

The tool also sets the gate's Actions secrets, so repo setup needs no secrets by hand (ADR-0022).
Store each one once in your login Keychain; each command prompts for the value, so it stays out
of your shell history:

```bash
security add-generic-password -s software-factory/OPENCODE_GO_API_KEY -a factory -w
security add-generic-password -s software-factory/GATEKEEPER_READ_TOKEN -a factory -w
```

`GATEKEEPER_READ_TOKEN` is a token that can read the private Ami-Khokhar/pr-gatekeeper. The tool
leaves alone a secret the repo already has, and reports each one as `set`, `already set`,
`missing: ...` (add it to the Keychain and ask again) or `failed: <command> exited <code>`. It
needs `gh` on factory-mcp's PATH and a `FACTORY_GITHUB_TOKEN` that may read and write the repo's
Actions secrets (a fine-grained token needs the "Secrets" permission, a classic one the `repo`
scope). When `gh secret list` fails, its status ends with gh's first error line, tokens removed; a
403 or "Resource not accessible" there means `FACTORY_GITHUB_TOKEN` lacks that permission.
A fine-grained token cannot read check runs, so in a private repo the factory reads the gate's
workflow run instead, which needs the "Actions" read permission. Without it the WorkItem still
moves on the gate's verdicts, but a gate that never answers reaches you only after
`FACTORY_GATE_WAIT_SECONDS`.

After the merge you need not enable the repo by hand: the first issue a trusted author
(`FACTORY_TRUSTED_AUTHORS`, or `FACTORY_OWNER` when that is empty; with neither, the factory searches
for no one and logs `discover_skipped`) marks
`factory:ready` there enables it (ADR-0027). So does the first plan, proposal or approval Day Desk
makes there, which is the easy way into a repo that has no factory labels yet; `factory new` needs
the label to exist. The factory checks the repo first and writes it to `repos.json` with who
enabled it. If the repo fails the check, the factory comments once on the issue with the failed
checks and their fixes, and takes the issue on its own once the repo passes. It never starts a
worker for a repo whose controls are still missing. Existing valid gate files are preserved, and a conflicting
standard or invalid gate config needs a person to fix it.

```bash
factory add-repo <owner>/<repo>
```

It runs `factory_repo_check` and prints each check, with a fix for each one that fails. When all
pass, `factory_add_repo` clones the worker checkout under `FACTORY_CHECKOUT_ROOT`, creates the
labels and adds the repo to `~/.config/factory-mcp/repos.json`. The factory works on it at once;
nothing restarts. If `verify` needs a tool the sandbox image lacks, the first worker pass fails
with `verify exited 127`: add the tool to `worker/Dockerfile`, then run
`docker build -t factory-worker:latest worker/`.

To take a repo away from the factory, remove it from `FACTORY_REPOS` or delete its entry in
`repos.json`, whichever lists it, and restart factory-mcp. Its
WorkItems stay as they are: no worker starts and no PR opens for them, and they carry on if the
repo comes back. A worker already running finishes; its Result is read when the repo comes back.

### Remote verify: a repo whose verify needs macOS

The worker runs in a Linux container, so a verify such as ideadump's
`xcodegen generate && xcodebuild test ...` cannot pass there. For such a repo the factory has a
GitHub macOS runner run the verify instead (ADR-0045). Nothing runs model-written code on your Mac.
How it goes: the worker writes the patch and skips its verify; factory-mcp pushes the patch to
`factory-verify/<issue>-<n>`; a workflow in the repo runs the verify there; factory-mcp reads the
result and deletes that branch. A pass continues as for any repo. A failure is a failed attempt, and
the run's log tail goes to the next WorkOrder as a finding, so Day Desk may retry it like any code
failure. Each run counts as a pass in the budget.

One-time setup per repo:

1. Add the workflow below as `.github/workflows/factory-verify.yml` in the repo, through a PR that
   you merge yourself: `.github/` is protected, so a worker cannot add it. It runs on the pushed
   branch with no secrets and read-only access, and the patch cannot change `.github/`, so the
   branch's copy is the merged one. Check that the runner image has the simulator the verify names
   (ideadump asks for `iPhone 17`; if `macos-latest` lacks it, use the newest macOS label in
   GitHub's runner-images list). This was not run for this runbook: run it once by hand
   (`git push origin HEAD:refs/heads/factory-verify/test`, then read the run, then delete the branch).
2. Add the repo to `FACTORY_REMOTE_VERIFY_REPOS` (comma-separated, like `FACTORY_LOCAL_ONLY_REPOS`) in
   factory-mcp's environment, then restart factory-mcp. Optional: `FACTORY_REMOTE_VERIFY_SECONDS`.
3. Check: `FACTORY_GITHUB_TOKEN` must be able to push branches, read Actions runs and delete branches
   (fine-grained: Contents write, Actions read). A WorkItem of that repo then shows
   `remoteVerify` in `factory_status` while it waits, and a `remote_verify_started` row in its history.

```yaml
name: factory-verify
on:
  push:
    branches: ['factory-verify/**']
permissions:
  contents: read
jobs:
  verify:
    runs-on: macos-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
        with:
          persist-credentials: false
      - run: |
          command -v xcodegen >/dev/null || brew install xcodegen
          bash -c "$(jq -r .verify .github/pr-gatekeeper.json)"
```

Cost: a public repo's standard GitHub-hosted runners, macOS included, are free. In a private repo
macOS minutes bill at 10 times the Linux rate and draw on the plan's included minutes.

The gate needs a macOS runner too. In `pr-gatekeeper`, `.github/workflows/gatekeeper.yml` sets
`runs-on: ubuntu-latest` on the `gates` job (line 45), and on `review` (line 104). The reusable
workflow's inputs are only `pr`, `base_sha` and `gatekeeper_ref` (lines 20-32), so a calling repo
cannot choose the runner: the gate's verify of an iOS repo fails on Linux until pr-gatekeeper gets an
input for it (say `gates_runs_on`, default `ubuntu-latest`) and the repo's caller workflow passes
`macos-latest`. That is a change in pr-gatekeeper, not in this repo, and it is not made yet.

### Lessons: where they live and what the service resolves (ADR-0020)

The lessons file the service runs with is `FACTORY_LESSONS_FILE`, or
`~/.config/factory-mcp/lessons.json` when it is unset (`factory serve` sets it, next to the
`repos.json` it sets). `factory lessons-path` prints that resolved path and verifies the file
loads, reading no credential and running no command, so you can check a service change before
or after it. A recurring gate finding becomes a lesson only when you approve it; the factory
only presents the candidate.

### Lesson ranking: opt-in, off by default (ADR-0058)

By default a WorkOrder carries the repo's approved lessons newest-first. Optionally, each dispatch
asks TypeSafe's Jev which lesson matters most for this task and sends the lessons in that order.
To turn it on:

1. Judge it first on real tasks of yours (the unit tests prove control flow, not Jev's quality).
2. Put the key in factory-mcp's secrets: `printf 'TYPESAFE_API_KEY=%s\n' "<key>" \
   >> ~/.config/factory-mcp/secrets`. The key goes to TypeSafe only, in one Authorization header;
   it is never logged or stored.
3. Add `FACTORY_JEV_LESSON_RANKING=1` to `~/.config/factory-mcp/env`, then `factory update`.
   With the flag on and the key missing, factory-mcp refuses to start and says so.

What crosses to TypeSafe: the task's goal, acceptance bullets and current `findingsToAddress`,
and this repo's approved lesson texts — nothing else (no other repo's lessons, issue comments,
command output, diffs or secrets). One call per dispatch, five-second timeout, no retries. Any
stop — an abstention, a malformed or unsure answer, an endpoint error — keeps the newest-first
order and logs `lesson_rank_fallback` with a fixed category on the WorkItem (`factory status`
and the audit log show it); the line never stops and no worker pass is spent. Ranking may only
reorder the lessons the code already chose; eligibility and the ten-lesson cap are code's.

## Starting, stopping, rebooting

The relay and factory-mcp are launchd services: they start at login and restart when they
exit. A worker pass starts OrbStack itself when Docker does not answer, so a reboot needs
nothing from you. `factory start` checks all three.

### A real-Docker check of the runner's preflight

The unit tests fake the Docker boundary. `python3 scripts/regression_docker.py` (below) runs the
missing-image (72) and missing-harness (71) preflight codes against real Docker on demand; the
missing-daemon code itself needs Docker stopped, so: with Docker stopped (quit OrbStack), run a
hand-made WorkOrder like the rehearsal does. The Result must fail before the harness runs, with
`diagnostics.phases` naming `environment setup`, exit code `69` and the tail `preflight: the
Docker daemon is not available`, and WorkItem #n must pause as `human_waiting` for the
environment (ADR-0051) rather than spend its passes. Rerun once with Docker up and a wrong
`--image`: exit code `72`, same phase.

`factory stop` stops everything now and keeps it stopped. Work in progress is not lost: after
`factory start`, a cancelled pass counts as one failed attempt and is retried.

### The runner regression harness

Run the runner's behavior against real containers on demand, and after each change to
`worker/runner.py` or `worker/Dockerfile`:

```bash
docker build -t factory-worker:latest worker/     # once, when the image changed
python3 scripts/regression_docker.py
```

It runs real `docker run` sandboxes through the runner for seven deterministic cases: a
successful harness whose work comes back as a patch, a failing verify command, a hung harness
killed at its time limit with its output kept, secret redaction and credential isolation, the
environment preflight failure codes, an oversized patch refused before any verify runs, and
cleanup: no container, work or scratch dir survives. It prints a JSON report of the commands
each case ran and what each showed, and exits 0 only when every case is backed by recorded
evidence. The cases need no model credential, but a missing Docker daemon or worker image is an
explicit nonzero failure naming what to do — it never reports a pass it did not run. The unit
tests fake the Docker boundary (`tests/test_regression_docker.py`); this harness is the
real-container side of the same checks.

## Where things are

| What | Where |
|---|---|
| Deployed code (runs the services) | `~/.local/share/software-factory` |
| Settings / secrets | `~/.config/factory-mcp/env` / `secrets` (mode 600) |
| The relay's API token | `~/.config/agent-relay/token` (mode 600), written by the relay on its first start; factory-mcp and `factory stop` read it. `AGENT_RELAY_TOKEN` in the settings or secrets overrides it |
| WorkItem store and audit log | `~/.local/state/factory-mcp/factory.sqlite3` |
| Logs | `~/.local/state/factory-mcp/server.log`, `~/.local/state/agent-relay/relay.log` |
| Deploy records | `~/.local/state/factory-mcp/deployed`: the last commit for which every deploy step succeeded; `last-update.txt`: the last successful update message sent; `last-update-failure.txt`: the full latest failed update and exit status, retained after duplicate delivery is suppressed |
| Worker checkouts | `$FACTORY_CHECKOUT_ROOT/<owner>/<repo>` |
| Worker definitions | The agents in `FACTORY_WORKERS`, in `~/.config/agent-relay/agents.json` |
| launchd services | `com.agent-relay`, `com.software-factory.factory-mcp` in `~/Library/LaunchAgents` |
| The runner regression harness | `scripts/regression_docker.py`: explicitly invoked real `docker run` containers, seven deterministic cases (success, failing verification, timeout, secret redaction and isolation, preflight, size refusal, cleanup); exits nonzero naming what to do when Docker or the worker image is missing |

A merged change deploys itself: the `factory-update` job below runs `factory update` every 10
minutes (ADR-0031). It fast-forwards the deployed checkout (and moves it to `main` when its branch
is merged), runs the tests there, and restarts factory-mcp only if they pass. A failing test keeps
the old version running, and the job sends you why once for that stable failure. Repeated failures
stay quiet until the output or status changes; the full latest failure remains in
`last-update-failure.txt`. It also rebuilds the worker image when
`worker/Dockerfile` changed, rebuilds Day Desk's sandbox image when `tenant/hermes/sandbox/`
changed, and runs `hermes profile update day-desk -y` when `tenant/hermes/` changed. Roll out one deploy at
a time (ADR-0055): verify the deployed factory-mcp accepts what each producer change emits — the
worker image especially — pilot it on one owner-approved live issue, and only then let the next
change follow. A successful source deploy is not runtime acceptance: the update reports the source
revision and deploy steps only. Pilot the deployed behavior on one owner-approved live issue before
calling that change accepted. A failed
build or profile update stops the deploy with its message, and the next update runs it again from
the deploy record, which names the last commit for which every step succeeded. After a profile
update succeeds, the deploy also verifies the live profile's tool discovery: it asks factory-mcp
what it lists for each token and checks the profile discovers all of them (ADR-0049). A profile
that leaves Day Desk blind is a failed update: the record stays and the next update runs the steps
again. The verification only reads the profile and factory-mcp, so your model, Telegram and secret
settings are untouched, and nothing is printed. A change to
`scripts/factory` brings its own new deploy logic, so after the tests pass the updated script runs
its own deploy steps once more (`--deploy-steps`, under a guard so it cannot chain); the record
advances only after those steps finish, so a step they fail at retries on the next update too, and
once they succeed the script restarts factory-mcp onto the new code. The first update
starts the record at the commit already deployed, so build the images and install the profile from
the deployed checkout once by hand, as below. A retry restarts factory-mcp only when it rebuilds the
worker image, which the restart follows; after a script update's re-executed steps, the restart
always follows once. A change to `tenant/hermes/config.yaml` is reported: the
live config also holds your model and Telegram settings, so merge the rest by hand. The one
factory-owned key the update changes by itself is `mcp_servers.factory_drive.tools.include`, which
it removes when present: the agent token already limits Day Desk's driving tools, and a stale copy
of that list once hid `factory_set_dependencies` (ADR-0048). When a deploy changes the tools
factory-mcp advertises, the update prints the new names and tells you to send `/reload-mcp` in the
Day Desk chat: the running gateway keeps the list it loaded at its last start, so a tool that a
deploy adds stays invisible to that chat until it reloads.
Run `factory update` yourself to deploy at once. The
tests need the packages in `requirements.txt`, installed once for the Python that runs `factory`
and that the `factory-update` job names: `/usr/bin/python3 -m pip install -r requirements.txt`.

## When something looks wrong

- **Nothing happens to a labelled issue.** Check the author (only trusted authors), the repo (in
  `FACTORY_REPOS` or `repos.json`), and the `## Acceptance` format. `factory status` shows a refusal as
  `rebuild_skipped`.
- **Blocked: `verify exited 1`.** The repo's tests failed in the sandbox. Run the verify command
  yourself. If the tests fail on `main` too, fix `main` first.
- **Blocked: `verify exited 127` or a platform error (xcodebuild, a simulator).** The worker
  sandbox is Debian Linux with git, python3, node and ripgrep, no macOS tools and no secrets
  (`worker/Dockerfile`). The verify cannot pass there, so a retry only spends passes (ADR-0044).
  Day Desk says so and offers the workarounds in the skill's `environment.md`: a verify that runs
  on Linux, a split that touches only files a Linux check covers, your own local run, or close.
- **Blocked: `docker is not available`.** Start OrbStack, then `factory retry <id> "docker was down"`.
- **Blocked: each attempt ran out of time.** A pass that ran out of time is not by itself the issue
  being too big: the cause stays unknown until a `Result.diagnostics` phase names it (ADR-0050).
  factory_status shows each attempt's cause — checkout or setup, harness timeout, empty patch,
  failing test, or none — with a redacted, bounded context. A verification failure is the runner's
  post-harness run of the gate's `verify`, not a test the harness chose to run in its own session,
  and its bounded digest of failing/error test identities and tracebacks is carried beside a later
  empty timeout, across the person's retry, so the next pass still sees what to fix (ADR-0078).
  Retry for a cause a retry can fix, and ask Day Desk to split only when the cause is scope (a
  harness timeout that reached the budget) and the issue reads like more than one pass holds. A
  timed-out pass keeps its partial work (ADR-0086): the runner collects the checkout on harness
  exit 124 and the Foreman pushes it to the scratch branch `factory-wip/<issue>` and records it as
  the WorkItem's `wip`, so the next pass bases itself on that commit and is told to keep what is
  right and finish. This also applies to gate-fix passes. A checkpoint is discarded if its source
  WorkOrder, scratch branch head, reviewed PR head, or default branch base no longer matches its
  saved provenance. `FACTORY_SALVAGE=off` turns this off. The wip moves forward at most
  twice, and a partial touching a protected path is never kept; a completed Result, a merge or an
  end deletes the scratch branch and clears the `wip`. A timed-out attempt routes the next pass to another model
  family and the block waits for the policy's failover, at most once per budget (ADR-0081): the
  `recovery` audit rows say what was chosen, and `recovery_result` whether it held.
- **Stuck in `dispatching` with `relay 401`.** The relay refused factory-mcp's token. factory-mcp
  sends `AGENT_RELAY_TOKEN` from its settings or secrets, else `~/.config/agent-relay/token`;
  make it match the relay's (`AGENT_RELAY_TOKEN` in the relay's environment, else that file).
- **Ready or dispatched again after a relay restart.** The relay keeps its tasks and request ids in
  memory, so a restart loses an accepted task. factory-mcp reconciles the facts, then re-queues the
  same WorkOrder under the same idempotency key with no attempt recorded, up to `RELAY_RETRIES_MAX`
  (2) times per budget — a person's retry or an allowance retry starts the count again; the
  `recovery`/`recovery_result` rows of action `relay_lost` show each
  re-queue and its outcome. Past the bound, or when a remote-verify scratch branch was pending, it
  records an ordinary failed attempt instead — read its reason, fix the relay, then retry.
- **HUMAN from the gate.** Read the gate's comment on the PR. `retry` sends its findings to the
  worker; `regate` re-reviews a fix you pushed yourself, or the same head when the gate failed
  itself ("reviewer verdict is invalid"): unless a gate run is going or a newer verdict is there,
  factory-mcp edits a hidden `<!-- factory regate n -->` line in the PR body, which starts a run.

## Day Desk: the factory in Telegram (Hermes)

Day Desk is one Hermes agent you talk to in Telegram (ADR-0013), and it drives the factory for
you (ADR-0030). Through factory-mcp's tools it shows what needs you, plans and starts issues, and
answers the escalations a worker can fix: it sends a PR back to the gate, or retries with a note,
once per WorkItem, and never closes one. With an owner-approved plan allowance (ADR-0070) it may
also queue retries on evidence and replace with open issues of the same repo, counted against the
allowance and audited, without one approval per recovery — see Rollout below — and the driving
job reads back the queued request through factory_status. It reports it as queued, not recovered,
until the automatic consumer is deployed. It calls those tools with its own token,
`FACTORY_AGENT_TOKEN`, for which factory-mcp lists only them. Setting up a repo and approving or
retiring a lesson still go through your token, and Hermes asks you to approve each of those calls
with buttons in Telegram; no answer in 15 minutes is a no. Its settings, skill and manner are the Hermes profile
distribution in `tenant/hermes/`, so they are reviewed like code.

Set it up once:

```bash
hermes profile install ~/.local/share/software-factory/tenant/hermes  # the deployed checkout, so a deploy updates it
cp ~/.hermes/profiles/day-desk/.env.EXAMPLE ~/.hermes/profiles/day-desk/.env
# Day Desk's driving token: a new secret, different from FACTORY_MCP_TOKEN, in factory-mcp's secrets
printf 'FACTORY_AGENT_TOKEN=%s\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  >> ~/.config/factory-mcp/secrets && factory update
# fill in FACTORY_MCP_TOKEN and FACTORY_AGENT_TOKEN (from ~/.config/factory-mcp/secrets),
# TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USERS (your Telegram user id), TELEGRAM_HOME_CHANNEL (the same
# id: without it the cron jobs' messages have nowhere to go) and DAYDESK_GITHUB_TOKEN (below).
# A Day Desk installed before TELEGRAM_HOME_CHANNEL was asked for: add it to that .env, then run
# hermes -p day-desk gateway restart
docker build -t day-desk-sandbox:1 ~/.local/share/software-factory/tenant/hermes/sandbox
hermes -p day-desk model             # choose your ChatGPT or Codex subscription
hermes -p day-desk gateway setup     # Telegram
hermes -p day-desk gateway install   # start it at login
mkdir -p ~/.hermes/profiles/day-desk/scripts
printf '#!/usr/bin/env bash\nexport PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"\nexec "$HOME/.local/bin/factory" watch\n' \
  > ~/.hermes/profiles/day-desk/scripts/factory-watch.sh
hermes -p day-desk cron create "every 5m" --no-agent --script factory-watch.sh --deliver telegram \
  --name factory-watch
printf '#!/usr/bin/env bash\nexport PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"\nexec "$HOME/.local/bin/factory" report\n' \
  > ~/.hermes/profiles/day-desk/scripts/factory-report.sh
hermes -p day-desk cron create "0 9 * * 1" --no-agent --script factory-report.sh --deliver telegram \
  --name factory-report
printf '#!/usr/bin/env bash\nexport PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"\nexec "$HOME/.local/bin/factory" drive-queue\n' \
  > ~/.hermes/profiles/day-desk/scripts/factory-drive-queue.sh
hermes -p day-desk cron create "every 5m" --monitor-script factory-drive-queue.sh --skill software-factory \
  --deliver telegram --name factory-drive "Run the 'Drive the factory' section of the software-factory skill."
cp ~/.local/share/software-factory/tenant/hermes/scripts/factory-update.sh \
  ~/.hermes/profiles/day-desk/scripts/factory-update.sh
chmod +x ~/.hermes/profiles/day-desk/scripts/factory-update.sh
hermes -p day-desk cron create "every 10m" --no-agent --script factory-update.sh --deliver telegram \
  --name factory-update
```

`factory drive-queue` lists each escalation Day Desk may answer as
`o/r#13 is blocked (waiting 30m): verify exited 1`. The wait rounds down to 30-minute
steps for six hours, then rises in 120-minute steps and stops at 2880 minutes (48 hours,
ADR-0109). It lists `o/r#13 asks question <questionId>` for a worker question nobody has
answered. It also lists a stuck acceptance item of an active Project as
`project demo: acceptance o/r#14 is changes_requested` (also `blocked` or `human_waiting`).
This wakes Day Desk every 30 minutes for six hours, then every 2 hours up to 48 hours, while an
item remains stuck, and once for each worker question.
`factory-drive` runs the model only when this queue text changes, and tells you
what it chose for each: through the plan allowance when one covers the WorkItem, reading the
queued `pendingRetry` request back before it answers, else through its own factory_resolve_human answer. `factory-watch` tells you of an escalation Day Desk may not answer, or one
it left for 30 minutes, and of an exhausted WorkItem either way, for non-members.
While Day Desk drives, it suppresses waiting notices for members of an active Project only when
`leadBlockedBy` is empty. A member the Lead cannot answer still notifies the owner, including
a safety stop, environment fault or member pass cap. These notices name the item and its blocker,
for example `o/r#13 is blocked: safety stop: pr-gatekeeper decided HUMAN`.
Unchanged scheduled runs are quiet.
It also reports material state, head, PR, gate-round and worker transitions after its first progress baseline.
A merge or issue closure message says that runtime acceptance still needs confirmation. Without
`FACTORY_AGENT_TOKEN`, the watcher tells you of every owner blocker at once. To retry an exhausted
WorkItem, reply to the alert in Telegram:
Day Desk retries a non-member with a fresh budget, as `factory retry` does (ADR-0043).
An active Project member instead spends its Project's shared budget and stops after 8 lifetime
passes, including passes before a retry. When the Project budget is spent, the next dispatch
pauses the Project and exhausts that member. A paused or stopped Project dispatches no member.
The owner runs `factory extend <id> <passes>` to add passes and resume it, then retries the
exhausted member if it has fewer than 8 passes. An extension preserves the deadline unless a new
ISO date-time with timezone is supplied, and the total may exceed 60 passes. `factory extend`
and `factory stop-project` record the git user name and use owner-only tools that Day Desk cannot
call. Members of done Projects keep the non-member budget rule.

Project notices use a silent first-run baseline, including when upgrading an existing
`watch.json`. After that, `factory-watch` reports:

- `Project Demo is done: 7 issues merged, 23 of 30 passes used.` followed by up to ten
  PR URLs for members in `closed` or `merged`, once on entering `done`.
- `Project Demo paused: the budget of 30 passes is spent. To continue: factory extend demo <passes>`
  once on entering `paused` or when its reason changes.
- An active member's safety stop or environment fault as the item notice above. Other member
  waiting notices stay with Day Desk.

Every watcher line is clipped to 110 characters. The version 2 `watch.json` keeps its `waiting`
and `progress` fingerprints, and adds `projects` (id to state) and `projectPausedReasons`.

`factory-watch` sends one message for each new owner blocker and each material progress transition,
and an error message when factory-mcp does not answer. `factory-report` sends the week in one message every Monday at
09:00 (ADR-0025): the autonomy rate of the WorkItems that ended in the last 7 days and in the 7
days before, how many went to a person or ran out of passes and the 3 most frequent reasons why,
the new lesson candidates and the lessons flagged as not working. Run `factory report` to see it
at any other time, or ask Day Desk. It runs no model and changes nothing. Reply to the message, and Day Desk answers the escalation with
you. `factory update` gives Day Desk the new skill and sandbox image when a deploy changes them.
It updates the profile only when `hermes profile show day-desk` names the deployed checkout as
its source, because a profile installed from another checkout would copy whatever that checkout
holds; otherwise the deploy stops and says so. To move a profile installed from a working
checkout, edit the source path in `~/.hermes/profiles/day-desk/distribution.yaml` to
`~/.local/share/software-factory/tenant/hermes`.
The deploy does not restart Day Desk's gateway, because the job runs inside it; a sandbox change
reaches new shell sessions at once, and `hermes -p day-desk gateway restart` is for anything else.

When you reply to an escalation, Day Desk first reads the failure and the repo's verify command.
If the failure is an environment limit, it does not retry: it explains why and offers workarounds
for you to pick (ADR-0044). It also warns you before the first approval in a repo whose verify
needs a platform the worker lacks. It reads this from the skill's `environment.md`, which you
update when `worker/Dockerfile` or the gate changes.

Day Desk has every Hermes toolset (ADR-0041). Its terminal, file and code tools run in the
sandbox below; its browser and computer use act on your Mac as you. It cannot tick or take in a WorkItem itself (factory-mcp does),
merge or label, and it never pushes except the first commit of an empty repo (ADR-0021; the
skill has the full list). Its shell runs in a Docker container from `tenant/hermes/sandbox/`, with
git, gh and python3. It uses the shell for any task you ask for, and in factory repos only to make
the first commit of an empty repo and to clone and read a repo. Hermes does not ask you before a shell command, so Day Desk shows you each command
that changes GitHub and waits for your yes in chat. The container has no host directory it can
write, and one secret, `DAYDESK_GITHUB_TOKEN`. From the host, Hermes mounts only Day Desk's
skills and its caches of files sent in chat (images, documents, audio, web pages), read-only
under `/root/.hermes`. `~/.hermes/profiles/day-desk/.env`, `~/.ssh`, `~/.config` and your other
files are not mounted. Treat anything you send Day Desk in chat as visible to the shell.

### The shell's GitHub token

Make a fine-grained token at GitHub → Settings → Developer settings → Fine-grained tokens:

- Resource owner: your account. Repository access: Only select repositories, the ones the
  factory works on. Add each new repo to the token when you create it. Expiration: 90 days, then
  make a new one.
- Repository permissions: Contents read and write, and Pull requests read only. Metadata
  read is added for you. Give nothing else. Without Workflows, GitHub refuses a push that changes
  `.github/workflows/`.
- Without Administration, the shell cannot create a repo. You create the empty repo on GitHub,
  and factory_bootstrap_repo does the rest, including its first commit. If you add
  Administration, the token can create repos, but it can also delete a repo and change branch
  protection. Day Desk's skill asks you to create each repo either way.

Open `~/.hermes/profiles/day-desk/.env` in an editor and add the line
`DAYDESK_GITHUB_TOKEN=<the token>`. Do not type the token on a command line. Every repo the
factory works on needs branch protection on its default branch (ADR-0008): a token that can push
could otherwise push there.

To turn the shell on in a profile installed before ADR-0021, build the image as above, add the
token, then run the following. `hermes profile update` keeps your config.yaml, so these commands
make the same changes as `tenant/hermes/config.yaml`:

```bash
hermes profile update day-desk
hermes -p day-desk config unset mcp_servers.factory_drive.tools.include  # its agent token lists the driving tools (ADR-0048)
hermes -p day-desk config set terminal.backend docker
hermes -p day-desk config set terminal.docker_image day-desk-sandbox:1
hermes -p day-desk config set terminal.container_persistent false
hermes -p day-desk config set terminal.cwd /day-desk
hermes -p day-desk config set terminal.env_passthrough '[DAYDESK_GITHUB_TOKEN]'
hermes -p day-desk config set gateway.standalone true
hermes -p day-desk gateway restart
```

### Check that it works

After setting it up, check in Telegram that Day Desk:

1. answers "what needs me?" with the WorkItems `factory status` shows waiting on you;
2. checks a repo without pr-gatekeeper, says what it lacks, and does not enable it;
3. enables a ready repo when you ask, with no approval prompt;
4. starts an issue only after you approve `factory_approve_issue`, and a worker starts on it
   within a minute;
5. runs `whoami; pwd; env | cut -d= -f1` in the sandbox and gets `daydesk`, `/day-desk` and
   no `FACTORY_MCP_TOKEN`, and cannot read `~/.hermes/profiles/day-desk/.env`, `~/.ssh` or
   `~/.config/gh` when asked to. If it can, the gateway runs the shell on the host; stop it
   (`hermes -p day-desk gateway stop`) until that is fixed;
6. relays a factory-watch message, and your reply to it resolves the WorkItem once you approve;
7. turns a goal of two issues into one `factory_start_plan` call that you approve once, and then
   answers "what needs me?" with the second issue waiting for the first.

The profile deploy check uses `hermes -p day-desk mcp test factory_drive` and
`hermes -p day-desk mcp test factory`. Before it runs, `factory update` repairs one
known gap itself: a live `mcp_servers.factory.tools.include` that predates a merged
owner tool. It widens that key in place to the shipped template's reviewed names —
the profile's own entries stay first, and the exact value is read back before
discovery is checked (ADR-0071). This is the only live write an update makes.

Everything else is refused, not guessed: a profile with no include, a null one, a
malformed one, or an include beside an exclude makes the update fail and say so
(`update: ... will not overwrite ... Set the owner tools by hand (RUNBOOK)`), and the
deployment record stays where it was. Set the list by hand, then run `factory update`
again, which verifies it by readback and discovery:

```bash
hermes -p day-desk config set mcp_servers.factory.tools.include '["factory_bootstrap_repo", "factory_add_repo", "factory_approve_lesson", "factory_retire_lesson", "factory_memory_put", "factory_dream_run", "factory_improvement_validate", "factory_approve_plan_allowance", "factory_authorize_setup_merge", "factory_revoke_setup_merge"]'
```

Keep the driving identity out of it: never add the approval tool, or any owner tool,
under `factory_drive` (ADR-0041); the deploy refuses to write that key at all.

The check compares discovered tool names against the live include/exclude filters
from `hermes -p day-desk config get mcp_servers.<server>.tools --json`. An
unsupported command or hidden required tool keeps the successful deployment record
unchanged. Repair the check or filter and run `factory update` again before
retrying blocked work.

Day Desk can call `factory_docker_status` and `factory_docker_control` with
`{"action":"start"}` to recover OrbStack without its terminal. The control verifies
readiness and never retries an issue. `{"action":"stop"}` refuses queued/running
factory work and any running container. Ask to stop Docker explicitly. Stop other
apps and finish Day Desk's terminal session first. The host process needs `docker`
and `orb` on PATH. No Docker socket is mounted into the sandbox.

### Dream eligibility schedule

The profile's `scripts/factory-dream.sh` runs the no-agent reflection check. Install it in the profile
before registering the job:

```bash
cp ~/.local/share/software-factory/tenant/hermes/scripts/factory-dream.sh ~/.hermes/profiles/day-desk/scripts/factory-dream.sh
chmod +x ~/.hermes/profiles/day-desk/scripts/factory-dream.sh
```

Register it for 03:00 Asia/Kolkata in the profile's configured scheduling timezone:
`hermes -p day-desk cron create "0 3 * * *" --no-agent --script factory-dream.sh --name factory-dream`.
Check the job's effective timezone with the installed Hermes version before enabling it; the cron
expression alone does not encode IST. The check writes to the job log without nightly Telegram delivery.

### Enable reflection

The same nightly script calls `factory_dream_run`. Configure `FACTORY_DREAM_URL`,
`FACTORY_DREAM_MODEL` and `FACTORY_DREAM_API_KEY` in the factory service's secure environment, permit
that provider destination, restart factory-mcp, and verify one synthetic run before enabling cron.
Do not commit the key or pass it as a CLI argument. The endpoint must include its chat completion
path. Without endpoint/model the job skips reflection but still records merge facts and revalidates
measurements. Repeated runs inside seven days skip reflection; failures retain that cooldown.
The same call saves candidate memories and a plan, then publishes its issues. Later calls resume
pending publication without another model request. Provider access and deployment need separate checks.

### Improvement admission

The same call publishes saved plans as `factory:proposed` issues. Set `FACTORY_DREAM_AUTO_REPOS`
to an explicit subset of `FACTORY_REPOS` only after validating proposed output and provider access.
One eligible issue per dream may become `factory:ready` through normal trusted-author admission;
normal ticks do the implementation and the gate does the merging. Existing fix-round allowances
still belong to the factory's budget policy; dreaming grants no additional retries.
Check `publication_pending` and `admission_reserved` records after network failures. A create that
might have succeeded is searched by marker on later calls and is not blindly repeated. Resolve
an uncertain intent after checking GitHub, rather than deleting state to force another create.

### Measure and revalidate repairs

Use `factory call factory_improvement_status '{"repo":"owner/repo"}'` to inspect progress.
A merge becomes a factual fix node on the next nightly call, even during the reflection cooldown.
After deploying and measuring relevant work, call factory_improvement_validate with repo,
fingerprint, full deployed revision, source, baselineOpportunities/baselineRecurrences,
opportunities/recurrences and the previous version (0 for creation). The tool records your
attestation; it does not run commands or certify that the deployment includes the fix.
No opportunities means no improvement claim. Keep measurement sources private where appropriate.
Relevant tracked code changes mark advice for revalidation on nightly maintenance. The report and
maintenance window covers the latest 100 repairs per repo; review older records explicitly when
operating beyond that window. All cooldown and admission coordination assumes one shared factory
SQLite database; independent database copies are independent factories, not an HA coordinator.

### Approving an original gate follow-up (#422)

Ordinary admission still requires a trusted original author. Do not add
`github-actions[bot]` to `FACTORY_TRUSTED_AUTHORS`. For an authentic original gate
follow-up, use `factory_approve_issue` with the **owner token**, `by` equal to
`FACTORY_OWNER` (the repo owner login by default), `sourcePr` and `issueDigest`.
The agent token cannot grant this exception, even if it supplies the owner's name.
No new owner configuration is needed. This is a content-specific approval, not bot trust.

First review the issue's complete title and body, including worker/path directives.
Add checkable `## Acceptance` bullets to the original issue if they are missing,
then review again. Acceptance and repo readiness remain required. Compute the digest
from the GitHub issue object using Python's default JSON separators:

```python
content = [issue.get("id"), issue["number"], issue.get("title"), issue.get("body"),
           (issue.get("user") or {}).get("id")]
issueDigest = hashlib.sha256(json.dumps(content, ensure_ascii=True).encode()).hexdigest()
```

The factory verifies GitHub's original bot creation cross-reference to `sourcePr`,
a merged same-repo PR merged by the gate identity, and exactly one reviewed MERGE
comment on that head. Reviewed non-blocking findings must appear in the issue.
It also requires exactly one successful `pull_request` run of the installed gate
workflow on that head, covering the review and issue creation, with GitHub's
`referenced_workflows` identifying a SHA-pinned reusable workflow from
`Ami-Khokhar/pr-gatekeeper-workflow`, matching the caller pin on the source PR base.
Missing/expired, ambiguous or mismatched
records refuse approval. A copied marker, title or ready label is insufficient.
This uses the existing gate identity plus trusted GitHub workflow/timeline records.
It does not attest to arbitrary workflows that share the Actions bot login.
Historical/manual-dispatch runs without these records remain unsupported and fail closed.

Approval records the owner-token authority, approver, original issue ID, digest,
source PR, review ID/content digest/head, workflow run ID/pin and validation result in the audit.
Refusals record a validation outcome. Repeating approval retains one WorkItem.
Admission and dispatch recheck content/provenance. An edit after approval stops new
coding in `human_waiting`; restore the approved content before recovery. Approval
cannot expand an existing WorkItem's scope. A lost audit store requires renewed
owner approval. This neither interrupts an already running worker nor grants a
retry, extra pass, protected-path exception or merge authority.

For agent-relay#60 the original author check explains the reported refusal. Its
original body also lacks factory Acceptance bullets, so provenance alone is not
permission to dispatch it. This change does not edit or retry that issue.
