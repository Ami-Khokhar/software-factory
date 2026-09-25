#!/usr/bin/env bash
# plan.md §9: offer factory-mcp tools to Day Desk (read) and factory-ops (repo + start), and let
# Day Desk hand off to factory-ops. Idempotent on OpenBot's side. Then set AGENT_COMPUTER_POLICY
# from tenant/factory/policy.json (scripts/install-tenant.sh prints it) and restart OpenBot.
set -euo pipefail
openbot="${OPENBOT_URL:-http://127.0.0.1:3001}"
grant() {  # kind ref agentId
  curl -fsS -X POST "$openbot/api/plugins/grants" -H 'content-type: application/json' \
    -d "{\"kind\":\"$1\",\"ref\":\"$2\",\"agentId\":\"$3\"}" >/dev/null && echo "granted $1 $2 -> $3"
}
for tool in factory_status factory_repos factory_repo_check factory_repo_status; do
  grant mcp "factory-mcp/$tool" day-desk
done
for tool in factory_status factory_repos factory_repo_check factory_repo_status \
            factory_add_repo factory_approve_issue; do
  grant mcp "factory-mcp/$tool" factory-ops
done
grant bot factory-ops day-desk     # handoff: Day Desk -> Factory Ops
grant bot factory-planner day-desk # handoff: Day Desk -> Planner (goal -> issues)
echo "Check: curl -s $openbot/api/plugins/for/day-desk | python3 -m json.tool"
