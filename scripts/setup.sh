#!/usr/bin/env bash
# Install Omarchy AI: sync the Python environment and install the systemd
# user unit. Does not touch package installs (adb, gst-plugins-*, gradle,
# android-sdk-cmdline-tools-latest) — those need sudo/yay and are listed in
# docs/DEPENDENCIES.md; run them by hand first if not already done.
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_DIR"

bash "$PROJECT_DIR/scripts/check-dependencies.sh"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is required (https://astral.sh/uv) — installing to ~/.local/bin" >&2
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi

echo "==> Syncing Python environment (system-site-packages, for python-gobject/GStreamer)"
if [[ ! -d .venv ]]; then
  uv venv --system-site-packages --python /usr/bin/python3
fi
uv sync --locked
if compgen -G "$PROJECT_DIR/python/jev_ultrafast-*.whl" >/dev/null; then
  uv pip install --python "$PROJECT_DIR/.venv/bin/python" --no-deps \
    "$PROJECT_DIR"/python/jev_ultrafast-*.whl
fi
"$PROJECT_DIR/.venv/bin/python" -c 'from importlib.metadata import version; from jev_ultrafast import Agent; assert version("jev-ultrafast")' \
  || { echo 'Jev Ultrafast browser dependency failed to install.' >&2; exit 1; }
echo "==> Verified Jev Ultrafast browser agent"

CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/omarchy-ai"
mkdir -p "$CONFIG_DIR"

WAKE_MODELS_DIR="$CONFIG_DIR/wake_models"
mkdir -p "$WAKE_MODELS_DIR"
echo "==> Installing trained wake-word models (omachy/omri/roni)"
cp -n wake_models/*.onnx "$WAKE_MODELS_DIR/" 2>/dev/null || true

if [[ ! -f "$CONFIG_DIR/config.yaml" ]]; then
  cat >"$CONFIG_DIR/config.yaml" <<'EOF'
# Overrides for src/omarchy_ai/config.py's defaults — only list what you
# want to change, it's merged over the defaults.
EOF
  echo "==> Wrote $CONFIG_DIR/config.yaml"
else
  echo "==> $CONFIG_DIR/config.yaml already exists, leaving it alone"
fi

echo "==> In Assistant Settings, choose a conversation model and save its API key."
echo "    For Jev desktop/browser actions, also save a Vercel AI Gateway key."
echo "    Existing configuration and key files are preserved. Apply saved changes to restart."

echo "==> Installing the systemd user unit"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"
sed \
  -e "s|@VENV@|$PROJECT_DIR/.venv|g" \
  -e "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
  -e "s|@PATH@|$PATH|g" \
  -e "s|@OMARCHY_PATH@|${OMARCHY_PATH:-/usr/share/omarchy}|g" \
  systemd/omarchy-ai.service >"$SYSTEMD_USER_DIR/omarchy-ai.service"

systemctl --user daemon-reload

bash "$PROJECT_DIR/scripts/install-plugins.sh"
bash "$PROJECT_DIR/scripts/install-terminal-context.sh"

echo
echo "Setup complete. Next steps:"
echo "  systemctl --user enable --now omarchy-ai"
echo "  journalctl --user -u omarchy-ai -f"
echo
echo "Say 'omachy' to talk. Additional bundled wake words can be enabled in Settings."
echo "End a conversation by saying goodbye/stop/that's all."
echo
echo "Optional: connect Gmail/Calendar/Drive/Notion/Slack and more via MyApi"
echo "(myapiai.com) from the Omarchy AI settings panel's \"Connect services"
echo "to Omarchy AI\" section, then watch live usage with:"
echo "  omarchy-ai-dashboard"
