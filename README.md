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
