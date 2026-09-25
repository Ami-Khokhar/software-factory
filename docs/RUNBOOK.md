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
| `factory update` | Deploys the deployed checkout's branch, or `main` once that branch is merged, if the tests pass there |

## What it can and cannot do

| Part | It can | It cannot |
|---|---|---|
| Worker (pi, GLM 5.3 Flash) | Edit and commit in a throwaway container that holds a fresh copy of one repo and its model key | Push, open PRs, read your files, or use your GitHub account: it has no GitHub credential, and its copy has no remote |
| worker/runner.py | Run the repo's gate `verify` command from the base commit, in the same sandbox | Accept a patch whose tests fail |
| factory-mcp | Push `factory/<issue>` and open the PR. It refuses a patch that touches a protected path (`.github/`, `AGENTS.md`) before the push, then checks the pushed branch and the size limits | Merge. There is no merge tool |
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

The repo needs pr-gatekeeper with working secrets, a `.github/pr-gatekeeper.json` with a `verify`
command, and an `AGENTS.md` that carries the factory standard (ADR-0015): copy `templates/AGENTS.md`
from this repo, fill in its top sections, and merge that PR yourself, because `AGENTS.md` is a
protected path. Then:

```bash
factory add-repo <owner>/<repo>
```

It runs `factory_repo_check` and prints each check, with a fix for each one that fails. When all
pass, `factory_add_repo` clones the worker checkout under `FACTORY_CHECKOUT_ROOT`, creates the
labels and adds the repo to `~/.config/factory-mcp/repos.json`. The factory works on it at once;
nothing restarts. If `verify` needs a tool the sandbox image lacks, the first worker pass fails
with `verify exited 127`: add the tool to `worker/Dockerfile`, then run
`docker build -t factory-worker:latest worker/`.

To take a repo away from the factory, remove it from `FACTORY_REPOS` and restart factory-mcp. Its
WorkItems stay as they are: no worker starts and no PR opens for them, and they carry on if the
repo comes back. A worker already running finishes; its Result is read when the repo comes back.

## Starting, stopping, rebooting

The relay and factory-mcp are launchd services: they start at login and restart when they
exit. A worker pass starts OrbStack itself when Docker does not answer, so a reboot needs
nothing from you. `factory start` checks all three.

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

To deploy a new version, run `factory update`. It fast-forwards the deployed checkout (and moves
it to `main` when its branch is merged), runs the tests there, and restarts factory-mcp only if
they pass. A failing test keeps the old version running.

## When something looks wrong

- **Nothing happens to a labelled issue.** Check the author (only trusted authors), the repo (in
  `FACTORY_REPOS`), and the `## Acceptance` format. `factory status` shows a refusal as
  `rebuild_skipped`.
- **Blocked: `verify exited 1`.** The repo's tests failed in the sandbox. Run the verify command
  yourself. If the tests fail on `main` too, fix `main` first.
- **Blocked: `docker is not available`.** Start OrbStack, then `factory retry <id> "docker was down"`.
- **HUMAN from the gate.** Read the gate's comment on the PR. `retry` sends its findings to the
  worker; `regate` re-reviews a fix you pushed yourself.

## Day Desk: the factory in Telegram (Hermes)

Day Desk is one Hermes agent you talk to in Telegram (ADR-0013). Through factory-mcp's tools it
shows what needs you, checks and enables repos, plans and starts issues, and answers escalations.
Hermes asks you to approve each tool call that changes something, with buttons in Telegram, and
no answer in 15 minutes is a no. Its settings, skill and manner are the Hermes profile
distribution in `tenant/hermes/`, so they are reviewed like code.

Set it up once:

```bash
hermes profile install ~/path/to/software-factory/tenant/hermes
cp ~/.hermes/profiles/day-desk/.env.EXAMPLE ~/.hermes/profiles/day-desk/.env
# fill in FACTORY_MCP_TOKEN (from ~/.config/factory-mcp/secrets), TELEGRAM_BOT_TOKEN and
# TELEGRAM_ALLOWED_USERS (your Telegram user id)
hermes -p day-desk model             # choose your ChatGPT or Codex subscription
hermes -p day-desk gateway setup     # Telegram
hermes -p day-desk gateway install   # start it at login
mkdir -p ~/.hermes/profiles/day-desk/scripts
printf '#!/usr/bin/env bash\nexport PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"\nexec "$HOME/.local/bin/factory" watch\n' \
  > ~/.hermes/profiles/day-desk/scripts/factory-watch.sh
hermes -p day-desk cron create "every 5m" --no-agent --script factory-watch.sh --deliver telegram \
  --name factory-watch
```

`factory-watch` sends one message for each WorkItem that newly waits on you, and an error message
when factory-mcp does not answer. Reply to the message, and Day Desk answers the escalation with
you. After `factory update` deploys a new version, run `hermes profile update day-desk` so Day
Desk gets the new skill.

Day Desk cannot tick or take in a WorkItem itself (factory-mcp does), merge, label or push. Its
shell is denied the `factory` write commands, `gh pr merge`, `gh label`, `gh api` and the
factory's config directory (`approvals.deny` in `tenant/hermes/config.yaml`). The shell still
runs as you, so these rules are a guard, not a sandbox.

### Check that it works

After setting it up, check in Telegram that Day Desk:

1. answers "what needs me?" with the WorkItems `factory status` shows waiting on you;
2. checks a repo without pr-gatekeeper, says what it lacks, and does not enable it;
3. enables a ready repo only after you approve the `factory_add_repo` call;
4. starts an issue only after you approve `factory_approve_issue`, and a worker starts on it
   within a minute;
5. refuses "merge PR 12" and "run factory retry" in its shell;
6. relays a factory-watch message, and your reply to it resolves the WorkItem once you approve.
