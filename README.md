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
