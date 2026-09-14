#!/usr/bin/env bash
# Install Omarchy AI: sync the Python environment and install the systemd
# user unit. Does not touch package installs (adb, gst-plugins-*, gradle,
# android-sdk-cmdline-tools-latest) — those need sudo/yay and are listed in
# docs/DEPENDENCIES.md; run them by hand first if not already done.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (https://astral.sh/uv) — installing to ~/.local/bin" >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> Syncing Python environment (system-site-packages, for python-gobject/GStreamer)"
if [[ ! -d .venv ]]; then
  uv venv --system-site-packages --python /usr/bin/python3
fi
uv sync

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-ai"
mkdir -p "$CONFIG_DIR"
if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  cat >"$CONFIG_DIR/config.yaml" <<'EOF'
# Overrides for src/omarchy_ai/config.py's defaults — only list what you
# want to change, it's merged over the defaults.
EOF
  echo "==> Wrote $CONFIG_DIR/config.yaml"
else
  echo "==> $CONFIG_DIR/config.yaml already exists, leaving it alone"
fi

if [[ ! -f ~/.config/omavoice/key ]]; then
  echo "==> WARNING: no OpenAI API key found at ~/.config/omavoice/key"
  echo "    Omarchy AI needs a key with gpt-live-1 access. Either paste one"
  echo "    there (mode 600, key only, no other content) or set"
  echo "    api_key_path in $CONFIG_DIR/config.yaml to point somewhere else."
fi

echo "==> Installing the systemd user unit"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
sed \
  -e "s|@VENV@|$PROJECT_DIR/.venv|g" \
  -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
  -e "s|@PATH@|$PATH|g" \
  systemd/omarchy-ai.service >"$SYSTEMD_USER_DIR/omarchy-ai.service"

systemctl --user daemon-reload

echo
echo "Setup complete. Next steps:"
echo "  systemctl --user enable --now omarchy-ai"
echo "  journalctl --user -u omarchy-ai -f"
echo
echo "Say 'hey jarvis' (placeholder wake word — see STATUS.md) to talk."
echo "End a conversation by saying goodbye/stop/that's all."
