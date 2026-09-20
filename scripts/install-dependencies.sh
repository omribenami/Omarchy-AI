#!/usr/bin/env bash
# Arch/Omarchy runtime packages; Android build tooling is not needed by users.
set -euo pipefail
if ! command -v pacman >/dev/null; then
  echo 'This installer supports Arch-based Omarchy installations.' >&2
  exit 1
fi
# uv is deliberately not listed here: it's commonly installed via astral's
# own installer (https://astral.sh/uv), not pacman, and pacman -Q can't see
# that install -- checking it here would demand sudo for an already-present,
# fully working tool. setup.sh has its own command -v-based check that
# installs it via astral's script only if it's genuinely absent.
packages=(python android-tools gst-plugins-bad gst-plugins-good qrencode
  python-gobject pipewire-audio pipewire-pulse libpulse)
missing=()
for package in "${packages[@]}"; do
  pacman -Q "$package" >/dev/null 2>&1 || missing+=("$package")
done
if ((${#missing[@]})); then
  printf 'Installing missing runtime packages (sudo may ask for your password):\n'
  printf '  %s\n' "${missing[@]}"
  sudo pacman -S --needed "${missing[@]}"
fi
