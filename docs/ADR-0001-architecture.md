# ADR-0001: Oma architecture and Phase 0 findings

Status: accepted for Phase 0 scope. Revisit after the Phase 2 vertical slice
(ADB → receiver → decoded frame) has run against real hardware once.

## Context

Oma is an independent voice assistant for Omarchy: local wake word, a
realtime voice model (`gpt-live-1`) for speech I/O, an LLM-driven planner
that converts natural language into typed tool calls (not a fixed phrase
table), and a casting subsystem that can put the desktop on a paired Android
TV/tablet without the user touching the TV's own interface.

This supersedes `jarvisd` (`~/Git/jarvisd`), a fixed-command wake-word daemon
built earlier the same day. jarvisd's systemd service has been disabled; its
code is left in place for reference (the action-dispatch patterns —
especially the Hyprland Lua/classic dispatch fallback — carry over directly,
see below). It is not deleted in case any of it is still useful.

## Decisions

**D1 — Policy-gated tool calls, not a phrase table.** Per spec: natural
language goes through conversation → planner → typed tool calls → policy
level check → execution → verification. This is a materially larger trust
surface than jarvisd's fixed table (see jarvisd's README "Safety" section for
what that traded away) — the mitigation is the 4-level policy classification
(read-only / reversible / sensitive-confirm / dangerous-deny) applied
per-tool, not per-utterance.

**D2 — Python 3.12 + asyncio for the daemon**, per spec's recommended
defaults. SQLite for device registry/task state/audit log (stdlib `sqlite3`,
no new dependency). `uv` for packaging, matching jarvisd.

**D3 — Hyprland control: probe, don't hardcode.** Confirmed live on this
machine (Hyprland 0.56.2, Omarchy 4.0.3): `hyprctl dispatch` now requires a
Lua expression (`hl.dsp.focus({ workspace = N })`); the classic
`dispatch <name> <args>` grammar errors. This was discovered and fixed in
jarvisd's `actions.py` (`_hyprctl_dispatch`: try the Lua form, fall back to
classic for older Hyprland). Oma's `execution/hyprland` module should reuse
that exact pattern rather than assume either syntax — the spec's own
instruction ("build a capability manifest... do not hardcode obsolete
Hyprland syntax") anticipated exactly this.

**D4 — Virtual displays via `hyprctl output create headless`.** Confirmed
working live: creates a 1920x1080 virtual output, appears in
`hyprctl monitors`, removed cleanly with `hyprctl output remove <name>`. This
is the classic (non-Lua) output subcommand and was not affected by the Lua
migration. This is the mechanism `display/` will use for "show my workspace
on the TV" without touching the physical monitor.

**D5 — Capture: PipeWire ScreenCast portal, not a bespoke capture path.**
Confirmed live: `org.freedesktop.portal.ScreenCast` is registered and
reachable over the user D-Bus session, backed by
`org.freedesktop.impl.portal.desktop.hyprland`. `gst-pipewiresrc` is also
installed and can consume a portal-negotiated PipeWire node directly.

**D6 — Encode: VAAPI H.264 via ffmpeg or GStreamer, hardware-accelerated.**
Confirmed live: `ffmpeg` lists `h264_vaapi` and the Intel HD 4000 in this
machine exposes it. This machine is the CPU-encode fallback case in the
spec's own terms if VAAPI ever fails to initialize for a given surface
format — worth a runtime check-and-fallback in the encoder module, not an
assumption that VAAPI always succeeds.

**D7 — WebRTC: `webrtcbin` (GStreamer), settled.** Installed via
`gst-plugins-bad` and confirmed live: `gst-inspect-1.0 webrtcbin` shows full
factory details, and `gst-launch-1.0 webrtcbin name=wb bundle-policy=max-bundle
! fakesink` reaches PLAYING and holds (the run ends only because the harness
timeout kills it — steady state while it waits for SDP negotiation is
correct). `aiortc` is not needed; this keeps the VAAPI-encoded path in
`display/encoder` on the same GStreamer pipeline as capture
(`pipewiresrc`) without a second media framework in the process.

## Confirmed available, no install needed

- Omarchy 4.0.3, Hyprland 0.56.2
- PipeWire + WirePlumber, `pw-record`/`pw-play` (already used by jarvisd)
- `xdg-desktop-portal` 1.22.1 + `xdg-desktop-portal-hyprland` 1.4.1
  (ScreenCast portal confirmed reachable)
- GStreamer 1.28.6 with `pipewiresrc`
- `ffmpeg` (n9.0.1) with `h264_vaapi`/`hevc_vaapi`/`av1_vaapi` encoders
- `avahi-daemon` active (mDNS/DNS-SD for device discovery — Cast/receiver
  advertisement)
- 93GB free disk (Android SDK + Gradle + JDK, if approved, fit comfortably)

## Toolchain — resolved

All installed. `adb`, `gradle`, `gst-plugins-bad` via `pacman`;
`android-sdk-cmdline-tools-latest` via AUR/`yay` (lands at `/opt/android-sdk`,
root-owned/read-only — SDK *packages* it manages, e.g. platform-tools,
platforms, build-tools, go to a user-writable `~/Android/Sdk` instead, set
via `ANDROID_SDK_ROOT`/`ANDROID_HOME` in `mise.toml`, same file that pins the
JDK). JDK 21 (Temurin) via `mise`, no sudo. Installed SDK packages:
`platform-tools` 37.0.1, `platforms/android-37.1`, `build-tools/37.0.0` (plus
`platforms/android-36`, pulled automatically by the project template).

The cmdline-tools package ships a newer, more capable `android` CLI
(v1.0.16261425) that deprecates the classic `sdkmanager`/`avdmanager` in favor
of unified `android sdk`/`android create`/`android emulator`/`android run`
subcommands — used throughout instead of the older tool names.

`android-receiver/` was scaffolded from the `empty-activity` template
(`android create empty-activity --minSdk 26`) and a first `./gradlew
assembleDebug` is running to confirm the full chain (JDK → Gradle → AGP →
SDK) actually produces an APK, not just that each piece installed.

## Still blocked on the user (hardware / live interaction)

1. **Live TV pairing.** Wireless ADB debugging has to be enabled *on the TV*
   and a pairing code read off its screen — this needs the user standing at
   the TV with me, in real time, it cannot be scripted ahead of that.
2. **Remote TV microphone.** The spec is explicit that this is not assumed
   available — most Android TV platforms reserve the remote's mic button for
   the system assistant and do not expose it to third-party apps via
   `AudioRecord`. Expect the capability test (Phase 4) to report
   `RESERVED_FOR_SYSTEM_ASSISTANT` on most hardware; a paired tablet/phone
   mic is the realistic fallback, not a hard requirement to design around as
   the primary path.
3. **`gpt-live-1`** — used as given per your confirmation. Not yet wired into
   any code; that's Phase 1 (voice subsystem), not Phase 0.

## Risks worth naming now

- **Trust surface jump vs. jarvisd.** D1 is the biggest one — an LLM decides
  which typed tool to call from natural language. The policy engine (4
  levels) and audit log are load-bearing safety, not nice-to-haves; they
  should exist and have tests *before* any Level 3+ tool is wired up for
  real, per the spec's own phase gating.
- **Android receiver toolchain weight.** Multi-GB install, plus this is the
  first Kotlin/Android work in this repo — expect Phase 2 to take
  meaningfully longer than the Linux-side phases.
- **WebRTC library choice is unresolved** (D7) — this affects both the
  receiver (must match) and the daemon's encoder module. Should be settled
  by a small spike (send one PipeWire-captured frame over `webrtcbin` to a
  trivial receiver) before Phase 2 real work starts, not assumed.
