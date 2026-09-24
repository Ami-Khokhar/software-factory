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
   "software-factory needs a person" and mentions you. Read the reason, then decide:

   ```bash
   factory retry  Ami-Khokhar/agent-relay#22 "what to do differently"   # try again
   factory regate Ami-Khokhar/agent-relay#22 "pushed a fix myself"       # re-review your push
   factory close  Ami-Khokhar/agent-relay#22 "not worth it"             # give up
   ```

4. **Done** is a PR merged by pr-gatekeeper and the issue closed (`Closes #n` is in every PR).

`factory status` lists every WorkItem. `factory status <owner/repo#n>` shows one WorkItem, its
last steps, and what it needs from you.

### Writing issues the factory can finish

- One issue is one PR: at most 600 changed lines and 20 files. Split larger work into issues that
  each make sense alone.
- Make each check readable from the result: a test that must pass, a command that must print
  something, a file that must contain something. "Works well" is not a check.
- Say what must not change. The worker reads the issue literally.
- Only you (`FACTORY_TRUSTED_AUTHORS`) can open issues the factory acts on. Issues from other
  people are skipped, even with the label.

## Commands

| Command | What it does |
|---|---|
| `factory new <repo> <title> "<claim> \| <check>" ... [-d <text>]` | Opens a `factory:ready` issue |
| `factory status [<repo#n>]` | What the factory is doing, and what it needs from you |
| `factory retry\|regate\|close <repo#n> <note>` | Answers an escalation |
| `factory add-repo <owner/repo>` | Makes a repo a factory target (it needs pr-gatekeeper) |
| `factory stop` | Kill switch: cancels worker tasks, kills sandboxes, stops factory-mcp |
| `factory start` | Starts Docker (OrbStack), the relay and factory-mcp |
| `factory logs` | Follows the factory-mcp log |

## What it can and cannot do

| Part | It can | It cannot |
|---|---|---|
| Worker (pi, GLM 5.3 Flash) | Edit and commit in a throwaway container that holds one repo checkout and its model key | Push, open PRs, read your files, or use your GitHub account: it has no GitHub credential |
| worker/runner.py | Run the repo's gate `verify` command from the base commit, in the same sandbox | Accept a patch whose tests fail |
| factory-mcp | Push `factory/<issue>` and open the PR, after it checks the branch, protected paths (`.github/`, `AGENTS.md`), and the size limits | Merge. There is no merge tool |
| pr-gatekeeper | Review and merge, or ask for changes, or send the PR to a person (HUMAN) | Be bypassed by the factory: a HUMAN verdict waits for you |

Limits: a WorkItem gets at most 4 worker passes (then `exhausted`). Two failed attempts in a row
block it. Each pass has 20 minutes. One worker runs per repo at a time. A PR with no gate verdict
for 2 hours goes to you.

What the design does not protect against:

- **A gate that approves bad code.** The reviewer models are small and can be wrong or fooled.
  Keep acceptance checks concrete, and read what merges.
- **Network access from the sandbox.** The worker can reach the internet, so it could leak the
  repo's code or its model key. Do not point the factory at a repo you would not show the model
  provider.
- **An unprotected `main`.** Nothing in the factory pushes to `main`, but branch protection is the
  independent backstop (ADR-0008). Turn it on where your plan allows it.

## Adding a repo

The repo needs pr-gatekeeper with working secrets, and a `.github/pr-gatekeeper.json` with a
`verify` command. Then:

```bash
factory add-repo <owner>/<repo>
```

It clones the worker checkout under `FACTORY_CHECKOUT_ROOT`, creates the labels, adds the repo to
`FACTORY_REPOS`, and restarts factory-mcp. If `verify` needs a tool the sandbox image lacks, add
it to `worker/Dockerfile`, then run `docker build -t factory-worker:latest worker/`.

## Starting, stopping, rebooting

The relay and factory-mcp are launchd services: they start at login and restart when they
exit. Workers need Docker, so turn on OrbStack's "Start at login". After a reboot, run
`factory start` once to check that all three are up.

`factory stop` stops everything now and keeps it stopped. Work in progress is not lost: after
`factory start`, a cancelled pass counts as one failed attempt and is retried.

## Where things are

| What | Where |
|---|---|
| Deployed code (runs the services) | `~/.local/share/software-factory` |
| Settings / secrets | `~/.config/factory-mcp/env` / `secrets` (mode 600) |
| WorkItem store and audit log | `~/.local/state/factory-mcp/factory.sqlite3` |
| Logs | `~/.local/state/factory-mcp/server.log`, `~/.local/state/agent-relay/relay.log` |
| Worker checkouts | `$FACTORY_CHECKOUT_ROOT/<owner>/<repo>` |
| Worker definition | `pi-glm-factory` in `~/.config/agent-relay/agents.json` |
| launchd services | `com.agent-relay`, `com.software-factory.factory-mcp` in `~/Library/LaunchAgents` |

To deploy a new version: `git -C ~/.local/share/software-factory pull`, then
`launchctl kickstart -k gui/$(id -u)/com.software-factory.factory-mcp`.

## When something looks wrong

- **Nothing happens to a labelled issue.** Check the author (only trusted authors), the repo (in
  `FACTORY_REPOS`), and the `## Acceptance` format. `factory status` shows a refusal as
  `rebuild_skipped`.
- **Blocked: `verify exited 1`.** The repo's tests failed in the sandbox. Run the verify command
  yourself. If the tests fail on `main` too, fix `main` first.
- **Blocked: `docker is not available`.** Start OrbStack, then `factory retry <id> "docker was down"`.
- **HUMAN from the gate.** Read the gate's comment on the PR. `retry` sends its findings to the
  worker; `regate` re-reviews a fix you pushed yourself.

## OpenBot (optional)

OpenBot adds a chat Foreman, a 15-minute routine and Telegram escalations on top of the steps above.
The factory works without it. To connect it, add `127.0.0.1 factory.lan.example` to `/etc/hosts`,
and add a custom MCP server in OpenBot admin → Plugins with the id `factory-mcp`, the URL
`https://factory.lan.example:43125/mcp`, and the token from `~/.config/factory-mcp/secrets`.
