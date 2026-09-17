#!/usr/bin/env bash
# Verify the native components that cannot live inside a Python wheel.
set -euo pipefail

missing=()
for command in adb gst-inspect-1.0 pw-record pw-play pactl systemctl omarchy-shell; do
  command -v "$command" >/dev/null 2>&1 || missing+=("$command")
done

if [[ ! -f /usr/lib/spa-0.2/aec/libspa-aec-webrtc.so ]]; then
  missing+=("PipeWire WebRTC echo/noise cancellation (pipewire-audio)")
fi

if ! gst-inspect-1.0 webrtcbin >/dev/null 2>&1; then
  missing+=("GStreamer webrtcbin (gst-plugins-bad)")
fi

if ! /usr/bin/python3 -c 'import gi' >/dev/null 2>&1; then
  missing+=("python-gobject")
fi

if ((${#missing[@]})); then
  printf 'Missing Omarchy AI runtime dependencies:\n' >&2
  printf '  - %s\n' "${missing[@]}" >&2
  printf '\nOn Omarchy install the documented packages, then retry:\n' >&2
  printf '  sudo pacman -S --needed android-tools gst-plugins-bad gst-plugins-good qrencode python-gobject pipewire-audio pipewire-pulse libpulse\n' >&2
  printf '  yay -S android-sdk-cmdline-tools-latest  # only required to rebuild the Android receiver\n' >&2
  exit 1
fi

echo 'Native Omarchy AI dependencies are available.'
