#!/usr/bin/env bash
# Gathers exactly the evidence needed to tell apart the two different causes
# behind "it says a newer version is available even though it's already
# installed": a stale cached "latest available" notice (check.json) vs. the
# systemd service still pointing at the pre-update install root after a
# self-update that didn't actually take over. Writes one log file instead of
# several commands run one at a time, so it can be read or shared as-is.
#
# Usage: bash scripts/diagnose-update.sh [output-path]
# Read-only: touches nothing but the one log file it writes.
set -uo pipefail

STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy-ai/updates"
OUT="${1:-$HOME/omarchy-update-diagnostics-$(date +%Y%m%d-%H%M%S).log}"

{
  echo "Omarchy AI update diagnostics — $(date -Is)"
  echo "Host: $(hostname)"
  echo

  echo "== systemd unit (omarchy-ai.service) =="
  if unit="$(systemctl --user cat omarchy-ai.service 2>&1)"; then
    grep -iE 'ExecStart|WorkingDirectory' <<<"$unit"
  else
    echo "$unit"
  fi
  echo

  echo "== running process =="
  pid="$(systemctl --user show omarchy-ai.service --property=MainPID --value 2>/dev/null)"
  if [[ -n "$pid" && "$pid" != "0" ]]; then
    ps -p "$pid" -o pid,etime,cmd 2>&1
    echo "cwd: $(readlink -f "/proc/$pid/cwd" 2>&1)"
  else
    echo "not running (MainPID unknown/0)"
  fi
  echo

  workdir="$(systemctl --user show omarchy-ai.service --property=WorkingDirectory --value 2>/dev/null)"
  echo "== installed version per the service's own WorkingDirectory (${workdir:-unknown}) =="
  if [[ -n "$workdir" && -f "$workdir/pyproject.toml" ]]; then
    grep '^version' "$workdir/pyproject.toml"
  else
    echo "pyproject.toml not found at WorkingDirectory -- path above may be stale/wrong"
  fi
  echo

  echo "== $STATE_DIR/check.json (last update-availability check) =="
  cat "$STATE_DIR/check.json" 2>&1
  echo
  echo

  echo "== $STATE_DIR/install.json (last self-update attempt/result) =="
  cat "$STATE_DIR/install.json" 2>&1
  echo
  echo

  echo "== recent update-related journal lines (omarchy-ai + omarchy-ai-update) =="
  journalctl --user -u omarchy-ai.service -u omarchy-ai-update.service --no-pager -n 300 2>&1 \
    | grep -iE 'update|version|install' || echo "(none found in the last 300 lines)"
} >"$OUT" 2>&1

echo "Wrote $OUT"
