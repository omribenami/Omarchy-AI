# Dependencies (running list, for the eventual installer)

Kept as we go, per-package, so `scripts/setup.sh` can be written once instead
of reconstructed from memory at the end. Not all of these need install
commands in the script — several are already standard on Omarchy — but every
one that required a manual step during development is listed so nothing gets
missed.

## Installed manually during Phase 0 / Phase 1 spikes

| Package | Source | Why |
|---|---|---|
| `android-tools` | pacman (`extra`) | `adb` |
| `gst-plugins-bad` | pacman (`extra`) | `webrtcbin` |
| `gst-plugins-good` | pacman (`extra`) | RTP payloaders (`rtpopuspay` etc.) — needed once we discovered `gpt-live-1` is WebRTC-only |
| `gradle` | pacman (`extra`) | build the Android receiver |
| `android-sdk-cmdline-tools-latest` | AUR (`yay`) | Android SDK's `android` CLI (`sdkmanager`'s successor); installs to `/opt/android-sdk` (root-owned) |

Then, via the `android` CLI itself (not pacman), into `~/Android/Sdk`
(user-writable — see ADR-0001 D-notes for why):
`platform-tools`, `platforms/android-37.1`, `platforms/android-36`,
`build-tools/37.0.0`.

JDK 21 (Temurin) via `mise`, pinned in `mise.toml` — no sudo needed, already
reproducible.

## Already present on this Omarchy install, not installed by us

Confirmed present and used; the installer should check-and-skip these on
another Omarchy machine, not assume identical to this one:

- `gstreamer` (1.28.6) + `pipewiresrc` (part of the core GStreamer PipeWire
  plugin)
- `libnice` — ICE agent, `webrtcbin` needs it at runtime
- `ffmpeg` with VAAPI encoders (`h264_vaapi` etc.)
- `xdg-desktop-portal` + `xdg-desktop-portal-hyprland` — ScreenCast portal
- `avahi-daemon` — mDNS/DNS-SD
- `python-gobject` (`gi`) — PyGObject; needed for the `webrtcbin`/GStreamer
  Python bindings used in `scripts/spike_live_webrtc.py` and (once it
  exists) `src/omarchy_ai/voice`. **Not pip-installable in the normal sense** — it's
  a system package tied to the installed GObject-introspection typelibs, so
  Omarchy AI's own venv must be created with `--system-site-packages` against the
  *system* Python (not a `uv`-downloaded one) — see the `.python-version`
  note below.
- `mise` — used for the JDK; assumed already on the machine like it was
  here, not something this project should install on someone's behalf.

## Non-obvious setup gotchas to carry into `setup.sh`

- **`.python-version` must pin the system Python** (`3.14` here, whatever
  `pacman -Q python` reports on the target machine), not left at whatever
  `uv init` wrote by default — otherwise `uv run` silently rebuilds an
  isolated venv that can't see `python-gobject`, and every `gi` import fails
  with no obvious reason why.
- The venv itself needs `uv venv --system-site-packages --python
  /usr/bin/python3` — the plain `uv venv`/`uv sync` default does not carry
  `--system-site-packages` and there's no pyproject.toml flag for it, it has
  to be recreated with the explicit flag.
- `ANDROID_SDK_ROOT`/`ANDROID_HOME` must point at a user-writable directory
  (`~/Android/Sdk`), not the AUR package's own root-owned
  `/opt/android-sdk` — `android sdk install` fails with a permissions error
  otherwise. Set in `mise.toml`.
