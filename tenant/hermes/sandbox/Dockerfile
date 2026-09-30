# syntax=docker/dockerfile:1
# Day Desk's shell (ADR-0021): git, gh and python3 for a user with no sudo. Hermes starts one
# container from this image per session and mounts no host directory it can write. Build it with
#   docker build -t day-desk-sandbox:1 tenant/hermes/sandbox

FROM debian:bookworm-slim AS gh
ARG GH_VERSION=2.102.0
ARG TARGETARCH
WORKDIR /download
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && base="https://github.com/cli/cli/releases/download/v${GH_VERSION}" \
 && tarball="gh_${GH_VERSION}_linux_${TARGETARCH}.tar.gz" \
 && curl -fsSLO "$base/$tarball" && curl -fsSLO "$base/gh_${GH_VERSION}_checksums.txt" \
 && grep " $tarball\$" "gh_${GH_VERSION}_checksums.txt" | sha256sum -c - \
 && tar -xzf "$tarball" && mv "gh_${GH_VERSION}_linux_${TARGETARCH}/bin/gh" /download/gh-bin

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git python3 \
 && rm -rf /var/lib/apt/lists/*
COPY --from=gh /download/gh-bin /usr/bin/gh

# Hermes forwards the token as DAYDESK_GITHUB_TOKEN, because it never forwards GH_TOKEN itself.
COPY --chmod=755 <<'EOF' /usr/local/bin/gh
#!/bin/sh
GH_TOKEN="$DAYDESK_GITHUB_TOKEN" exec /usr/bin/gh "$@"
EOF

# git asks gh for the token on every https push and fetch to GitHub. A new repo's first branch is
# main, as in worker/Dockerfile, so a first commit cannot make another branch the default.
COPY <<'EOF' /etc/gitconfig
[credential "https://github.com"]
	helper =
	helper = !/usr/local/bin/gh auth git-credential
[init]
	defaultBranch = main
EOF

# Hermes mounts root-owned tmpfs over /workspace, /home and /root, which this user cannot write, so
# its home is /day-desk in the container's own layer. Clones go when Hermes removes the container.
RUN useradd --home-dir /day-desk --create-home --shell /bin/bash daydesk
USER daydesk
WORKDIR /day-desk
