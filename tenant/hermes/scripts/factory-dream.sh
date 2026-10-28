#!/bin/sh
# No-agent nightly call; the server checks configuration, evidence and cooldown first.
set -eu
exec "${FACTORY_BIN:-factory}" call factory_dream_run '{}'
