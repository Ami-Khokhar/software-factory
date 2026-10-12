# Batch A verification checkpoint — 2026-10-12

The final name of this checkpoint matters more than its headline: **implementation merged**, **runtime
verified**, and **live deployed** are three different facts, and each part below says which of them
it is. Nothing here is a pass that was not run; commands that could not be run are recorded with
their exact error, not omitted.

## With what

The checks ran against the checkout of `Ami-Khokhar/software-factory` on branch `factory/196` at
commit `c71d14a494627d7ea0efe2004ab5e923ec1413e0` ("Explicit real-Docker runner regression harness
(#195)", the head that #195 merged). The environment is the worker sandbox: a Linux host with
`/usr/bin/python3`, `/usr/bin/git`, PyYAML and jsonschema (installed from `requirements.txt`), and
**no Docker daemon and no agent-relay** — a `/tmp/opencode/agent-relay` path named by the
dispatcher does not exist (`ls: cannot access /tmp/opencode/agent-relay: No such file or
directory`).

## What was executed

### The gate's verify

```bash
$ python3 -m unittest discover -s tests -p 'test_*.py'
Ran 451 tests in 38.685s
OK
```

This is **runtime verified in the worker sandbox**. It is not the gate's own verdict: pr-gatekeeper
is expected to run this same command on the PR head, in its own job, after factory-mcp pushes the
branch, and its verdict is the only merge authority (ADR-0004). No mock environment is claimed to
stand in for that verdict.

### The relay rehearsal: normal and lost-store paths

```bash
$ python3 scripts/rehearsal.py /tmp/opencode/agent-relay
Traceback (most recent call last):
    wait_http(f"http://127.0.0.1:{relay_port}/healthz")
raise RuntimeError(f"{url} did not come up")
RuntimeError: http://127.0.0.1:46479/healthz did not come up   (exit 1)
$ python3 scripts/rehearsal.py /nonexistent/agent-relay --lose-store
raise RuntimeError(f"{url} did not come up")
RuntimeError: http://127.0.0.1:33557/healthz did not come up   (exit 1)
```

**Blocked, exit recorded.** The rehearsal starts the real agent-relay server from a checkout
(`src/server.py`); no relay checkout exists in this environment, so the relay never comes up and the
rehearsal fails before any tool call, both normal and `--lose-store`. The blocker is exact: no
agent-relay checkout is reachable from this sandbox; the input named `/tmp/opencode/agent-relay`,
and that path does not exist.

### Real Docker harness and bare-repo deployment

```bash
$ timeout 300 python3 scripts/regression_docker.py
regression harness failed: docker is not on PATH; install Docker and rerun scripts/regression_docker.py
{"harness": "scripts/regression_docker.py", "image": "factory-worker:latest", "cases": [], "status": "failed", "error": "docker is not on PATH; install Docker and rerun scripts/regression_docker.py"}
(exit 1)
```

The bare-repo deployment case (factory-mcp pushing a patch to a bare repo as it pushes to GitHub)
lives inside the rehearsal (`FACTORY_GIT_URL`), so it shares the same blocker: no relay, no stretch
of the path from a patch Result to a pushed branch. **Blocked, exit recorded** for both. A macOS
runner, Day Desk's Telegram gateway, and the live `factory update` deploy are host things and were
**not reachable from here at all**: no claim is made from mocks about them, and they are recorded as
**unfinished, not passed**.

## Rollout stand

The previous attempt's finding — `pi-glm-factory: unknown field diagnostics` — is the cost of doing
this wrong: a worker image that produced `Result.diagnostics` while the deployed factory-mcp was
still the older controller, so every Result it sent was rejected by the schema's
`additionalProperties: false` and a pass was lost per WorkOrder (ADR-0052). It must not happen
again, in either direction. So:

1. **Consumers go first.** Rebuild the worker image — or enable any new producer behavior at all —
   only once the factory-mcp that accepts it is deployed and *observed running* on the host
   (ADR-0052's hold point, which this verifies is still the rule). One end-to-end rollout lands per
   deploy cycle; no producer and consumer change ships interleaved.
2. **Pilot before broad rollout.** One owner-approved representative live issue runs the new
   behavior first; a wider rollout follows only after that issue merges with a clean human-deployed
   run behind it.
3. **Nothing here changes authority, the protected paths, or the patch limits.** `.github/` and
   `AGENTS.md` stay worker-refused; 600 changed lines, 20 files, and `FACTORY_MAX_PASSES` stay as
   they are; the gate stays the only merge authority.

## Where each claim stands

| Part | Implementation merged | Runtime verified | Live deployed |
|---|---|---|---|
| The gate's verify passes here | not merged yet (this branch) | yes, in this worker sandbox | no |
| Rehearsal, normal and lost-store | not merged yet | **no — exact blocker above** | no |
| Real Docker regression harness | not merged yet | **no — exact blocker above** | no |
| macOS/Day Desk live discovery | n/a | **unfinished — not reachable from a sandbox** | no |

Live host and relay dwell-time checks remains unfinished work after this checkpoint: they belong to
the next environment, not to a fabricated pass from this one.
