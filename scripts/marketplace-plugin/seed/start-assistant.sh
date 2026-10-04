#!/usr/bin/bash
# Install and start the assistant when this plugin is enabled.
#
# `omarchy plugin add` does not run plugin code. Enabling the plugin loads
# AssistantService.qml, which runs this script. The settings panel keeps
# resolving omarchy-ai-settings at runtime; nothing writes a path into
# Panel.qml.
#
# The archive is the published Omarchy-AI release named below. Its sha256
# is pinned in this file. The bytes are saved, checked, and extracted.
# They are never piped into a shell. Native packages, when missing, are
# installed by that release's own dependency script. Python packages are
# synced with uv from the extracted tree. This script does not download
# an installer and execute it.
#
# OMARCHY_AI_ASSISTANT_ARCHIVE and OMARCHY_AI_ASSISTANT_SHA256 override the
# pin for tests. Production uses the defaults below.
set -euo pipefail

PINNED_VERSION="${OMARCHY_AI_ASSISTANT_VERSION:-0.13.2}"
PINNED_SHA256="${OMARCHY_AI_ASSISTANT_SHA256:-e41d406b0581e48623b27f1f7c442224522750016c881222ea71d0fa6de6fc97}"
PINNED_URL="${OMARCHY_AI_ASSISTANT_URL:-https://github.com/omribenami/Omarchy-AI/releases/download/v${PINNED_VERSION}/omarchy-ai-${PINNED_VERSION}-linux-x86_64.tar.gz}"

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
stage=""
cleanup() {
  if [[ -n "${stage}" && -d "${stage}" ]]; then
    rm -rf "${stage}"
  fi
}
trap cleanup EXIT

config_home() {
  if [[ -n "${XDG_CONFIG_HOME:-}" ]]; then
    printf '%s\n' "$XDG_CONFIG_HOME"
    return 0
  fi
  printf '%s\n' "$HOME/.config"
}

data_home() {
  if [[ -n "${XDG_DATA_HOME:-}" ]]; then
    printf '%s\n' "$XDG_DATA_HOME"
    return 0
  fi
  printf '%s\n' "$HOME/.local/share"
}

state_home() {
  if [[ -n "${XDG_STATE_HOME:-}" ]]; then
    printf '%s\n' "$XDG_STATE_HOME/omarchy-ai"
    return 0
  fi
  printf '%s\n' "$HOME/.local/state/omarchy-ai"
}

runtime_dir() {
  if [[ -n "${XDG_RUNTIME_DIR:-}" && -d "${XDG_RUNTIME_DIR}" && -w "${XDG_RUNTIME_DIR}" ]]; then
    printf '%s\n' "$XDG_RUNTIME_DIR"
    return 0
  fi
  state_home
}

emit() {
  local started="$1" reason="$2"
  printf '{"ok":true,"started":%s,"reason":"%s"}\n' "$started" "$reason"
  printf '{"ok":true,"started":%s,"reason":"%s"}\n' "$started" "$reason" >>"$log"
}

fail() {
  local message="$1"
  printf '%s\n' "$message" >&2
  printf '%s\n' "$message" >>"$log"
  printf '{"ok":false,"started":false,"error":"%s"}\n' "$message"
  exit 1
}

run_logged() {
  "$@" >>"$log" 2>&1
}

project_root_from_cli() {
  local cli="$1"
  case "$cli" in
    */.venv/bin/omarchy-ai-settings)
      printf '%s\n' "${cli%/.venv/bin/omarchy-ai-settings}"
      ;;
    *)
      return 1
      ;;
  esac
}

unit_points_at() {
  local root="$1" unit line workdir
  unit="$(config_home)/systemd/user/omarchy-ai.service"
  [[ -f "$unit" ]] || return 1
  line="$(/usr/bin/grep -m1 '^WorkingDirectory=' "$unit" || true)"
  workdir="${line#WorkingDirectory=}"
  workdir="${workdir%$'\r'}"
  [[ "$workdir" == "$root" ]]
}

write_unit() {
  local root="$1" dest template
  dest="$(config_home)/systemd/user/omarchy-ai.service"
  mkdir -p "$(dirname "$dest")"
  template="$root/systemd/omarchy-ai.service"
  if [[ -f "$template" ]]; then
    sed \
      -e "s|@VENV@|$root/.venv|g" \
      -e "s|@PROJECT_DIR@|$root|g" \
      -e "s|@PATH@|$PATH|g" \
      -e "s|@OMARCHY_PATH@|${OMARCHY_PATH:-/usr/share/omarchy}|g" \
      "$template" >"$dest"
    return 0
  fi
  cat >"$dest" <<EOF
[Unit]
Description=Omarchy AI voice assistant daemon
After=pipewire.service
Wants=pipewire.service

[Service]
Type=simple
ExecStart=$root/.venv/bin/python -m omarchy_ai
WorkingDirectory=$root
Restart=on-failure
RestartSec=2
Environment=PATH=$PATH
Environment=OMARCHY_PATH=${OMARCHY_PATH:-/usr/share/omarchy}

[Install]
WantedBy=default.target
EOF
}

rewrite_helper_token() {
  local dest="$1" cli="$2"
  python3 - "$dest" "$cli" <<'PY'
import json
import sys
from pathlib import Path

directory, executable = sys.argv[1:]
replacement = json.dumps(executable)[1:-1]
marker = "@" + "OMARCHY_AI_SETTINGS" + "@"
for path in Path(directory).rglob("*.qml"):
    text = path.read_text()
    if marker in text:
        path.write_text(text.replace(marker, replacement))
PY
}

copy_companion_plugins() {
  local root="$1" cli="$2" src dest id plugin_root
  [[ -d "$root/quickshell/plugins" ]] || return 0
  plugin_root="$(config_home)/omarchy/plugins"
  mkdir -p "$plugin_root"
  shopt -s nullglob
  for src in "$root"/quickshell/plugins/*; do
    [[ -d "$src" ]] || continue
    id="$(basename "$src")"
    # The marketplace checkout of this plugin is already the enabled copy.
    [[ "$id" == "omarchy-ai.settings" ]] && continue
    dest="$plugin_root/$id"
    if [[ -d "$dest" ]]; then
      continue
    fi
    mkdir -p "$dest"
    cp -a "$src/." "$dest/"
    rewrite_helper_token "$dest" "$cli"
  done
  shopt -u nullglob
  if command -v omarchy-shell >/dev/null 2>&1; then
    omarchy-shell shell rescanPlugins >>"$log" 2>&1 || true
  fi
  if command -v omarchy >/dev/null 2>&1; then
    shopt -s nullglob
    for src in "$root"/quickshell/plugins/*; do
      id="$(basename "$src")"
      [[ "$id" == "omarchy-ai.settings" ]] && continue
      [[ -d "$plugin_root/$id" ]] || continue
      omarchy plugin enable "$id" >>"$log" 2>&1 || true
    done
    shopt -u nullglob
  fi
}

prepare_runtime_files() {
  local root="$1" config models
  config="$(config_home)/omarchy-ai"
  mkdir -p "$config"
  if [[ ! -f "$config/config.yaml" ]]; then
    cat >"$config/config.yaml" <<'EOF'
# Overrides for Omarchy AI defaults. Only list what you want to change.
EOF
  fi
  if [[ -d "$root/wake_models" ]]; then
    models="$config/wake_models"
    mkdir -p "$models"
    shopt -s nullglob
    local model
    for model in "$root"/wake_models/*.onnx; do
      cp -n "$model" "$models/" || true
    done
    shopt -u nullglob
  fi
}

sync_environment() {
  local root="$1"
  local cli="$root/.venv/bin/omarchy-ai-settings"
  if [[ -x "$cli" ]]; then
    resolved_cli="$cli"
    return 0
  fi
  if [[ -f "$root/scripts/install-dependencies.sh" ]]; then
    run_logged bash "$root/scripts/install-dependencies.sh"
  fi
  export PATH="${HOME:-}/.local/bin:${PATH}"
  if ! command -v uv >/dev/null 2>&1; then
    fail "uv is required to create the assistant environment"
  fi
  if [[ ! -d "$root/.venv" ]]; then
    run_logged bash -c 'cd "$1" && uv venv --system-site-packages --python /usr/bin/python3' bash "$root"
  fi
  run_logged bash -c 'cd "$1" && uv sync --locked' bash "$root"
  if [[ ! -x "$cli" ]]; then
    fail "the assistant environment has no omarchy-ai-settings command"
  fi
  resolved_cli="$cli"
}

verify_sha256() {
  local file="$1" digest
  digest="$(/usr/bin/sha256sum "$file" | /usr/bin/awk 'NR==1 { print $1 }')"
  if [[ "$digest" != "$PINNED_SHA256" ]]; then
    fail "release checksum does not match the pinned assistant"
  fi
}

install_release() {
  local parent root archive extracted cli
  parent="$(data_home)/omachy-ai-releases"
  root="$parent/omarchy-ai-${PINNED_VERSION}-linux-x86_64"
  if [[ -x "$root/.venv/bin/omarchy-ai-settings" && -f "$root/pyproject.toml" ]]; then
    resolved_cli="$root/.venv/bin/omarchy-ai-settings"
    return 0
  fi
  stage="$(mktemp -d)"
  archive="$stage/payload"
  if [[ -n "${OMARCHY_AI_ASSISTANT_ARCHIVE:-}" ]]; then
    [[ -f "$OMARCHY_AI_ASSISTANT_ARCHIVE" ]] || fail "assistant archive is missing"
    cp -f "$OMARCHY_AI_ASSISTANT_ARCHIVE" "$archive"
  else
    run_logged /usr/bin/curl -fL --retry 3 --retry-delay 1 -o "$archive" "$PINNED_URL"
  fi
  verify_sha256 "$archive"
  run_logged tar -xzf "$archive" -C "$stage"
  extracted="$stage/omarchy-ai-${PINNED_VERSION}-linux-x86_64"
  if [[ ! -f "$extracted/pyproject.toml" ]]; then
    fail "release archive does not contain the assistant tree"
  fi
  mkdir -p "$parent"
  rm -rf "$root"
  mv "$extracted" "$root"
  rm -rf "$stage"
  stage=""
  sync_environment "$root"
  prepare_runtime_files "$root"
  copy_companion_plugins "$root" "$resolved_cli"
}

start_service() {
  local cli="$1" root
  if ! root="$(project_root_from_cli "$cli")"; then
    fail "omarchy-ai-settings is not inside an assistant environment"
  fi
  if ! unit_points_at "$root"; then
    write_unit "$root"
  fi
  run_logged systemctl --user daemon-reload
  run_logged systemctl --user enable --now omarchy-ai.service
}

log="$(state_home)/start-assistant.log"
mkdir -p "$(dirname "$log")"
lock="$(runtime_dir)/omarchy-ai-start.lock"
mkdir -p "$(dirname "$lock")"
exec 9>"$lock"
if ! /usr/bin/flock -n 9; then
  emit false "already-starting"
  exit 0
fi

printf '%s\n' "==> $(date -u +%Y-%m-%dT%H:%M:%SZ) start-assistant ${PINNED_VERSION}" >>"$log"

resolved_cli=""
if resolved_cli="$("$script_dir/resolve-settings.sh" --print-path)"; then
  if systemctl --user is-active --quiet omarchy-ai.service; then
    emit false "already-running"
    exit 0
  fi
  start_service "$resolved_cli"
  emit true "started"
  exit 0
fi

install_release
start_service "$resolved_cli"
emit true "installed"
