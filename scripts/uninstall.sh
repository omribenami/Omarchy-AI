#!/usr/bin/env bash
# Stop and remove the systemd unit. Leaves ~/.config/omarchy-ai/ (your
# config) and the shared ~/.config/omavoice/key (used by both projects) in
# place.
set -euo pipefail

echo "==> Stopping and disabling the service"
systemctl --user disable --now omarchy-ai 2>/dev/null || true

SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
rm -f "$SYSTEMD_USER_DIR/omarchy-ai.service"
systemctl --user daemon-reload

for rc in "$HOME/.bashrc" "$HOME/.zshrc"; do
  if [[ -f "$rc" ]]; then
    sed -i '/^# >>> omarchy-ai terminal context >>>$/,/^# <<< omarchy-ai terminal context <<</d' "$rc"
  fi
done

echo
echo "Left in place, remove by hand if you don't want them:"
echo "  ${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-ai/    (your config)"
echo "  .venv/                                           (Python environment)"
