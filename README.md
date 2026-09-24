# software-factory

A human states a goal; the factory drives it to a reviewed, merged PR and escalates to a
human only on true exceptions.

- **Control plane:** OpenBot (jobs, governance, audit, HITL, routines).
- **Execution bus:** [agent-relay](https://github.com/Ami-Khokhar/agent-relay) (workers: pi, codex, opencode, …).
- **Gate and merge authority:** pr-gatekeeper.
- **Source of truth:** GitHub.

Start with [`docs/HANDOFF.md`](docs/HANDOFF.md). Contracts are in [`schemas/`](schemas/) and
explained in [`docs/contracts.md`](docs/contracts.md). Decisions are in
[`docs/decisions.md`](docs/decisions.md).

```bash
python3 -m unittest discover -s tests -p 'test_*.py'
```

## Run factory-mcp

```bash
FACTORY_MCP_TOKEN=<secret> FACTORY_REPOS=owner/repo FACTORY_GITHUB_TOKEN=<token> \
  python3 -m factory_mcp        # serves POST http://127.0.0.1:43125/mcp
```

| Variable | Meaning |
|---|---|
| `FACTORY_MCP_TOKEN` | Required. Bearer token OpenBot sends (the credential field when adding the server) |
| `FACTORY_REPOS` | Comma-separated repo allowlist |
| `FACTORY_GITHUB_TOKEN` | GitHub REST token (fine-grained: issues, pull requests, contents) |
| `FACTORY_RELAY_URL` | agent-relay HTTP API, default `http://127.0.0.1:43124` |
| `FACTORY_DB` | SQLite path, default `~/.local/state/factory-mcp/factory.sqlite3` |
| `FACTORY_HOST`, `FACTORY_PORT` | Listen address, default `127.0.0.1:43125` |
| `FACTORY_TLS_CERT`, `FACTORY_TLS_KEY` | Serve https; OpenBot refuses plain-http custom servers |
| `FACTORY_WORKERS` | Relay agents with locality, preferred first, e.g. `codex:hosted,pi-glm:local` |
| `FACTORY_LOCAL_ONLY_REPOS` | Repos whose work may only go to `local` workers |
| `FACTORY_MAX_PASSES`, `FACTORY_WALL_MS` | WorkOrder budget: worker passes per WorkItem (4), wall time per pass (20 min) |
| `FACTORY_GIT_URL` | Where patch Results are pushed, default `https://github.com/{repo}.git` |
| `FACTORY_OWNER` | GitHub login that owns escalations, default the repo owner |
| `FACTORY_POLL_SECONDS` | Background tick interval, default 60; `0` leaves ticking to the OpenBot routine |
| `FACTORY_GATE_WAIT_SECONDS` | No gate verdict on the PR head for this long sends it to a person, default 7200 |
| `FACTORY_GITHUB_API` | GitHub REST base URL, default `https://api.github.com` (GitHub Enterprise, rehearsal) |

`python3 scripts/rehearsal.py <agent-relay checkout> [--lose-store]` runs one issue from
`factory:ready` to `closed` with the real relay and a stand-in GitHub.

The OpenBot tenant package is in `tenant/factory/`; install it with `scripts/install-tenant.sh`
(see `docs/HANDOFF.md` §14).
