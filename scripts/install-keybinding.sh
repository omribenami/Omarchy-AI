#!/usr/bin/env bash
# Install an idempotent Hyprland binding for activating the assistant.
set -euo pipefail

BINDINGS_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/hypr/bindings.lua"
ACTIVATE_CMD="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/.venv/bin/omarchy-ai-settings"
printf -v ACTIVATE_SHELL '%q' "$ACTIVATE_CMD"
START_MARKER="-- >>> Omarchy AI assistant toggle >>>"
END_MARKER="-- <<< Omarchy AI assistant toggle <<<"

mkdir -p "$(dirname "$BINDINGS_FILE")"
touch "$BINDINGS_FILE"
temporary="$(mktemp "${BINDINGS_FILE}.XXXXXX")"
trap 'rm -f "$temporary"' EXIT

sed "/^${START_MARKER}$/,/^${END_MARKER}$/d" "$BINDINGS_FILE" >"$temporary"
cat >>"$temporary" <<'EOF'

-- >>> Omarchy AI assistant toggle >>>
-- GRAVE is the ` key; SHIFT + GRAVE is ~. Support both forms.
hl.unbind("SUPER + GRAVE")
hl.unbind("SUPER + SHIFT + GRAVE")
o.bind("SUPER + GRAVE", "Activate Omarchy AI", "@ACTIVATE_CMD@ activate")
o.bind("SUPER + SHIFT + GRAVE", "Activate Omarchy AI", "@ACTIVATE_CMD@ activate")
-- <<< Omarchy AI assistant toggle <<<
EOF
sed -i "s|@ACTIVATE_CMD@|${ACTIVATE_SHELL//&/\\&}|g" "$temporary"
mv "$temporary" "$BINDINGS_FILE"
trap - EXIT

echo "==> Installed SUPER + grave to activate Omarchy AI"
