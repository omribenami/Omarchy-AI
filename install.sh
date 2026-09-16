#!/usr/bin/env bash
# Install Omarchy AI from a source checkout or an unpacked release bundle.
set -euo pipefail

project_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$project_dir"

if [[ ! -f pyproject.toml || ! -d scripts ]]; then
  echo "This installer must be run from the Omarchy AI project directory." >&2
  exit 1
fi

echo "==> Installing Omarchy AI from $project_dir"
bash "$project_dir/scripts/setup.sh"

echo
echo "Installation complete. Start the assistant with:"
echo "  systemctl --user enable --now omarchy-ai.service"
