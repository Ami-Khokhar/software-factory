#!/usr/bin/env bash
# Report each update result once without turning a first failure into a successful cron run.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
state="$HOME/.local/state/factory-mcp"
last="$state/last-update.txt"
failure="$state/last-update-failure.txt"
mkdir -p "$state"

# Hermes's PATH may find another Python without requirements.txt: use the installed interpreter.
if out="$(/usr/bin/python3 "$HOME/.local/bin/factory" update 2>&1)"; then
  rm -f "$failure"
  [ -z "$out" ] && exit 0  # no source or deploy step changed
  [ "$out" = "$(cat "$last" 2>/dev/null)" ] && exit 0
  printf '%s\n' "$out" > "$last"
  printf '%s\n' "$out"
else
  status=$?
  record="$(printf 'exit=%s\n%s' "$status" "$out")"
  [ "$record" = "$(cat "$failure" 2>/dev/null)" ] && exit 0
  printf '%s' "$record" > "$failure"  # keep the full local diagnostic after delivery is deduped
  rm -f "$last"
  if [ -n "$out" ]; then
    printf '%s\n' "$out"
  else
    printf 'factory update failed with exit status %s\n' "$status"
  fi
  exit "$status"
fi
