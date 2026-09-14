# ADR-0001: Omarchy AI architecture and Phase 0 findings

Status: accepted for Phase 0 scope. Revisit after the Phase 2 vertical slice
(ADB → receiver → decoded frame) has run against real hardware once.

## Context

Omarchy AI is an independent voice assistant for Omarchy: local wake word, a
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
classic for older Hyprland). Omarchy AI's `execution/hyprland` module should reuse
that exact pattern rather than assume either syntax — the spec's own
instruction ("build a capability manifest... do not hardcode obsolete
Hyprland syntax") anticipated exactly this.

**D4 — Virtual displays via `hyprctl output create headless`.** Confirmed
working live: creates a 1920x1080 virtual output, appears in
`hyprctl monitors`, removed cleanly with `hyprctl output remove <name>`. This
is the classic (non-Lua) output subcommand and was not affected by the Lua
migration. This is the mechanism `display/` will use for "show my workspace
on the TV" without touching the physical monitor.

**D5 — Capture: `wlr-screencopy-unstable-v1` direct, NOT the PipeWire
ScreenCast portal (revised from the original decision after real evidence
it doesn't work).**

Original decision: `org.freedesktop.portal.ScreenCast` (confirmed
reachable over the user D-Bus session, `gst-pipewiresrc` consuming a
portal-negotiated PipeWire node). Confirmed live end to end once (Phase 2,
`scripts/spike_cast_sender.py`): real ICE COMPLETED / WebRTC CONNECTED
session to the Android TV. But `xdg-desktop-portal-hyprland`'s own
screencopy→PipeWire producer runs itself out of buffers almost immediately
(`[screencopy/pipewire] Out of buffers` / `Retrying screencopy` /
`Building modifiers for dma`, matching `hyprwm/xdg-desktop-portal-
hyprland#434` and GNOME LP#1987631) and reproduced as "one or two video
frames then a permanent stall," **every time, across three separate
mitigation attempts, each confirmed live and each confirmed NOT to fix
it**: `always-copy=true` + bounded `min/max-buffers` (2 frames instead of
1, then the same stall); a `video/x-raw` capsfilter forcing our consumer
off DMA-BUF (reasoned unlikely to reach the actual bug since
`videoconvert` already implicitly excluded `memory:DMABuf`, and confirmed
so); PipeWire's own `support.dmabuf.modifiers=false` config drop-in,
matching a documented community fix for the identical log signature (Arch
forum thread id=308493) — also re-tested live and confirmed NOT to fix it,
because that setting governs generic PipeWire client/session-manager
buffer negotiation, not xdph's own compositor-internal DMA-BUF capture
code, which turned out to be a separate path the setting doesn't touch at
all. `xdg-desktop-portal-hyprland` was already at the latest available
version (1.4.1, Arch `extra` and upstream's newest GitHub tag agree) — not
a "wait for a package bump" situation either.

**Decision, revised: bypass the portal entirely for video.** Talk to
Hyprland's `wlr-screencopy-unstable-v1` Wayland global directly (the same
protocol `grim` uses, proven reliable dozens of times this session) via
`pywayland`, feeding a GStreamer `appsrc` instead of consuming a
portal-negotiated `pipewiresrc` node. Proven in isolation first
(`scripts/spike_cast_wlr_screencopy.py`): 40 real seconds, ~60fps
sustained, 0 failures, real pixel-content changes confirmed via per-frame
CRC and a visual PNG sanity dump, **zero** `xdg-desktop-portal-hyprland`
log lines during the entire test (definitive proof of zero portal
involvement), and no consent dialog at any point (confirmed, not assumed:
`wlr-screencopy` is compositor-policy-gated, not portal-consent-gated, on
this Hyprland build — a real side benefit matching the earlier "no click
required" goal). Wired into `scripts/spike_cast_sender.py`'s video branch
(shared capture core in `scripts/wlr_screencopy_capture.py`) and confirmed
live end to end on real hardware: twelve consecutive 4-second windows of
Android's `EglRenderer` log all reading `Frames received: 60. Dropped: 0.
Rendered: 60. Render fps: 15.0` — continuous, steady video for the whole
test, not a stall at any point. This is the actual fix; the frozen-
after-one-frame bug is closed. Audio (`pipewiresrc target-object=<sink
monitor>`) is untouched and was never implicated — a different PipeWire
producer than the screencopy/DMA-BUF path that was actually broken. See
`STATUS.md` "Casting video: wlr-screencopy-unstable-v1 direct capture" for
the full trail, including a real segfault-on-exit bug found and fixed
along the way (unordered GC destroying Wayland proxies after their
Display was already freed — fixed with an explicit `disconnect()`).

**D6 — Encode: VAAPI H.264 via ffmpeg or GStreamer, hardware-accelerated.**
Confirmed live: `ffmpeg` lists `h264_vaapi` and the Intel HD 4000 in this
machine exposes it. This machine is the CPU-encode fallback case in the
spec's own terms if VAAPI ever fails to initialize for a given surface
format — worth a runtime check-and-fallback in the encoder module, not an
assumption that VAAPI always succeeds.

**D7 — WebRTC: split by job, revised after real evidence.**
`display/encoder` (TV casting) uses **`webrtcbin`** (GStreamer,
`gst-plugins-bad`) — confirmed live reaching PLAYING, and it keeps the
VAAPI-encoded path on the same pipeline as capture (`pipewiresrc`) without a
second media framework in that process.

`voice` (the `gpt-live-1` connection) uses **`aiortc`** instead, reversing
the original all-webrtcbin plan. Reason: webrtcbin hit two distinct, real
bugs building the voice spike — a streaming-thread deadlock linking an
incoming track synchronously (fixed with `GLib.idle_add`), then a deeper
issue where ICE reached `COMPLETED` and stayed there but exactly zero audio
frames were ever delivered to the linked pad (confirmed with a buffer
probe), never root-caused. `aiortc` — pure Python, async-native, matches
the daemon's own asyncio architecture (D2), no opaque C pipeline threading
— worked on the first properly-configured attempt and carried a full,
verified multi-turn conversation including a live language switch. See
`STATUS.md` "Phase 1: real two-way conversation confirmed working" for the
full debugging trail and the reverse-engineered `gpt-live-1` session
schema. Not a reversal of the tool choice generally — a recognition that
casting (tight PipeWire+VAAPI integration, GStreamer's actual strength) and
a simple audio-only client talking to one external API are different jobs.

**D8 — "Which window do you mean?" is answered with real floating labels,
not speech or a border flash.** When several open windows plausibly match
what the user said, the assistant needs to ask — and cheaper options (flash
a border, read out a text list) were considered and rejected per the user's
explicit request: real per-window name badges, positioned at each
candidate's actual on-screen rectangle. Built by copying this machine's own
existing precedent exactly rather than inventing a new mechanism:
`omarchy-osd` (the volume/brightness HUD) is a user-owned Quickshell plugin
(`~/.config/omarchy/plugins/<id>/`) that registers an `IpcHandler` on the
long-running `omarchy-shell` process and is driven by an external script
calling `omarchy-shell -q <target> <method> '<json>'`. `omarchy-ai.window-
labels` (`~/.config/omarchy/plugins/omarchy-ai.window-labels/`) follows the
identical shape — `manifest.json` + one QML file, `IpcHandler { target:
"windowLabels" }` exposing `show(payloadJson)`/`hide()`/`state()`/`ping()` —
with two differences from the OSD: it renders one floating chip per window
(a `Repeater` inside a `Variants{model: Quickshell.screens}` block, the same
per-output layer-shell pattern `omarchy.notifications`/`omarchy.background`
use, so a window's global x/y resolves correctly regardless of which
monitor it's actually on), and it has no auto-hide timer — the caller
(`hide_window_labels`) owns the lifecycle explicitly, unlike the OSD's
timed dismiss.

Two real bugs found and fixed getting this live, both confirmed via
`journalctl --user` (the shell logs to the journal under the
`omarchy-shell` tag, `QS_DISABLE_FILE_WATCHER=1` in
`omarchy-launch-shell` only disables Quickshell's own top-level reload-popup
mechanism, not the file-level plugin-reload path `PluginRegistry` uses):

1. `IpcHandler` needs `import Quickshell.Io` — omitting it fails silently
   from the CLI's perspective (`omarchy-shell <target> <method>` just prints
   "Target not found", no hint why) and only surfaces as `IpcHandler is not
   a type` in the journal.
2. `qs ipc call` (Quickshell's own IPC CLI, which `omarchy-shell` wraps)
   **splats a top-level JSON array argument into multiple positional CLI
   arguments** instead of passing it through as one string — confirmed
   live: `show '[{...},{...},{...}]'` (3 windows) failed with "Too many
   arguments provided (1 required but 3 were provided)", while the same
   call with exactly one array element worked. The OSD never hit this
   because its payload is always a JSON *object*. Fixed by wrapping the
   array: the wire payload is `{"windows": [...]}`, not a bare array — the
   QML `open()` accepts a bare array too (defensive, for any future caller
   that reaches the `IpcHandler` some other way), but every real caller
   must send the object form.

Also confirmed live: a plugin that fails to compile (bug 1 above) leaves the
running `omarchy-shell` process in a bad cached state that
`omarchy-shell shell rescanPlugins` and even repeated file saves (which do
correctly re-trigger `PluginRegistry`'s "Local plugin changed, reloading"
path — that part of hot-reload works) do **not** clear; only a real process
restart (`omarchy restart shell`) picks up the fix. Worth remembering for
any future Quickshell plugin work on this machine: if a change stops
producing new journal warnings but old behavior persists, restart the shell
process before assuming the fix itself is wrong.

A brand-new third-party plugin also isn't loaded just by existing on disk —
first-party non-bar plugins default to enabled, but a user-installed one
needs an explicit `omarchy plugin enable <id>` (which records it in
`~/.config/omarchy/shell.json`'s top-level `plugins[]` array), confirmed via
`omarchy-shell shell listPlugins` showing `"enabled": false` until that
ran.

On the Python side, `show_window_labels`/`hide_window_labels`
(`src/omarchy_ai/execution/actions.py`) resolve real window geometry via
`hyprctl clients -j` — the same shape `list_windows` already parses — using
the identical app/title substring fuzzy-match `focus_window` uses, just
applied once per target in a list (or every mapped window when no targets
are given). Confirmed live end-to-end, independent of the voice daemon
(direct Python call → real IPC → screenshot): with 1, 3, and all-4 open
terminal windows, correctly-positioned badges appeared at each window's
actual top-left corner, matching titles, and `hide_window_labels` cleared
them immediately. See `STATUS.md` for the screenshots' details and the
`config.py` instructions wiring.

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
- **WebRTC for voice is settled (D7), casting is not yet exercised
  end-to-end.** The Android receiver side of `webrtcbin` (send one
  PipeWire-captured frame to a trivial receiver, then to the actual
  Kotlin app) is still an assumption, not a confirmed spike, going into
  Phase 2.
- **Residual audio-quality issue, not yet resolved.** Some static under a
  longer/heavier voice conversation, investigated with real evidence
  (ruled out: packet loss, PipeWire XRUNs) but not conclusively
  root-caused — see `STATUS.md`. Not a functional blocker; worth revisiting
  before this becomes the default daily-use experience.
