#!/usr/bin/env bash
# Install the factory's two launchd services (macOS) and the `factory` command. Both restart when
# they exit and start at login.
#
#   com.agent-relay                   agent-relay from ~/.local/share/agent-relay, with the 4 MB output
#                                     limit the worker Result needs (HANDOFF §14.6). agent-relay's own
#                                     scripts/install-service.sh writes the same label without it.
#   com.software-factory.factory-mcp  `scripts/factory serve` from this checkout
#
# Run `factory stop` (or stop any hand-started relay and factory-mcp) first, then this, then
# `factory start`. Uninstall: scripts/install-services.sh --uninstall
set -euo pipefail
here="$(cd "$(dirname "$0")/.." && pwd)"
agents="$HOME/Library/LaunchAgents"
relay_dir="${AGENT_RELAY_DIR:-$HOME/.local/share/agent-relay}"
python="$(command -v python3)"
path="$HOME/.local/bin:$HOME/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
uid="$(id -u)"

if [ "${1:-}" = "--uninstall" ]; then
  for label in com.software-factory.factory-mcp com.agent-relay; do
    launchctl bootout "gui/$uid/$label" 2>/dev/null || true
    rm -f "$agents/$label.plist"
  done
  rm -f "$HOME/.local/bin/factory"
  echo "removed both services and ~/.local/bin/factory"
  exit 0
fi

plist() {  # plist <label> <log> <working dir> <extra env xml> <program args...>
  local label="$1" log="$2" dir="$3" env="$4"; shift 4
  mkdir -p "$agents" "$(dirname "$log")"
  {
    printf '<?xml version="1.0" encoding="UTF-8"?>\n'
    printf '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
    printf '<plist version="1.0">\n<dict>\n  <key>Label</key><string>%s</string>\n' "$label"
    printf '  <key>ProgramArguments</key>\n  <array>\n'
    for arg in "$@"; do printf '    <string>%s</string>\n' "$arg"; done
    printf '  </array>\n  <key>WorkingDirectory</key><string>%s</string>\n' "$dir"
    printf '  <key>EnvironmentVariables</key>\n  <dict>\n    <key>PATH</key><string>%s</string>\n' "$path"
    printf '%b' "$env"
    printf '  </dict>\n'
    printf '  <key>RunAtLoad</key><true/>\n  <key>KeepAlive</key><true/>\n  <key>ThrottleInterval</key><integer>30</integer>\n'
    printf '  <key>StandardOutPath</key><string>%s</string>\n  <key>StandardErrorPath</key><string>%s</string>\n' "$log" "$log"
    printf '</dict>\n</plist>\n'
  } > "$agents/$label.plist"
  plutil -lint -s "$agents/$label.plist"
  launchctl bootout "gui/$uid/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$uid" "$agents/$label.plist"
  echo "installed $label (log: $log)"
}

[ -f "$relay_dir/src/server.py" ] || { echo "no agent-relay checkout at $relay_dir" >&2; exit 1; }
plist com.agent-relay "$HOME/.local/state/agent-relay/relay.log" "$relay_dir" \
  "    <key>AGENT_RELAY_MAX_OUTPUT_BYTES</key><string>4194304</string>\n" \
  "$python" "$relay_dir/src/server.py"
plist com.software-factory.factory-mcp "$HOME/.local/state/factory-mcp/server.log" "$here" "" \
  "$python" "$here/scripts/factory" serve

mkdir -p "$HOME/.local/bin"
ln -sfn "$here/scripts/factory" "$HOME/.local/bin/factory"
echo "linked ~/.local/bin/factory -> $here/scripts/factory"
