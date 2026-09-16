# Dependencies

These are the native dependencies checked by `scripts/check-dependencies.sh`.
The release bundle contains the Python distributions, receiver APK, wake
models, plugins, and systemd unit. It intentionally does not bundle Arch
packages, PipeWire, GStreamer, or an Android SDK.

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

## Added for direct `wlr-screencopy-unstable-v1` video capture (replaces the portal/PipeWire video path)

| Package | Source | Why |
|---|---|---|
| `pywayland` | `uv add pywayland` (project venv, PyPI) | Low-level Wayland client bindings — talks to `zwlr_screencopy_manager_v1` directly, bypassing `xdg-desktop-portal`'s broken ScreenCast->PipeWire bridging (see STATUS.md/ADR-0001 D5). Not a pacman package. |

**`wlr-protocols` (the pacman package that ships `wlr-screencopy-unstable-v1.xml`)
was NOT installed** — no passwordless sudo in this session
(`sudo -n pacman -Q wlr-protocols` confirmed). Vendored the same XML
content from the upstream `wlr-protocols` GitHub repo into
`scripts/protocols/wlr-screencopy-unstable-v1.xml` instead, and compiled
Python bindings from it once via `python -m pywayland.scanner` into
`scripts/protocols/generated/wlr_screencopy_unstable_v1.py` (checked into
git, not regenerated at install time). **A real installer for another
machine should prefer `sudo pacman -S wlr-protocols` and read the XML from
`/usr/share/wlr-protocols/unstable/wlr-screencopy-unstable-v1.xml`
instead** — the vendored copy here is a workaround for this session's
missing sudo, not the intended long-term approach.
