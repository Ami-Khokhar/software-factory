#!/bin/sh
# Cheap eligibility check; the reflection runner is added separately.
set -eu
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
exec "${FACTORY_BIN:-factory}" call factory_dream_status '{}'
