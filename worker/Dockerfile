# The factory worker sandbox. The harness runs here and sees only the mounted checkout and a copy
# of its model credentials: no GitHub credential, no host files. worker/runner.py starts it.
#
#   docker build -t factory-worker:latest worker/
FROM node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 python3-pip python3-venv ca-certificates ripgrep curl procps lsof \
 && rm -rf /var/lib/apt/lists/*

# The harnesses a relay agent may run here (#98). Each agent's args name its command, credentials
# and env; worker/runner.py knows none of them.
ARG PI_VERSION=0.87.1
ARG CODEX_VERSION=0.159.0
ARG OPENCODE_VERSION=1.18.32
RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" "@openai/codex@${CODEX_VERSION}" \
      "opencode-ai@${OPENCODE_VERSION}" \
 && pi --version && codex --version && opencode --version \
 && npm cache clean --force
# The scratch HOME and its XDG dirs exist and are writable by any UID, so a credential mount such as
# /tmp/factory-home/.local/share/opencode leaves no root-owned parent where a harness writes state.
RUN mkdir -p /tmp/factory-home/.config /tmp/factory-home/.cache /tmp/factory-home/.local/share \
      /tmp/factory-home/.local/state \
 && chmod 1777 /tmp/factory-home /tmp/factory-home/.config /tmp/factory-home/.cache /tmp/factory-home/.local \
      /tmp/factory-home/.local/share /tmp/factory-home/.local/state

# The checkout is owned by the host user, so git must accept it. Commits made here are local only:
# there is no credential helper and no token, and runner.py turns them into a patch.
RUN git config --system safe.directory '*' \
 && git config --system user.name factory-worker \
 && git config --system user.email factory-worker@users.noreply.github.com \
 && git config --system init.defaultBranch main

USER node
# The container is thrown away after each step, so a verify command may pip-install freely. No
# bytecode, because `git add -A` would put the __pycache__ of a test run into the patch.
ENV HOME=/home/node PI_OFFLINE=1 PI_TELEMETRY=0 PIP_BREAK_SYSTEM_PACKAGES=1 PIP_USER=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /work
