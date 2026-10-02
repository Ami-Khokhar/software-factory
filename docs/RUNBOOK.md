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
- To choose the worker, add a `Worker: <relay agent id>` line (or `factory new ... -w <id>`).
  Several lines give an order, and a failed attempt moves only to the next one named. The agent
  must be in `FACTORY_WORKERS`; another name sends the issue to you. Without the line, the factory
  uses `FACTORY_WORKERS` in order (ADR-0029).
- Only you (`FACTORY_TRUSTED_AUTHORS`) can open issues the factory acts on. Issues from other
  people are skipped, even with the label.

## Commands

| Command | What it does |
|---|---|
| `factory new <repo> <title> "<claim> \| <check>" ... [-d <text>] [-w <worker>]` | Opens a `factory:ready` issue; each `-w` adds a `Worker:` line |
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
| Worker (any relay agent: pi, Codex, OpenCode, ...) | Edit and commit in a throwaway container that holds a fresh copy of one repo and a copy of its harness's login files | Push, open PRs, read your files, or use your GitHub account: it has no GitHub credential, and its copy has no remote |
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

## Workers

Any agent registered in agent-relay can be a factory worker. Each one runs `worker/runner.py`,
which gives its harness a sandbox and holds no harness name (ADR-0028). The agent's args say
which harness runs, with which model, and what it logs in with:

- `--credential HOST_FILE:CONTAINER_DIR` copies one login file into a fresh 0700 directory, mounts
  it at `CONTAINER_DIR` in the harness container only, and fails the pass when the file is missing.
  Repeat it for each file. The runner never copies a file back, so a harness that rotates its
  login in the sandbox leaves the host's copy as it was.
- `--env KEY=VALUE` sets env in the harness container only.
- `--image IMAGE` picks another image; the default `factory-worker:latest` has pinned pi, Codex
  and OpenCode.

The harness, patch collection, and verification run as the same host UID with a writable
container-only HOME (`/tmp/factory-home`). Build the image with
`docker build -t factory-worker:latest worker/`; `factory update` rebuilds it when
`worker/Dockerfile` changes. An agent registered before #98 must add its `--credential` and
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
| The relay's API token | `~/.config/agent-relay/token` (mode 600), written by the relay on its first start; factory-mcp and `factory stop` read it. `AGENT_RELAY_TOKEN` in the settings or secrets overrides it |
| WorkItem store and audit log | `~/.local/state/factory-mcp/factory.sqlite3` |
| Logs | `~/.local/state/factory-mcp/server.log`, `~/.local/state/agent-relay/relay.log` |
| Deploy records | `~/.local/state/factory-mcp/deployed`: the last commit for which every deploy step succeeded; `last-update.txt`: the last message the `factory-update` job sent |
| Worker checkouts | `$FACTORY_CHECKOUT_ROOT/<owner>/<repo>` |
| Worker definitions | The agents in `FACTORY_WORKERS`, in `~/.config/agent-relay/agents.json` |
| launchd services | `com.agent-relay`, `com.software-factory.factory-mcp` in `~/Library/LaunchAgents` |

A merged change deploys itself: the `factory-update` job below runs `factory update` every 10
minutes (ADR-0031). It fast-forwards the deployed checkout (and moves it to `main` when its branch
is merged), runs the tests there, and restarts factory-mcp only if they pass. A failing test keeps
the old version running, and the job sends you why. It also rebuilds the worker image when
`worker/Dockerfile` changed, rebuilds Day Desk's sandbox image when `tenant/hermes/sandbox/`
changed, and runs `hermes profile update day-desk -y` when `tenant/hermes/` changed. A failed
build or profile update stops the deploy with its message, and the next update runs it again from
the deploy record, which names the last commit for which every step succeeded. The first update
starts the record at the commit already deployed, so build the images and install the profile from
the deployed checkout once by hand, as below. A retry restarts factory-mcp only when it rebuilds the
worker image, which the restart follows. A change to `tenant/hermes/config.yaml` is only reported: the
live config also holds your model and Telegram settings, so merge it by hand.
Run `factory update` yourself to deploy at once. The
tests need the packages in `requirements.txt`, installed once for the Python that runs `factory`
and that the `factory-update` job names: `/usr/bin/python3 -m pip install -r requirements.txt`.

## When something looks wrong

- **Nothing happens to a labelled issue.** Check the author (only trusted authors), the repo (in
  `FACTORY_REPOS` or `repos.json`), and the `## Acceptance` format. `factory status` shows a refusal as
  `rebuild_skipped`.
- **Blocked: `verify exited 1`.** The repo's tests failed in the sandbox. Run the verify command
  yourself. If the tests fail on `main` too, fix `main` first.
- **Blocked: `docker is not available`.** Start OrbStack, then `factory retry <id> "docker was down"`.
- **Stuck in `dispatching` with `relay 401`.** The relay refused factory-mcp's token. factory-mcp
  sends `AGENT_RELAY_TOKEN` from its settings or secrets, else `~/.config/agent-relay/token`;
  make it match the relay's (`AGENT_RELAY_TOKEN` in the relay's environment, else that file).
- **HUMAN from the gate.** Read the gate's comment on the PR. `retry` sends its findings to the
  worker; `regate` re-reviews a fix you pushed yourself.

## Day Desk: the factory in Telegram (Hermes)

Day Desk is one Hermes agent you talk to in Telegram (ADR-0013), and it drives the factory for
you (ADR-0030). Through factory-mcp's tools it shows what needs you, plans and starts issues, and
answers the escalations a worker can fix: it retries with a note or sends a PR back to the gate,
once per WorkItem, and never closes one. It calls those tools with its own token,
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
cat > ~/.hermes/profiles/day-desk/scripts/factory-update.sh <<'SH'
#!/usr/bin/env bash
# Sends what factory update printed only when it differs from the last run: a commit whose tests
# fail stays on origin, and each run would report it again.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
last="$HOME/.local/state/factory-mcp/last-update.txt"
# Hermes's PATH may find another python3, without requirements.txt: name the one you installed it for
out="$(/usr/bin/python3 "$HOME/.local/bin/factory" update 2>&1)"
[ -z "$out" ] && exit 0  # nothing to deploy
[ "$out" = "$(cat "$last" 2>/dev/null)" ] && exit 0
printf '%s\n' "$out" > "$last"
printf '%s\n' "$out"
SH
chmod +x ~/.hermes/profiles/day-desk/scripts/factory-update.sh
hermes -p day-desk cron create "every 10m" --no-agent --script factory-update.sh --deliver telegram \
  --name factory-update
```

`factory-drive` runs the model only when the escalations Day Desk may answer change, and tells you
what it chose for each. `factory-watch` tells you of an escalation Day Desk may not answer, or one
it left for 30 minutes; without `FACTORY_AGENT_TOKEN` it tells you of every one at once.

`factory-watch` sends one message for each WorkItem that newly waits on you, and an error message
when factory-mcp does not answer. `factory-report` sends the week in one message every Monday at
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

Day Desk has no file tools, browser or subagents (`agent.disabled_toolsets` in
`tenant/hermes/config.yaml`). It cannot tick or take in a WorkItem itself (factory-mcp does),
merge or label, and it never pushes except the first commit of an empty repo (ADR-0021; the
skill has the full list). Its shell runs in a Docker container from `tenant/hermes/sandbox/`, with
git, gh and python3. It uses the shell to make the first commit of an empty repo and to clone and
read a repo. Hermes does not ask you before a shell command, so Day Desk shows you each command
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
hermes -p day-desk config set agent.disabled_toolsets '[code_execution, file, browser, computer_use, delegation]'
hermes -p day-desk config set terminal.backend docker
hermes -p day-desk config set terminal.docker_image day-desk-sandbox:1
hermes -p day-desk config set terminal.container_persistent false
hermes -p day-desk config set terminal.cwd /day-desk
hermes -p day-desk config set terminal.env_passthrough '[DAYDESK_GITHUB_TOKEN]'
hermes -p day-desk gateway restart
```

### Check that it works

After setting it up, check in Telegram that Day Desk:

1. answers "what needs me?" with the WorkItems `factory status` shows waiting on you;
2. checks a repo without pr-gatekeeper, says what it lacks, and does not enable it;
3. enables a ready repo only after you approve the `factory_add_repo` call;
4. starts an issue only after you approve `factory_approve_issue`, and a worker starts on it
   within a minute;
5. runs `whoami; pwd; env | cut -d= -f1` in the sandbox and gets `daydesk`, `/day-desk` and
   no `FACTORY_MCP_TOKEN`, and cannot read `~/.hermes/profiles/day-desk/.env`, `~/.ssh` or
   `~/.config/gh` when asked to. If it can, the gateway runs the shell on the host; stop it
   (`hermes -p day-desk gateway stop`) until that is fixed;
6. relays a factory-watch message, and your reply to it resolves the WorkItem once you approve;
7. turns a goal of two issues into one `factory_start_plan` call that you approve once, and then
   answers "what needs me?" with the second issue waiting for the first.
