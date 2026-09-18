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

**D9 — The Watch Dogs overlay is driven by the daemon's own lifecycle, not
by a model tool call, and reuses D8's IPC pattern exactly.** User-requested,
tracked as a "not started" next-action since early in this project: a
Matrix/hacking-HUD overlay (cascading green/cyan monospace feed, terminal
readouts, subtle scanline/pulse accents) that appears for the duration of a
voice conversation and shows real activity — not decoration. The explicit
ask was "connect it to the tooling so it can actually do something", so the
feed is literal, real tool-call activity (`> execute_command("Browser")`,
`> volume_up() -> ok`), not randomly generated characters.

Unlike D8 (a tool the model calls on its own, one-shot show/hide), this
overlay stays open for an entire session and needs an ongoing event stream
while up, so the IPC surface has three methods instead of D8's two:
`start` (show + reset scroll content for a new session), `event` (append a
feed line or update the state indicator — called repeatedly through the
conversation), `stop` (hide, session over). Built as a new user-owned
plugin, `~/.config/omarchy/plugins/omarchy-ai.watchdog/` (`manifest.json` +
`Watchdog.qml`), copying D8's own shape exactly: `IpcHandler { target:
"watchdog" }`, the same `Variants{model: Quickshell.screens}` per-output
layer-shell pattern, `BorderSurface`/`Style`/`Color`/`Border` design tokens
from `qs.Commons`/`qs.Ui`, but with the color/font choices skewed toward
the hacking-terminal aesthetic rather than the shell's default theme
colors: `Style.font.family` (which already resolves to this machine's
system monospace, `JetBrainsMono Nerd Font` — confirmed via this repo's own
terminal configs, `~/.config/{alacritty,foot,ghostty}`) for the feed, and a
small green/cyan palette (`#39ff88`/`#39e6ff`/`#a6ff4d` for
connecting/listening/thinking/speaking states and call/ok/thinking tones,
`#ff5f5f` for tool-call errors) layered on top of the theme's own
`Color.background`/`Border` tokens rather than replacing them outright.
Kept GPU-light per the original ask: a small bottom-right corner panel
(similar footprint to a small waveform panel, not a full-screen takeover),
one animated scanline `Rectangle` and one pulsing state dot as the only
continuous animations, both paused via `running: root.opened` whenever the
overlay is closed — no shaders, no per-frame JS.

One QML gotcha specific to this plugin, not present in D8: `Item` already
reserves a property named `state` for QtQuick's own States/Transitions
machinery, so the conversation-state property had to be named `convState`
instead — redeclaring `state` directly on an `Item` subclass collides with
the built-in one rather than cleanly overriding it. (The `IpcHandler`
method literally named `state()` — returning `"open"`/`"closed"`, same as
D8 — is unaffected; it lives on a different QML object with no such
built-in.) Same array-vs-object payload rule as D8 applies to every payload
here too, though none of them happen to carry an array today.

Python side: a new `src/omarchy_ai/voice/watchdog.py` wraps the
`omarchy-shell -q watchdog ...` calls, mirroring `actions.py`'s `_run`
subprocess convention but going further — every call is wrapped so a
failed/missing IPC call only logs a warning and returns, never raises,
because this sits directly in `live.py`'s own hot paths (session connect,
every real tool call, transcript-delta handling, hangup) and must never be
allowed to take down an actual conversation. Wired into `voice/live.py` at
four points: `run()` calls `watchdog.start()` right after "live session
connected" logs; `_run_tool_call()` calls `watchdog.state("thinking")` +
`watchdog.tool_call(name, args)` before running the action and
`watchdog.tool_result(...)` after, reusing the exact `name`/`args`/
`result.ok`/`result.message` already flowing into `_action_log` and the
existing log lines — no parallel tracking; `_on_data_message()` emits
`watchdog.state("listening")`/`"speaking"` on the *first* delta of each new
user/assistant utterance (checked via "was the buffer empty before this
delta", not once per token, to avoid an event storm — the ask explicitly
called this out); and `run()`'s `finally:` block calls `watchdog.stop()`
unconditionally, so even a session that fails before ever connecting still
clears the overlay.

Verified live: `omarchy plugin validate`, then `omarchy-shell watchdog
ping`/`state` confirmed the plugin compiled and the IpcHandler responded
(no repeat of D8's "not a type" failure mode). Direct IPC calls through
both raw `omarchy-shell watchdog ...` and the real `watchdog.py` module
(bypassing only the live WebRTC session, which requires an actual spoken
wake word this agent cannot produce) exercised every state
(connecting/listening/thinking/speaking), both tool-call/tool-result tones
including the error path, and line truncation on a deliberately long
window title and error message — screenshotted at each step: the panel
renders bottom-right, cascading green/cyan lines match the ask's own
example format exactly (`> execute_command("Browser")`, `> volume_up() ->
ok`), and `stop` clears it with no leftover artifacts. `omarchy-ai.service`
was independently restarted mid-session by another in-progress agent task
in this same repo (unrelated wake-word/ONNX work) while this feature was
being built; that restart picked up this feature's `live.py`/`watchdog.py`
changes too and the service came up clean (`omarchy-ai ready`, no crash),
which is real evidence the integration loads correctly inside the actual
daemon process, even though no live spoken conversation happened to occur
in the window this agent had to observe it. See `STATUS.md` for the
screenshots' details.

**D10 — TV discovery is real mDNS (`_androidtvremote2._tcp` only), "which
TV?" is a plain spoken back-and-forth (not a D8-style visual badge), and
new-TV setup is an honest, human-in-the-loop guided flow, not a magic
one-shot install.** Extends the single-hardcoded-IP `start_casting`/
`stop_casting` built and confirmed live earlier the same day (`_TV_ADB_ADDR
= "192.168.1.86:5555"`) with real auto-discovery, disambiguation, and
assisted receiver installation. Full live evidence trail in `STATUS.md`
("Casting: auto-discovery, TV disambiguation, assisted receiver install");
this entry is the design reasoning.

*Which mDNS service type indicates "a real cast target".* `avahi-browse -a
-t` run live on this network surfaced two different Android-TV-adjacent
service types, confirmed NOT interchangeable: `_androidtvremote2._tcp`
(three real devices: "Living Room TV", "Idol TV", "Philips 4K A1" — the
protocol the official Android TV Remote/Google Home app uses, a strong
signal the device is actually Android TV OS) versus `_googlecast._tcp` (a
much broader "any Cast-capable device" set that, on this same live browse,
also included plain Chromecasts and — concretely — a "Nest Audio" smart
speaker, a "Google Home" smart speaker, and a "Google Nest Hub" smart
display, none of which run Android TV OS or accept `adb install`). One
`_googlecast._tcp` entry ("Idol TV") shares an IP with its
`_androidtvremote2._tcp` counterpart, confirming they can name the same
physical device, but the other three `_googlecast._tcp` entries have no
`_androidtvremote2._tcp` match at all — real, live proof `_googlecast._tcp`
alone would surface unusable "TVs" (a Nest speaker isn't installable).
Decision: `src/omarchy_ai/display/discovery.py` browses
`_androidtvremote2._tcp` only for cast targets. The real ADB debug port
(5555 for this project's already-paired TV) is unrelated to the port
`_androidtvremote2._tcp` advertises (6466, the remote-control protocol's
own port) — discovery only supplies the IP; 5555 is still assumed for an
already-adb-enabled device, same assumption `_TV_ADB_ADDR` always made.

*Disambiguation is spoken, not visual, on purpose.* D8 built real on-screen
name badges for "which window?" because windows have an actual screen
position to badge. TVs are physical devices in different rooms — there is
no on-screen rectangle to badge. So the parallel mechanism is a
`list_cast_targets` tool (returns discovered `{name, address}` pairs) the
model calls right before asking "which TV?" out loud, and `start_casting`
takes an optional `target` (name substring or raw IP) so the model can
pass along the user's spoken answer. `start_casting` never guesses when
it's genuinely ambiguous: with no `target` and >1 discovered TV, it
returns `ok=false` with the candidate names in the message instead of
picking one, mirroring the "ask, don't guess" spirit of D8 without
reusing its visual machinery, which doesn't apply here. Confirmed live
against the real network: `list_cast_targets` returned exactly the two
resolvable real devices (`Living Room TV`/192.168.1.191,
`Idol TV`/192.168.1.203 — "Philips 4K A1" intermittently fails to
*resolve* to an address even though it's *seen*, a real avahi timeout
against this specific device, not a parsing bug: confirmed by rerunning
`avahi-browse -r -p -t _androidtvremote2._tcp` directly and seeing the
identical "Failed to resolve service 'Philips 4K A1' ... Timeout reached"
on stderr); `start_casting` with no target and 2 real devices discovered
returned the correct ambiguous-refusal message; with `target="idol"` it
resolved to the single real match; with a nonexistent name it returned an
honest "not found, available: ..." rather than falling back silently; with
a raw IP (`192.168.1.86`, the already-paired TV, which isn't
`_androidtvremote2._tcp`-discoverable at all) it passed the address
through directly. Zero-discovery (mDNS having a bad moment, or
`avahi-browse` itself unavailable) falls back to the one TV already
proven working earlier today, rather than turning a working feature into
a hard failure the instant discovery hiccups.

*Assisted install is honest about the one step that can't be automated.*
Real, checked-live constraint: `_adb-tls-connect._tcp`/
`_adb-tls-pairing._tcp` (the mDNS services Android's own Wireless
Debugging feature advertises) return zero results on this network right
now — confirmed directly, matching that no TV currently has Wireless
debugging toggled on. There is no way to discover, pair with, or install
onto a TV that has never had a human turn that on, on the TV's own screen
— this is a real Android OS/UX gate, not a gap in this project's tooling,
and `install_receiver_on_tv`'s tool description says so plainly rather
than implying the whole thing is one-shot automatic. What IS automated:
building the APK if `android-receiver/app/build/outputs/apk/debug/
app-debug.apk` doesn't already exist (`./gradlew assembleDebug` — not
needed this session, the APK from Phase 2's `dfed7bf` wallpaper commit
was already present and current, 55.9MB), discovering a TV that has
reached the "Pair device with pairing code" screen
(`_adb-tls-pairing._tcp`), running `adb pair <addr> <code>` once the user
reads the code out, discovering the *separate* general reconnect address
Wireless Debugging assigns (`_adb-tls-connect._tcp` — a real, checked
distinction from the fixed 5555 this project's already-paired TV uses,
because that TV was set up the older `adb tcpip 5555` way, not via
Wireless Debugging; a genuinely new TV's post-pairing connect port is a
different, randomly assigned port, not 5555), then `adb connect` and
`adb install -r`. Module-level state (`_pending_pair_target`, same pattern
`_cast_process` already used across separate tool calls) carries the
discovered pairing target from the "no code yet" call to the "here's the
code" call.

**Not verified live end to end** — honestly, not just as a caveat: no
unpaired TV was available this session to actually run the pairing flow
against (`_adb-tls-pairing._tcp` returned empty throughout, as expected
with every real TV on this network already set up some other way). What
IS verified live: the discovery calls themselves run cleanly against the
real network and correctly return empty (not an error, not a crash) for
both adb-tls service types; the APK-exists check short-circuits
correctly when the file is already there; the "no pairing signal yet"
narration path returns real, correct guidance text
(`run_action("install_receiver_on_tv", {})` called directly). The
`adb pair`/`adb connect`/`adb install` sequence itself, and the
`_pending_pair_target` hand-off between calls, are reasoned through
against real `adb`/mDNS semantics and this project's own already-proven
`adb connect`/`adb install` calls elsewhere in `actions.py`, but not
exercised against a live pairing session — flagged here rather than
claimed as proven.

Also **not** re-run this session: a full `start_casting(target=...)`
end-to-end live cast. Real reason, checked rather than assumed: the
earlier-today `start_casting({})` tool call (confirmed via `journalctl`,
15:08:11-15:08:14) left a real `spike_cast_sender.py` process (pid 190420)
actively streaming to the paired TV — confirmed via `ss` showing live
ESTABLISHED websocket connections both to the local signaling relay and
from the TV's own connection to it, 8+ minutes elapsed. The signaling
relay (`display/signaling.py`) is deliberately single-sender/single-viewer
("the older socket is dropped in favor of the new one") — starting a
second sender to test `target=` would have kicked that live session off
mid-stream. The changed logic itself (`_resolve_cast_target`) was
independently verified live instead (see above), and it's the only new
code in `start_casting`'s path — the connect/launch/spawn sequence after
target resolution is byte-for-byte the sequence already proven live
earlier today, just parameterized on `tv_addr` instead of the constant
(plus `-s tv_addr` added to the `am start` call, to target the right
device if more than one TV is ever adb-connected at once — not itself
exercised against two simultaneously-connected TVs, no second TV was
adb-paired this session to test that with).

Also confirmed, a sharper version of this project's existing
"don't casually restart `omarchy-ai.service`" discipline: `systemctl
--user show omarchy-ai -p KillMode` is `control-group`, and
`spike_cast_sender.py` (pid 190420, the live cast above) is a *member of
that same cgroup* (`/proc/190420/cgroup`), despite being a detached
(`start_new_session=True`) process the daemon merely spawned. A service
restart right now would kill the live cast too, not just interrupt a
conversation — this is a stronger reason to defer the routine post-change
restart than the discipline previously accounted for. Deliberately not
restarted this session; see `STATUS.md`'s next actions.

**D10 — Settings live in `config.yaml`; the panel talks to a Python CLI,
never YAML directly.** The settings bar panel (`omarchy-ai.settings`,
`~/.config/omarchy/plugins/omarchy-ai.settings/`) is plain QML/JS with no
YAML parser or `Config`-merge logic of its own — duplicating
`config.py`'s merge-over-defaults semantics in JS would drift out of sync
with the real daemon. Instead every control shells out to a new
`src/omarchy_ai/cli/settings.py` (the `omarchy-ai-settings` console
script) via a Quickshell `Process`, the same pattern
`$OMARCHY_PATH/shell/plugins/panels/dropbox/status.py` already
establishes for "a panel needs real backend logic beyond what QML/JS
should own." Only a curated whitelist of `Config` fields is settable this
way (wake models, `wake_threshold`, `watchdog_enabled`,
`watchdog_display_mode`, `voice`) — not every dataclass field; internal
plumbing (`api_key_path`, `context_max_chars`, `instructions`, ...) stays
off the panel entirely.

**D11 — The panel triggers its own restart, gated by the same
conversation-lifecycle check this project's agents already apply by
hand.** `Config` is only loaded once, at `OmaDaemon.__init__` — a config
write is inert until the daemon restarts, so *some* restart step is
unavoidable regardless of who initiates it. Chose "the panel's Restart
button calls `restart`, which replays `daemon.py`'s own `"wake word
detected..."`/`"session ended..."` log lines over recent `journalctl`
output to refuse if a conversation is in progress" over "always just tell
the user to restart manually" — automating a step behind a safety check
this project already trusts by hand is less friction without materially
more risk. See `STATUS.md`'s "Settings menu + ASCII visualizer" entry for
a real gap found in that check after the fact: it only knows about the
daemon's own conversation lifecycle, not detached subprocesses (e.g. a
live TV cast) sharing the service's cgroup, which `KillMode=control-group`
also kills on restart — not yet fixed, no harm done this round only by
lucky timing against a concurrent restart.

**D12 — The ASCII visualizer is a Watch Dogs *display mode*, not a
separate overlay.** Reuses D9's existing `IpcHandler{target:"watchdog"}`
and per-conversation lifecycle (`start`/`event`/`stop`) rather than adding
a fourth plugin: `start()` now carries a `displayMode` ("feed" |
"visualizer" | "both") sourced from `Config.watchdog_display_mode`, and a
new `event {"kind":"level", "level": 0..1}` message (dispatched from
`live.py`'s `_play_remote_audio` via a **non-blocking**
`subprocess.Popen(start_new_session=True)` — every other `watchdog.py`
helper can afford `subprocess.run`'s brief wait, this one sits in a
real-time audio loop that already had a jitter bug from a blocking call
once before, see `STATUS.md`'s audio debugging trail) feeds a rolling
28-sample amplitude buffer rendered as one `Text` element indexed into
`" ▁▂▃▄▅▆▇█"`. Cleared on any state transition away from `"speaking"` so
it can't show a stale frozen frame. `watchdog_enabled: bool = True` gates
the whole overlay (every `watchdog.*` call site in `live.py`, not just
`start()`) — a user who wants it off entirely gets zero `omarchy-shell`
IPC calls per conversation, not just "the panel never becomes visible."

**D13 — Signaling buffers the sender's offer/ICE instead of dropping them,
and cast subprocesses run in their own systemd scope instead of the
daemon's cgroup.** Two related fixes for "casting reports success but
never actually mirrors" (real live evidence, full trail in STATUS.md);
design reasoning here.

*Buffering, not a longer sleep.* The relay (`display/signaling.py`) used
to relay sender<->viewer messages only while both were simultaneously
connected, dropping anything else. Confirmed live (7/7 real attempts) this
isn't a rare race: the sender creates its WebRTC offer in 300-700ms, but
a cold Android app start (APK load, ART, native WebRTC init, its own
signaling connect) reliably takes longer — so the offer was dropped
*every* time, not occasionally. A fixed longer delay in `start_casting`
was rejected as the fix: it doesn't address the actual design flaw (no
retry/buffer), degrades UX (adds latency even on a warm start), and is
still fragile against a genuinely slow TV/network. Instead `Relay` buffers
the sender's most recent offer plus any ICE candidates while no viewer is
connected, replaying them in order the instant one registers. Only
sender->viewer is buffered — the project's own evidence is 100%
sender-before-viewer, no observed case of the reverse, so buffering that
direction too was judged unnecessary complexity rather than added
defensively. A fresh sender connection (new cast attempt) discards any
stale buffered state rather than accumulating it or replaying it into a
later, unrelated session — keyed to connection lifecycle, not a timer.

*Cast subprocesses decoupled from `omarchy-ai.service`'s cgroup.*
Separate, real problem found while verifying the above: the service's
`KillMode` is systemd's default, `control-group`, and `_cast_process`/
`_signaling_process` — spawned via plain `subprocess.Popen(...,
start_new_session=True)` — were still members of that cgroup despite
`start_new_session=True` (confirmed via `/proc/<pid>/cgroup`), so
restarting the daemon to load *any* code change would kill an active cast
too, directly contradicting the requirement that casting only ever stops
via an explicit `stop_casting` call. Fixed with `_spawn_own_cgroup` in
`actions.py`: casts now launch via `systemd-run --user --scope --collect
--quiet -- <argv>`. Confirmed live before wiring it in that `--scope`
execs straight into the target (no wrapper process — `Popen.pid` is the
real payload pid, `poll()`/`terminate()` behave identically to a plain
`Popen`) while the process lands in its own transient scope unit under
`user.slice`, not the service's. Falls back to a plain `Popen` if
`systemd-run` is missing rather than failing casting outright. Verified
live end to end, including an actual `systemctl --user restart
omarchy-ai.service` fired mid-cast (user-authorized disruptive testing):
video kept flowing, 0 drops, straight through the restart — see
STATUS.md for the full frame-log evidence.

*A third, independent root cause found during verification, not fixed
here:* this machine's `ufw` only allows inbound TCP on the signaling port
from one hardcoded IP (`192.168.1.86`, the original Phase 0 TV) —
`/etc/ufw/user.rules`: `-A ufw-user-input -p tcp --dport 8765 -s
192.168.1.86 -j ACCEPT`, almost certainly predating D10's multi-TV
discovery and never widened. `journalctl -k` showed real `[UFW BLOCK]`
lines for Living Room TV's SYN packets to port 8765, including on the
very first cast attempt of this session, before any of today's code
changes loaded — proving this has silently sabotaged every cast to any
TV other than 192.168.1.86 regardless of the signaling fix. Not fixable
by this session (no passwordless sudo); see STATUS.md for the exact
one-line command the user needs to run.

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

## 2026-09-18 addendum: native Jev delegation

The live conversational model delegates supported native desktop goals to a
bounded typed worker, following the existing Jev browser approach. Jev receives
structured state and selects operations/targets; existing code executes and
independently verifies them. Text generation, vision, complex planning and
uncertain decisions remain with the live model. Reference knowledge is retrieved
from packaged Omarchy research and Arch notes and cannot expand the executable
allowlist. See [Jev desktop design and validation](JEV-DESKTOP.md).
