#!/usr/bin/env bash
# Install a source line in the interactive shells Omarchy uses. The source
# file is part of this checkout/release bundle, so upgrades replace its
# implementation without multiplying rc-file entries.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source_line="[ -r \"$project_dir/scripts/terminal-context.sh\" ] && source \"$project_dir/scripts/terminal-context.sh\""
begin="# >>> omarchy-ai terminal context >>>"
end="# <<< omarchy-ai terminal context <<<"

install_for() {
  local rc="$1"
  touch "$rc"
  if grep -Fqx "$begin" "$rc"; then
    return
  fi
  printf '\n%s\n%s\n%s\n' "$begin" "$source_line" "$end" >>"$rc"
  echo "==> Enabled terminal context in $rc"
}

install_for "$HOME/.bashrc"
install_for "$HOME/.zshrc"
