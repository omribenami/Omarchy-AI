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

1. **Waiting on the user to run one command** (needs sudo, which this
   session doesn't have passwordless):
   ```
   sudo pacman -S --needed android-tools gst-plugins-bad gradle && yay -S android-sdk-cmdline-tools-latest
   ```
   Covers adb, webrtcbin, Gradle (all official `extra` repo — no manual
   downloads), and the Android SDK cmdline-tools (AUR, `yay`).
2. Live pairing needed: wireless ADB debugging has to be turned on *on the
   TV* and a pairing code read off its screen with the user present — can't
   be scripted ahead of that moment.

**JDK: done, no sudo needed.** Temurin 21.0.12 installed via `mise` (already
in use on this machine), pinned in `mise.toml` (committed) so `oma`'s own
setup script can reproduce it the same way — same pattern jarvisd used for
its Python venv.

**Lesson logged:** a WebFetch summary of an Android developer docs page
fabricated a plausible-looking but nonexistent download URL (404'd on
fetch — checked before using it further). Switched to Arch's own package
repos/AUR instead of hand-fetching Google's CDN, which is more reliable
here anyway. Worth remembering for any future "get me the download link"
step in this project: verify before treating a fetched URL as real,
especially from JS-rendered doc sites.

## Test results

See ADR-0001 "Confirmed available" and the two passed spikes above. Nothing
failed outright; nothing Android-side has been touched yet (toolchain not
installed).

## Hardware-dependent findings

**Paired: the user's Android TV, 192.168.1.86:5555.** ADB-over-network
authorized (the TV's own confirmation dialog was dismissed by accident, but
a disconnect/reconnect re-triggered it and it came back authorized —
network debugging on this device does not require the pairing-code dance
some newer Android versions use, just the one-time on-screen allow).

Pulled via `adb shell getprop`/`pm list packages`:

- **Model `HY300Pro`** (board `exdroid`) — an Allwinner-chipset Android
  projector running a community/enthusiast Android TV build, *not* a
  mainstream Google-certified box (no NVIDIA Shield/Chromecast-with-Google-TV
  pedigree). Relevant because Cast/CEC support on this class of device is
  less standardized than on certified hardware — treat as untested until
  checked directly, not assumed present.
- **Android 11 / API 30.** Confirms `minSdk 26` on the receiver template
  (already set) is the right call, not higher.
- **Display 1280x720 @ density 240** — a 720p device. The spec's "1080p at
  30fps minimum" target doesn't apply to *this* unit; encoder/bitrate
  tuning should target what the device actually outputs, not a fixed floor.
- **Has Google Play Services** (`com.google.android.gms`, `gsf`) — some
  Google integration present, so Cast may work, unconfirmed.
- **`com.softwinner.miracastReceiver` is installed** — this device has a
  built-in Miracast (WiFi Direct mirroring) receiver. Notably simpler than
  the full custom-receiver pipeline for *video-only* mirroring, but no
  remote-input or mic-uplink channel — doesn't replace the Oma Receiver app,
  worth keeping in mind as a possible quick-mirror fallback path later.
- No Android TV "Leanback" launcher package turned up in one grep pass —
  not conclusive on its own, worth a closer look when actually launching
  the receiver Activity (Phase 2), not assumed either way from this.

## Phase 0: complete

- `./gradlew assembleDebug` → real 11.9MB debug APK, confirmed (not just
  "exit 0" — the file exists and is that size).
- `adb install` → `adb shell am start` → confirmed via `dumpsys activity
  activities` that `com.example.omareceiver/.MainActivity` became
  `mResumedActivity` on the paired TV (192.168.1.86). The full vertical
  slice the spec asks for as Phase 0's target — build → push via ADB →
  launch → verify running, no manual TV navigation — works end to end on
  real hardware.
- Package name (`com.example.omareceiver`) and Activity name are template
  defaults; rename to `ai.oma.receiver` (per spec) when receiver
  development actually starts in Phase 2.

## Phase 1 in progress

**`gpt-live-1` WebRTC connection confirmed working against the real API**
(`scripts/spike_live_webrtc.py`): `webrtcbin` generates a real SDP offer,
`POST https://api.openai.com/v1/live/sessions` with
`{"session": {"model": "gpt-live-1"}, "transport": {"type": "webrtc", "sdp":
...}}` returns a real answer, ICE reaches `CONNECTED`. Test audio only so
far (`audiotestsrc`), not the mic, and no data channel for control
events/function-calling wired up yet.

Key facts learned by probing the real API directly (not from docs, which
were incomplete via WebFetch):
- `session.type` is *not* a valid field — just `{"model": "gpt-live-1"}`
  under `session`.
- WebRTC-only transport; the API rejects anything else.
- Response has top-level `session` and `transport` keys; the answer SDP is
  at `transport.sdp`.

Needed `gst-plugins-good` (RTP payloaders — `rtpopuspay` etc.) on top of
`gst-plugins-bad`; both now logged in `docs/DEPENDENCIES.md`, which is the
running list for the real installer, per the user's request to track every
package as we go rather than reconstruct it later.

## Next action

Real mic audio in, remote audio out, then the data channel for
control/function-calling events (client-delegation mode — Oma's own tool
registry handles the actual function calls, matching ADR-0001 D1). Wake
word comes before any of this opens a connection at all — no always-on
billed session. Then the daemon shell (`core/`) that ties it together.
