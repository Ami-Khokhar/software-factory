# What the factory can run

Read this before you answer an escalation, retry a WorkItem or tell the owner a repo is ready. A
worker can pass only a verify command that the worker sandbox can run. Each fact has its source
file in the repo `software-factory`.

## The worker sandbox

- One worker pass runs in a throwaway Docker container on the owner's Mac (`worker/runner.py`,
  `Sandbox.run`). The image is `worker/Dockerfile`: `FROM node:24-bookworm-slim`, so the OS is
  Debian bookworm Linux, never macOS. The container's CPU architecture is the host's: unknown.
- Installed in the image: git, python3 (also as `python`), pip, venv, ca-certificates, ripgrep, curl,
  procps, lsof, node and npm, and three harnesses (pi, codex, opencode). Nothing else.
- Not installed: xcodegen, xcodebuild, Xcode, an iOS Simulator, Swift, Go, Rust, Java, a C
  compiler, Docker, a GPU driver. The list is "what the Dockerfile installs": a tool missing
  from it is missing in the pass.
- `pip install` works in a pass (the Dockerfile sets `PIP_USER=1` and says verify may pip-install
  freely). The runner sets no `--network` flag, so Docker's default network applies: outbound
  access is expected but was not tested. The image has no apt access for the worker: it runs as a
  non-root user and `--cap-drop ALL`, so a worker cannot `apt-get install` a missing tool.
- The container has the checkout copy and a copy of the harness's model credential. It has no
  GitHub credential, no host file, no push (`worker/runner.py` docstring, ADR-0017). A repo
  secret, an API key for the repo's own services, a signing certificate or a database the verify
  needs is not there.
- Limits per container: 6 GB memory, 4 CPUs, 1024 processes (`Sandbox.run`).

## The pass budget

- `FACTORY_MAX_PASSES` is 4 worker passes between two decisions (`factory_mcp/config.py`). A
  pass has 20 minutes (`wall_ms`): the harness gets 70% of it and the repo's verify command
  25%. A timed-out pass keeps none of its work (`runner.py`).
- The runner runs the repo's `verify` command itself, in the sandbox, after the harness, from
  the base commit's `.github/pr-gatekeeper.json`. A non-zero exit fails the pass with
  `verify exited N: <last 400 characters of output>`. The worker's own claim does not count.
- Day Desk's retry does not buy more passes. Only the owner's retry does (ADR-0035). A
  WorkItem that used its budget is the owner's to decide.

## Where verify runs

- First in the worker sandbox above, on every pass. A pass that fails verify there returns no
  patch and no PR opens.
- Then in the gate, on GitHub Actions: the repo's `pr-gatekeeper.yml` calls a reusable workflow
  in `Ami-Khokhar/pr-gatekeeper-workflow`, and its `gates` job runs verify on the PR branch with
  no secrets. The local `pr-gatekeeper` checkout declares `runs-on: ubuntu-latest` for its jobs,
  so GitHub-hosted Linux is likely, but the pinned wrapper that a repo calls was not read:
  treat the gate's runner as unknown. A repo can pass the gate only if its verify passes there
  too.
- So a verify that needs macOS fails in both places. `ubuntu-latest` is Linux.

## Day Desk's own sandbox

- The `terminal` tool runs in a Docker container from `tenant/hermes/sandbox/Dockerfile`:
  Debian bookworm Linux with git, gh and python3 and a non-root user (ADR-0021).
- It holds the owner's GitHub token only (`DAYDESK_GITHUB_TOKEN`). It reaches the network
  outbound, since gh and git work there. Nothing in it lasts after the session
  (`container_persistent: false`).
- It is a place to read a repo, not a copy of the worker. You cannot use it to prove that a worker
  could pass verify: it has less installed than the worker image (no node, no ripgrep).
  A command that fails there says nothing about the worker, and a command that passes there may
  still fail in the worker.

## Diagnose before you answer or retry

Read the failure first: the escalation's `reason` and `tried`, which hold the verify output tail.
Then read the repo's verify command: factory_repo_check shows it in the `gate_config` detail
as `verify: <command>`, or `gh repo clone` and read `.github/pr-gatekeeper.json`.

Decide: is the failure a limit of the environment, or a fault in the code?

An environment limit is any of these:

- `command not found` or `No such file` for a tool (xcodegen, xcodebuild, swift, go, cargo,
  java, make, a compiler).
- A platform the worker cannot provide: Xcode, an iOS or Android simulator or emulator,
  macOS-only frameworks and tools, Windows-only tools, a GPU, a display, hardware, a
  `-destination 'platform=iOS Simulator,...'` argument.
- A secret, certificate, account or service the container does not have.
- A step that needs a person (a login, an approval).

Verify is the same command in every pass, so it fails the same way. A retry cannot fix it. When the failure is
an environment limit:

1. Do not call factory_resolve_human with `retry`. A retry spends a pass and cannot work.
2. Tell the owner in plain words: what the verify command needs, what the worker has, why no
   pass can succeed, and which line of `tried` shows it. Name the WorkItem id.
3. Offer the workarounds below that fit, and let the owner pick. Do not start one yourself.

A fault in the code (a test that fails on an assertion, a syntax error, a patch that did not
apply) is not an environment limit: the Drive section's rules apply.

## Workarounds to offer

Say each as a choice with its cost.

- Split the issue so the worker changes only files that a Linux-runnable check covers, and the
  owner checks the rest by hand. Cost: the verify command still must pass in the worker, so this
  helps only when the verify command itself runs on Linux.
- Make the verify run on Linux. Move logic that does not need the platform into a part with
  its own Linux test command, and point `verify` at that command in
  `.github/pr-gatekeeper.json`. Example for an iOS repo: put the testable logic in a Swift
  package whose tests run with `swift test`. Cost: the worker image has no Swift toolchain, so
  the image must gain one first (a change to `worker/Dockerfile`, which is a change to the
  factory). And the gate protects `.github/`, so a person must merge the verify change.
- Run the platform-only verify yourself, on the Mac. The owner runs the command locally, then
  `factory regate` after pushing a fix by hand, or merges the branch. Cost: the factory's
  evidence is the owner's, and the gate's own verify run must still pass.
- Close the issue (`close`). Cost: the factory does not do this work. Best when the whole
  change is platform-only.
- Make no change to the verify. The owner decided in ADR-0044 that the factory gets no macOS
  worker and its verify is not weakened: do not offer to drop or skip the verify command, and
  never suggest a worker that "fakes" a missing tool.

## Check at enable time

factory_repo_check passes when the gate files exist. It does not run verify. After it passes, and
before you approve an issue or start a plan in a repo, read the `verify: <command>` detail.
If the command names a platform the worker lacks (xcodebuild, xcodegen, a simulator
destination, swift, go, cargo, gradle, a GPU tool, a macOS-only tool), warn the owner before the
first approval: say what is missing, say that every pass will fail verify, and offer the
workarounds above. Wait for the owner's choice. A command of `python3`, `pytest`, `node`,
`npm` or `git` is fine.
