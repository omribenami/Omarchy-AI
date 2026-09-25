#!/usr/bin/env bash
# Install/update the desktop UI alongside the Python daemon.
set -euo pipefail
PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export OMARCHY_PATH="${OMARCHY_PATH:-/usr/share/omarchy}"
PLUGIN_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy/plugins"
BACKUP_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy-ai/plugin-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$PLUGIN_DIR" "$BACKUP_DIR"
for source in "$PROJECT_DIR"/quickshell/plugins/*; do
  id="${source##*/}"
  if [[ -d "$PLUGIN_DIR/$id" ]]; then
    cp -a "$PLUGIN_DIR/$id" "$BACKUP_DIR/"
  fi
  mkdir -p "$PLUGIN_DIR/$id"
  cp -a "$source/." "$PLUGIN_DIR/$id/"
  python3 "$PROJECT_DIR/scripts/render-plugin-paths.py" "$PLUGIN_DIR/$id" "$PROJECT_DIR/.venv/bin/omarchy-ai-settings"
done
# The shell watches plugin files, but rescan before enabling new IDs.
omarchy-shell shell rescanPlugins
for id in settings watchdog window-labels myapi tv-discovery quota-alert; do
  omarchy plugin enable "omarchy-ai.$id"
done
# The MyApi widget itself stays hidden until myapi_enabled is true.
omarchy bar move omarchy-ai.settings --section right
omarchy bar move omarchy-ai.myapi --section right --after omarchy-ai.settings
# A rescan refreshes the registry, but an open panel can retain its previous
# QML instance. Restart after all plugin and layout changes during upgrades.
# Omarchy refuses a restart while the session is locked; the rescan above is
# still applied, and the refreshed panel appears when the session unlocks.
if command -v omarchy-hyprland-session-locked >/dev/null 2>&1 && omarchy-hyprland-session-locked; then
  echo 'Omarchy shell restart deferred while the session is locked; plugins were rescanned.'
else
  omarchy restart shell
fi
