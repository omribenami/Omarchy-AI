# Status

## Completed

- Retired `jarvisd` (disabled its systemd service; code left in
  `~/Git/jarvisd` for reference — its Hyprland Lua/classic dispatch fallback
  pattern carries over directly, see ADR-0001 D3).
- Repo scaffolded at `~/Git/oma`: `src/oma/{core,voice,execution,vision,
  devices,display,policy,cli}`, `systemd/`, `android-receiver/`, `docs/`.
- Environment audit complete — see ADR-0001 for the full list. Summary:
  Omarchy 4.0.3 / Hyprland 0.56.2; PipeWire, portal, GStreamer+pipewiresrc,
  ffmpeg+VAAPI H.264, avahi-daemon all present and confirmed working;
  `adb`, `gst-plugins-bad` (webrtcbin), and a Java/Android SDK/Gradle
  toolchain are all *not* installed.
- **Spike 1 (passed):** `hyprctl output create headless` → new 1920x1080
  virtual output appears in `hyprctl monitors` → `hyprctl output remove`
  tears it down cleanly. This is the mechanism `display/` will use.
- **Spike 2 (passed):** `org.freedesktop.portal.ScreenCast` confirmed
  reachable over the user D-Bus session, backed by
  `xdg-desktop-portal-hyprland`. Capture path exists; not yet exercised
  end-to-end (no frame pulled yet — that's the next spike).

## Current blockers

All need the user, not more investigation:

1. Sudo install needed: `sudo pacman -S android-tools gst-plugins-bad`
   (adb + webrtcbin). No passwordless sudo in this session.
2. Decision needed: install a full Android SDK + Gradle + JDK toolchain
   (multi-GB, one-time) to build the Phase 0 "minimal receiver launchable
   via ADB" — not started, waiting for a go-ahead given the size.
3. Live pairing needed: wireless ADB debugging has to be turned on *on the
   TV* and a pairing code read off its screen with the user present — can't
   be scripted ahead of that moment.

## Test results

See ADR-0001 "Confirmed available" and the two passed spikes above. Nothing
failed outright; nothing Android-side has been touched yet (toolchain not
installed).

## Hardware-dependent findings

None yet — no Android hardware has been reachable from this session (no
adb, no IP/pairing exchanged). The Android TV is confirmed to exist per the
user but its make/model, network reachability, and remote-mic capability are
all unknown until the live pairing session.

## Next action

Waiting on the user for blockers 1–3 above. Once `adb`/`gst-plugins-bad` are
installed: a GStreamer spike (`pipewiresrc` → `webrtcbin`, LAN loopback, no
Android yet) to settle ADR-0001's open WebRTC-library question (D7) before
committing to it in the receiver design.
