#!/usr/bin/env bash
# Arch/Omarchy runtime packages; Android build tooling is not needed by users.
set -euo pipefail
if ! command -v pacman >/dev/null; then
  echo 'This installer supports Arch-based Omarchy installations.' >&2
  exit 1
fi
packages=(python uv android-tools gst-plugins-bad gst-plugins-good qrencode
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
