#!/usr/bin/env bash
# Link tenant/factory into an OpenBot checkout and print the .env lines it needs.
set -euo pipefail
openbot="${1:?usage: scripts/install-tenant.sh <path to the OpenBot checkout>}"
here="$(cd "$(dirname "$0")/.." && pwd)"
[ -d "$openbot/examples" ] || { echo "not an OpenBot checkout: $openbot" >&2; exit 1; }
ln -sfn "$here/tenant/factory" "$openbot/examples/factory"
policy="$(python3 -c 'import json, sys; print(json.dumps(json.load(open(sys.argv[1])), separators=(",", ":")))' \
  "$here/tenant/factory/policy.json")"
cat <<ENV
Linked $openbot/examples/factory -> $here/tenant/factory
Set in $openbot/.env, then restart OpenBot:
TENANT_PACKAGE_DIR=../examples/factory
FACTORY_MODEL=<a model your OpenAI-compatible endpoint publishes>
AGENT_COMPUTER_POLICY=$policy
ENV
