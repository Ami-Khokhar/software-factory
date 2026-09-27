# The factory worker sandbox. The harness runs here and sees only the mounted checkout and a copy
# of its model credential: no GitHub credential, no host files. worker/runner.py starts it.
#
#   docker build -t factory-worker:latest worker/
FROM node:24-bookworm-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends git python3 python3-pip python3-venv ca-certificates ripgrep curl procps lsof \
 && rm -rf /var/lib/apt/lists/*

ARG FREEBUFF_VERSION=0.0.203
RUN npm install -g "freebuff@${FREEBUFF_VERSION}" \
 && freebuff --version \
 && npm cache clean --force

COPY freebuff.py /usr/local/bin/factory-freebuff
RUN chmod 755 /usr/local/bin/factory-freebuff \
 && mkdir -p /tmp/factory-home/.config /home/node/.config/manicode \
 && ln -s /home/node/.config/manicode /tmp/factory-home/.config/manicode \
 && chown -R node:node /home/node/.config \
 && chmod 1777 /tmp/factory-home

# The checkout is owned by the host user, so git must accept it. Commits made here are local only:
# there is no credential helper and no token, and runner.py turns them into a patch.
RUN git config --system safe.directory '*' \
 && git config --system user.name factory-worker \
 && git config --system user.email factory-worker@users.noreply.github.com \
 && git config --system init.defaultBranch main

USER node
# The container is thrown away after each step, so a verify command may pip-install freely.
ENV HOME=/home/node PIP_BREAK_SYSTEM_PACKAGES=1 PIP_USER=1
WORKDIR /work
