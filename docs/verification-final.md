# Final integrated verification checkpoint — 2026-10-13

Checkpoint for #205: the whole owner specification's recovery, completion reporting and staged
rollout. The name of each state matters more than its headline: **implementation merged**, **runtime
verified**, and **live deployed** are three different facts (ADR-0068), and each part below says
which of them it is. Nothing here is a pass that was not run; a command that could not run is
recorded with its exact error as an explicit incomplete blocker, never omitted.

## With what

The checks ran against the checkout of `Ami-Khokhar/software-factory` on branch `factory/205` at the
worker's commit. The environment is the worker sandbox: a Linux host with `/usr/bin/python3` and
`/usr/bin/git`, PyYAML and jsonschema installed from `requirements.txt`, and **no Docker daemon and
no agent-relay checkout** — `/tmp/opencode/agent-relay` does not exist there.

## What was executed

### The gate's verify, with the new integrated regression suite

```bash
$ python3 -m unittest discover -s tests -p 'test_*.py'
Ran 565 tests in 55.462s
OK
```

The five new tests in `tests/test_final_verification.py` drive the combined cases over the public
Foreman API: a replacement interrupted on its last write replays the one missing write after a
store restart and refuses as conflict-free repeat-free once done; a replacement worker that busts
the 600-line PR limit fails twice and ends `blocked`, reported once, with the size reason and no
push that exceeds the limits; a confirmed environment fault pauses the replacement on the first
pass and the weekly report names the environment; a dependency that would cycle the plan is
refused before any GitHub write; a timed-out `gh secret set` reports `failed: gh secret set
timed out` with neither the reason nor the status ever carrying the secret's value.

### The relay rehearsal: normal and lost-store paths

```bash
$ python3 scripts/rehearsal.py /tmp/opencode/agent-relay
/usr/bin/python3: can't open file '/tmp/opencode/agent-relay/src/server.py': [Errno 2] No such file or directory
RuntimeError: http://127.0.0.1:36121/healthz did not come up   (exit 1)
$ python3 scripts/rehearsal.py /nonexistent/agent-relay --lose-store
/usr/bin/python3: can't open file '/nonexistent/agent-relay/src/server.py': [Errno 2] No such file or directory
RuntimeError: http://127.0.0.1:39185/healthz did not come up   (exit 1)
```

**Blocked, exits recorded.** The rehearsal starts the real agent-relay server from a checkout; no
relay checkout is reachable from this sandbox, so the relay never comes up, both normal and
`--lose-store`, and the whole path from a patch Result to a pushed branch on a bare repo is not
stretched. Blocker: no agent-relay checkout in this environment.

### Real Docker worker regression and bare-repo deployment

```bash
$ timeout 200 python3 scripts/regression_docker.py
regression harness failed: docker is not on PATH; install Docker and rerun scripts/regression_docker.py
{"cases": [], "status": "failed", "error": "docker is not on PATH"}   (exit 1)
```

**Blocked, exit recorded** for the real Docker harness and, with it, the bare-repo deployment case
(FACTORY_GIT_URL inside the rehearsal). The macOS runner, Day Desk's live Telegram host, the live
`factory update` deploy, and the live host tools/diagnostics/recovery history and audit comparison
were **not reachable from this sandbox at all**: no claim is made from mocks or a simulator about
them, and they stay **unfinished, not passed**.

## Rollout stand

Rollout lands one change at a time, deployed consumers before worker producers (ADR-0052), and the
first live run is one owner-approved representative issue before wider use (ADR-0068). This
checkpoint itself is recorded work, not a rollout: the gate stays the only merge authority,
`.github/` and the repo root's `AGENTS.md` stay protected, and 600 changed lines, 20 files, and
the pass budget are unchanged.

## Where each claim stands

| Part | Implementation merged | Runtime verified | Live deployed |
|---|---|---|---|
| The gate's verify with the integrated suite | not merged yet (this branch) | yes, in this worker sandbox | no |
| Rehearsal, normal and lost-store | not merged yet | **no — exact blocker above** | no |
| Real Docker regression, bare-repo deployment | not merged yet | **no — exact blocker above** | no |
| macOS runner, live Day Desk host, live discovery/audit | n/a | **unfinished — not reachable from a sandbox** | no |

The live host tools, diagnostics, recovery history and audit comparison remain unfinished work
after this checkpoint: they belong to the next environment, and reading them there in the
owner-approved representative run is the blocker this branch leaves standing, not a fabricated
pass from mocks.
