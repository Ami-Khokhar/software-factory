# software-factory

A person states a goal as a GitHub issue with checkable acceptance criteria. The factory drives
it to a reviewed, merged pull request, and asks the person only when it is stuck.

- **Front door:** Day Desk, one Hermes agent in Telegram ([`tenant/hermes/`](tenant/hermes/)), and
  the `factory` command. Day Desk can prepare a one-time repo setup PR for your merge.
- **Foreman:** factory-mcp, in this repo. It owns every WorkItem's state and makes every decision.
- **Workers:** [agent-relay](https://github.com/Ami-Khokhar/agent-relay) runs Freebuff with GLM 5.3
  Flash in a Docker sandbox ([`worker/`](worker/)).
- **Gate and merge authority:** pr-gatekeeper.
- **Source of truth:** GitHub.

To run the factory, read [`docs/RUNBOOK.md`](docs/RUNBOOK.md). To change it, read
[`AGENTS.md`](AGENTS.md) first, then [`docs/architecture.md`](docs/architecture.md). The contracts
are in [`schemas/`](schemas/), explained in [`docs/contracts.md`](docs/contracts.md), and the
decisions are in [`docs/decisions.md`](docs/decisions.md).

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
| `FACTORY_MCP_TOKEN` | Required. The bearer token every client sends: Day Desk's `.env` and the `factory` command's secrets file hold it |
| `FACTORY_REPOS` | Comma-separated repo allowlist |
| `FACTORY_REPOS_FILE` | `repos.json`, the repos a factory tool enabled; they join the allowlist (ADR-0014). `factory serve` sets `~/.config/factory-mcp/repos.json` |
| `FACTORY_LESSONS_FILE` | `lessons.json`, the lessons the owner approved from repeated gate findings (ADR-0020). `factory serve` sets `~/.config/factory-mcp/lessons.json` |
| `FACTORY_GITHUB_TOKEN` | GitHub REST token (fine-grained: issues, pull requests, contents, secrets read and write) |
| `FACTORY_RELAY_URL` | agent-relay HTTP API, default `http://127.0.0.1:43124` |
| `AGENT_RELAY_TOKEN` | agent-relay's API token, sent as `Authorization: Bearer` on every relay call. Unset: the file at `AGENT_RELAY_TOKEN_FILE` (default `~/.config/agent-relay/token`, which the relay writes on its first start), read on each call. With neither, no token is sent |
| `FACTORY_DB` | SQLite path, default `~/.local/state/factory-mcp/factory.sqlite3` |
| `FACTORY_PORT` | The port on 127.0.0.1, default 43125. factory-mcp speaks plain HTTP, so it listens on loopback only |
| `FACTORY_WORKERS` | Relay agents with locality, preferred first, e.g. `freebuff-factory:local` |
| `FACTORY_LOCAL_ONLY_REPOS` | Repos whose work may only go to `local` workers |
| `FACTORY_MAX_PASSES`, `FACTORY_WALL_MS` | WorkOrder budget: worker passes per WorkItem (4), wall time per pass (20 min) |
| `FACTORY_GIT_URL` | Where patch Results are pushed, default `https://github.com/{repo}.git` |
| `FACTORY_OWNER` | GitHub login that owns escalations, default the repo owner |
| `FACTORY_POLL_SECONDS` | Background tick interval, default 60; `0` turns it off, and only `factory_tick` ticks |
| `FACTORY_GATE_WAIT_SECONDS` | No gate verdict on the PR head for this long sends it to a person, default 7200 |
| `FACTORY_GITHUB_API` | GitHub REST base URL, default `https://api.github.com` (GitHub Enterprise, rehearsal) |
| `FACTORY_CHECKOUT_ROOT` | Worker checkouts live at `<root>/<owner>/<repo>`; sent to the relay as the task cwd |
| `FACTORY_TRUSTED_AUTHORS` | Issue authors a worker may act for, default the repo owner |

`python3 scripts/rehearsal.py <agent-relay checkout> [--lose-store]` runs one issue from
`factory:ready` to `closed` with the real relay and a stand-in GitHub.
