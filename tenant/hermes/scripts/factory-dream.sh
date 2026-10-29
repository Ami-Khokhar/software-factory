#!/bin/sh
# No-agent nightly call; the server checks configuration, evidence and cooldown first.
set -eu
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "${FACTORY_BIN:-factory}" call factory_dream_run '{}'
