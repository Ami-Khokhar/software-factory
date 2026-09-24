# The factory worker sandbox. The harness runs here and sees only the mounted checkout and a copy
# of its model credential: no GitHub credential, no host files. worker/runner.py starts it.
#
#   docker build -t factory-worker:latest worker/
FROM node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 python3-venv ca-certificates ripgrep \
 && rm -rf /var/lib/apt/lists/*

ARG PI_VERSION=0.87.0
RUN npm install -g "@earendil-works/pi-coding-agent@${PI_VERSION}" && npm cache clean --force

# The checkout is owned by the host user, so git must accept it. Commits made here are local only:
# there is no credential helper and no token, and runner.py turns them into a patch.
RUN git config --system safe.directory '*' \
 && git config --system user.name factory-worker \
 && git config --system user.email factory-worker@users.noreply.github.com \
 && git config --system init.defaultBranch main

USER node
ENV HOME=/home/node PI_OFFLINE=1 PI_TELEMETRY=0
WORKDIR /work
