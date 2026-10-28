#!/bin/sh
# Cheap eligibility check; the reflection runner is added separately.
set -eu
exec "${FACTORY_BIN:-factory}" call factory_dream_status '{}'
