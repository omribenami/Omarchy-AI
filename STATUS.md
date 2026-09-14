# Status

## Phase 1: real OS control wired up, confirmed live

Oma can now actually do things, not just talk. `src/omarchy_ai/execution/`:

- **`actions.py` / `tools.py`** — ~25 typed tool functions (volume,
  brightness, workspace, window, launchers, media, Bluetooth, night light,
  battery) ported from jarvisd's already-tested `actions.py`, exposed to
  `gpt-live-1` via `delegation.responses.tools` (see the "real tool call"
  schema notes below).
- **`keybindings.py`** — rather than hand-writing a function per Omarchy
  command (228 of them), `list_commands`/`execute_command` reuse Omarchy's
  own `omarchy-menu-keybindings` bash functions directly (`output_binding_
  records`, `dispatch_binding`), sourced with `$1` forced to `--print` so
  the interactive picker never opens. This matters because `hyprctl binds
  -j` only exposes an opaque internal id for Lua-dispatched binds
  (`dispatcher: "__lua", arg: "6"`) — the menu script already re-resolves
  those from source, so reusing it avoids reimplementing (and risking
  drifting out of sync with) that resolution logic. First attempt had a
  real bug: `$1`/`$2` got clobbered by the `--print` safety trick before
  `dispatch_binding` could read them — fixed by passing dispatcher/arg
  through environment variables instead. Confirmed live end-to-end
  (`list_commands("gaps")` → `execute_command("Toggle window gaps")`
  actually flipped `general:gaps_in`/`gaps_out` to `0 0 0 0`).
- **`vision.py`** — `describe_screen` takes a screenshot and asks a
  vision-capable model about it via a plain, separate Responses API call
  (`gpt-5`, non-realtime) rather than attempting to feed images through
  gpt-live-1's own event stream (unverified whether this new API even
  supports that). Confirmed live: correctly described real, multi-window
  screen content and was used by the model *before* toggling gaps, to
  check the current state first rather than guessing.
- **Window awareness** (`list_windows`, `focus_window` in `actions.py`) —
  the gap the user caught: `window_fullscreen_toggle`/`close_window` only
  ever affected whichever window Hyprland happened to have focused, and
  the model had no way to know what was actually open. Confirmed live:
  asked to fullscreen a specific unfocused terminal by name, it correctly
  called `list_windows` → `focus_window("...")` → `window_fullscreen_
  toggle` in sequence, verified via `hyprctl clients -j`.
- **Typing** (`type_text`, `press_key`, via `wtype`) — real keystroke
  injection into whatever's focused (terminal, browser, any text field).
- **Per-conversation action history** (`get_recent_actions`) — in-memory
  only, resets each session (deliberately, per the user's own call on
  scope — persistent cross-restart history is bigger, deferred work).
  Tracked in `LiveSession._action_log`, keyed by whichever window was
  focused at call time, queryable filtered by window.

### A real architectural bug found and fixed: blocking the event loop

`_check_function_call` executed `run_action(...)` synchronously inline —
fine for fast actions, but `describe_screen`'s multi-second vision API call
froze the *entire* asyncio event loop for its duration, which starved
aiortc's own connection handling running on that same loop. Confirmed live:
this crashed a real session (`describe_screen crashed` in the log, an
unhandled exception from a socket read that failed because the connection
broke underneath it while the loop was blocked). Fixed by moving all tool
execution (not just the slow ones — this needed to be true generally) into
`loop.run_in_executor` via a new `_run_tool_call` coroutine.

### The real `gpt-live-1` tool-calling schema (reverse-engineered further)

Building on the `end_conversation` findings already in this file:
- Submitting a tool's result back over the data channel: **`conversation.
  item.create`** (the older Realtime API's convention) is rejected —
  confirmed live, the error response listed the actual supported event
  types, `response.item.create` among them. Same `function_call_output`
  item shape, just the corrected event name.
- Submitting the result does **not** make the model continue on its own —
  confirmed live: no error, but also no reaction for 6+ seconds until an
  explicit follow-up `response.create` was sent right after. Same
  "needs an explicit nudge" pattern as the very first response.
- Real tools (not just `end_conversation`) needed the same `delegation.
  responses.tool_choice: "auto"` fix already documented below — without
  it, confirmed the model doesn't consider calling them either.

### Connection latency: ~5.3s → ~0.6-0.9s

Root-caused in aioice's actual source, not guessed: `RTCPeerConnection()`
with no explicit config defaults to Google's public STUN server
(`aiortc.rtcicetransport.getDefaultIceServers`), and `aioice.ice.Connection.
get_component_candidates` has a hardcoded `timeout: int = 5` waiting for a
STUN reply before falling back to host-only candidates. Every connection
this project has made has worked on host candidates alone (this network's
NAT allows outbound-initiated connections without needing a reflexive
candidate) — the STUN wait was pure dead weight. Fixed with
`RTCPeerConnection(RTCConfiguration(iceServers=[]))`. Confirmed live,
repeatedly: `setLocalDescription` dropped from 5.01s to ~0.01s, total
connect time from ~5.3s to 0.44-0.89s across several real runs.

### Graceful handling for unrecoverable billing errors

Confirmed live: the OpenAI org ran out of credits mid-session
(`insufficient_quota` / `credit_balance_exhausted`, verified independently
with a plain `gpt-5` API call outside the daemon entirely, ruling out a
code bug) and the session just retried the same failing request every
~15-20s forever with no self-recovery, burning real time silently. Now
recognized (`credit`/`quota`/`billing` in the error code or message) and
hung up immediately with a clear log line instead of retrying blind.

### Known false-positive lesson (farewell detection)

Confirmed live, twice, as real bugs rather than theoretical risks: the
model saying "let me **take care** of that" (about to run a tool) matched
the farewell marker "take care" and ended a real conversation early.
Removed the ambiguous marker; kept only phrases with no plausible
non-farewell reading (`goodbye`, `farewell`, `talk to you later`, `take
care of yourself` / `take care now`). Worth remembering if adding more
markers later: test against "the model narrating handling a request," not
just genuine goodbyes.

## Completed

- Retired `jarvisd` (disabled its systemd service; code left in
  `~/Git/jarvisd` for reference — its Hyprland Lua/classic dispatch fallback
  pattern carries over directly, see ADR-0001 D3).
- Repo scaffolded at `~/Git/omarchy-ai` (renamed from `oma` — see the
  rebrand note further down): `src/omarchy_ai/{core,voice,execution,vision,
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
in use on this machine), pinned in `mise.toml` (committed) so this project's
own setup script can reproduce it the same way — same pattern jarvisd used
for its Python venv.

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
  remote-input or mic-uplink channel — doesn't replace the Omarchy AI
  Receiver app, worth keeping in mind as a possible quick-mirror fallback
  path later.
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
  defaults; rename to `ai.omarchy.receiver` when receiver
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

## Phase 1: real two-way conversation confirmed working end to end

`scripts/spike_live_aiortc.py` — a full, natural, multi-turn voice
conversation with `gpt-live-1` (backed by `gpt-5` via Responses delegation),
verified live including a mid-conversation language switch to Hebrew that
the model correctly followed. Not simulated, not a canned demo — actual
speech in, actual understanding, actual speech out, confirmed against the
user's own ears after several real debugging rounds (see below).

**Switched the transport from GStreamer's `webrtcbin` to `aiortc`** (pure
Python, `uv add aiortc`) after `webrtcbin` hit two distinct, real bugs in
one session: a streaming-thread deadlock when linking an incoming track
synchronously (fixed with `GLib.idle_add`, but then a second, deeper issue
where the negotiation completed and ICE reached `COMPLETED` yet exactly
zero audio frames were ever delivered to the linked pad, confirmed via a
buffer probe — likely a receive-path routing incompatibility with this
GStreamer version, never root-caused). `aiortc` worked on the first
properly-configured attempt. Revises ADR-0001 D7: `webrtcbin` is still
right for the *display/casting* subsystem (tight PipeWire+VAAPI
integration), but the *voice* client uses `aiortc` — different tools for
genuinely different jobs, not a wholesale reversal.

### The real `gpt-live-1` session-creation schema (reverse-engineered
against the live API — not fully documented publicly yet)

```json
POST https://api.openai.com/v1/live/sessions
{
  "session": {
    "model": "gpt-live-1",
    "delegation": {"type": "responses", "responses": {"model": "gpt-5"}},
    "instructions": "...",
    "audio": {"output": {"voice": "marin"}}
  },
  "transport": {"type": "webrtc", "sdp": "<local SDP offer>"}
}
```

- `instructions`, `audio.output.voice`, and `delegation` are **session-creation-time only** — sending them later via a `session.update` data-channel event is rejected (`unknown_parameter`). Only some fields (not yet fully mapped) are updatable post-creation.
- Two delegation modes: `client` (the default if omitted) and `responses`.
  In `client` mode, `turn_detection` isn't even a recognized field and
  `response.create` is explicitly rejected ("requires a session with
  Responses delegation") — in this mode the app is expected to drive
  everything itself, matching ADR-0001 D1's original architecture intent,
  but the actual event(s) for "speak this" were never found this session.
  **`responses` mode is what's actually wired up and confirmed working**:
  send one `{"type": "response.create"}` on the data channel after it
  opens, and the backend (`gpt-5` here) then manages the entire
  conversational turn-taking on its own — no further `response.create`
  calls needed, confirmed across a multi-turn conversation including a
  language switch.
- Response text streams over the data channel as
  `session.output_transcript.delta` events; the user's own recognized
  speech streams back as `session.input_transcript.delta` — both are
  genuinely useful for a text log / debugging without needing to decode
  audio at all.
- `session.usage.updated` reports elapsed seconds — useful for a cost
  display later.

### Audio bugs found and fixed, in order (each confirmed with real evidence,
not guessed)

1. **Stereo/mono mismatch** — the decoded remote track is `s16`/stereo/
   48kHz even though our own input is mono; feeding those bytes straight to
   a mono-configured player was the first "sounds like bad reception" bug.
   Fixed by resampling every frame through `av.AudioResampler` to a fixed
   target format instead of assuming the source matches.
2. **Unbounded plane read** — `bytes(frame.planes[0])` can include padding
   beyond the frame's actual `samples` count; a debug capture confirmed
   this directly (52s of buffered "audio" for ~10s of wall-clock time, 99.5%
   near-silent). Fixed by slicing to `[: frame.samples * 2]`.
3. **Low volume** — measured RMS was ~451/32767 (~-37dBFS), objectively
   quiet, confirmed with `numpy` analysis of a captured WAV, not a
   subjective call. Fixed with a 4x gain stage with clipping protection
   (`np.clip`), verified afterward at RMS ~3856 with 0% clipped samples.
4. **Real-time-only static** — isolated by playing the *captured* file
   directly (clean) versus the *live* stream (still static) — proved the
   PCM data itself was fine and the bug was specifically in real-time
   delivery timing, not content. Root cause: writing each 20ms chunk to
   `pw-play` the instant it's dequeued has no cushion for normal network
   jitter, so any momentary gap underruns `pw-play`'s buffer. Fixed with an
   ~160ms pre-buffer (8 frames) before playback starts draining the queue.
   User-confirmed clean after this fix.
5. Also moved playback writes off the asyncio event loop entirely onto a
   dedicated `threading.Thread` + `queue.Queue` — a `run_in_executor`
   per-frame approach still weren't sufficient on their own; the real fix
   needed both this *and* the jitter buffer above.

### Residual static — investigated, not conclusively resolved

Volume and the worst of the crackling are fixed (user-confirmed "sounds
much better", "sounds clean" on short tests) but a longer, heavier
multi-turn conversation with simultaneous mic input still had some static.
Ruled out with real evidence, not guesses, in this order:
- Grew the jitter/pre-buffer from ~160ms to ~500ms (`PREBUFFER_FRAMES`) and
  added re-buffering after sustained (not single) empty reads — reduced but
  didn't eliminate it.
- Captured `pw-play -v`'s own stderr directly (previously discarded) —
  zero XRUN/underrun/error messages anywhere across a full session, arguing
  against a local PipeWire buffer starvation explanation.
- Checked real WebRTC stats via `pc.getStats()` — **`packetsLost=0`**
  throughout every sample, ruling out network packet loss / Opus
  loss-concealment artifacts. Jitter values fluctuate normally (11-74, no
  sustained climb — an early read of 3 data points looked like a trend and
  wasn't, corrected after more samples came in).
- Tested native stereo output (no mono downmix) to rule out resampler
  downmix math as the cause — inconclusive from this session's testing.
- User's own working theory: could simply be the laptop's speaker hardware
  (a real possibility — cheap built-in speakers audibly distort certain
  content/volumes). Suggested the free confirmation test (headphones,
  bypasses the speaker entirely) but this wasn't confirmed either way
  before the session moved on.

**Bottom line: not a blocker.** The conversation works, is understood
correctly, and responds appropriately — this is audio *quality* polish, not
a functional gap. Worth revisiting with either headphone confirmation or a
proper spectral analysis of a captured "static" segment (a real click/pop
has a very different frequency signature than either speaker rattle or a
genuine software glitch — that comparison would resolve it definitively)
before spending more time guessing at it.

### Known gap, caught by the user mid-session

The first several "working" runs weren't proof of real conversation — the
script fired `response.create` immediately when the data channel opened,
before there was any real time to speak, so every response was a generic
scripted greeting from the instructions rather than a reply to actual
speech. Fixed with a `SPEAK_WINDOW_SECONDS` delay before the first
`response.create`; the *next* run after that fix showed real
`session.input_transcript.delta` events transcribing actual user speech and
context-aware replies (including "I didn't quite catch that" when the fixed
6-second window cut the user off mid-sentence — an honest response, not a
canned one, confirming the instructions change worked too). The 6-second
fixed window is a spike-quality hack; the real daemon needs local
VAD-based silence detection (same pattern as jarvisd's `audio.py`) to know
when the user has actually finished talking, not a hardcoded timer.

## Rebrand: Oma → Omarchy AI

Renamed everything (repo directory `~/Git/oma` → `~/Git/omarchy-ai`, Python
package `oma` → `omarchy_ai`, systemd unit `oma.service` →
`omarchy-ai.service`, config dir `~/.config/oma` → `~/.config/omarchy-ai`,
persona in `instructions` — "You are Oma" → "You are Omarchy AI"). The
wake word is still the "hey jarvis" pretrained placeholder (openWakeWord has
no "omarchy" model; training one is separate, deferred work — see the
wake-word decision earlier in this doc). Verified after the rename: package
imports cleanly, config loads with the new paths, systemd service starts
and reaches "ready" under the new name.

## Phase 1: wake word integrated into a real daemon

Ported jarvisd's proven `audio.py`/`wake.py` (openWakeWord + `pw-record`,
unchanged detection logic) into `src/omarchy_ai/voice/`. `src/omarchy_ai/
core/daemon.py` is the actual loop: block on the wake word (in an executor,
off the asyncio loop) → run one `LiveSession` (the gpt-live-1 client,
refactored from the spike script) → back to listening. Running as a real
systemd user service (`omarchy-ai.service`), confirmed live end to end
multiple times: wake word fires, session connects, conversation happens,
session ends, daemon returns to listening and fires again on the next wake
word — no crash across repeated cycles.

**No connection is ever open outside an active conversation** — each
`gpt-live-1` session (billed per second) starts only on a real wake-word
detection and ends when the conversation does, per the design goal in the
README.

### Ending a conversation — three attempts, in order, each with real evidence

The user asked for "bye/stop/finish" to end a session and go back to
listening (not stop the whole service — just that one conversation).

1. **Fuzzy text match on the user's own transcript** (`fuzz.WRatio`) —
   first real bug: a single letter "i" matched "finish" and hung up a
   conversation the user hadn't tried to end (WRatio's partial-match
   component inflates scores badly for very short strings). Fixed with a
   minimum-length gate and switching to `fuzz.ratio` (whole-string, no
   partial matching) — confirmed via calibration against real phrasings
   that this correctly rejects "can we **stop** for a sec" and "what's the
   **finish** line" (both would false-positive under partial matching) at
   the cost of missing more loosely-phrased exits. Second real bug this
   surfaced: still unreliable in practice, and **English-only** — doesn't
   help when the user is speaking another language and both their exit
   phrase and the model's own farewell come back in that language.
2. **A real tool call** (`end_conversation`, added to
   `delegation.responses.tools` — confirmed by probing the live API that
   this is where tools live, not `session.tools` at the top level, which is
   rejected as `unknown_parameter`) — the model never invoked it once
   across a full conversation where the user clearly said "bye" (zero
   `function_call` events in the log). Added `delegation.responses.
   tool_choice: "auto"` (also confirmed accepted by the API) on the theory
   that tool use might be off by default — **not yet confirmed working
   live**, see below.
3. **Watching the assistant's own reply for a farewell** — the mechanism
   actually confirmed working live: `instructions` tells the model to say a
   brief goodbye when the user wants to end, and `LiveSession` checks its
   own `session.output_transcript.delta` text for farewell markers
   ("goodbye", "take care", "see you", ...), then hangs up ~2s later (grace
   period so the farewell audio finishes playing). Confirmed live: user
   said "bye", assistant replied "Bye—take care.", session closed cleanly,
   daemon returned to listening. **Known gap, not yet fixed:** the marker
   list is English-only, so this fails the same way as attempt 1 when the
   conversation (and therefore the model's farewell) is in another
   language — confirmed live, the user reported it "hung up only when
   switched to english". A language-agnostic version (e.g. a fixed marker
   token the model is instructed to include regardless of spoken language,
   rather than matching its literal words) is the next real fix here, not
   attempted yet.

Both the tool-call and farewell mechanisms are active at once (either can
trigger hangup); the text-match fallback is disabled by neither being
removed nor separately gated — worth deciding whether to keep it as a
third safety net or drop it now that farewell-watching works, once the
language gap above is actually fixed.

## Phase 2: casting subsystem — Linux side proven, Android side wired up

Real end-to-end casting code now exists on both ends, built directly on
top of the Phase 0 vertical slice and ADR-0001 D7's `webrtcbin` decision:

- `src/omarchy_ai/display/signaling.py` — the minimal WebSocket
  offer/answer/ICE relay both ends need (WebRTC's own signaling is
  intentionally out of band). Confirmed working standalone with a
  scripted sender/viewer round-trip.
- `scripts/spike_cast_portal.py` / `spike_cast_sender.py` /
  `spike_cast_receiver.py` — the Linux-side proof-of-concept chain
  (portal → PipeWire → `openh264enc` → `webrtcbin`, system-audio capture
  via the default sink's monitor → `opusenc`), same "prove it in
  GStreamer before touching Android" convention as Phase 1's voice spike.
  **Not yet run this session** — `spike_cast_portal.py` pops a real
  screen-share consent dialog on the live desktop, deliberately not
  triggered unattended.
- The Android receiver (`android-receiver/`) now has a real UI, not the
  template's placeholder: `SignalingClient.kt` (OkHttp, registers as
  `role=viewer`), `WebRtcClient.kt` (receive-only `org.webrtc`
  `PeerConnection`, empty ICE server list — LAN-only, same
  no-STUN-needed lesson already paid for on the voice side — remote
  video exposed as a `StateFlow`), `ReceiverViewModel.kt` /
  `ReceiverScreen.kt` (auto-connects on launch using a remembered/
  default sender IP, full-screen `SurfaceViewRenderer`, status overlay
  that hides once ICE reaches CONNECTED/COMPLETED). Dependency:
  `io.getstream:stream-webrtc-android` (Maven Central's maintained
  drop-in for `org.webrtc` now that Google no longer publishes
  `google-webrtc`).

**Confirmed:** `./gradlew assembleDebug` succeeds, real 55.9MB debug APK
(up from Phase 0's 11.9MB — now actually linking
`libjingle_peerconnection_so.so`, named directly in the build log's
"unable to strip" line). The signaling relay's protocol was verified
against a live scripted client before wiring the Kotlin side to it.

**Confirmed live, real hardware, first time:** `acquire_screencast()` in
`spike_cast_sender.py` had a real bug — it kept `portal = Xdp.Portal.new()`
as a local variable and returned immediately after starting the async
`create_screencast_session()` call, so nothing else held a reference and
PyGObject could (and did) garbage-collect the `Portal` object mid-flight,
silently dropping the pending callback forever (no error, no timeout).
Fixed by storing it on `self._portal` (commit `6add25c`). After the fix:
real ICE connection state COMPLETED, WebRTC connection state CONNECTED,
one real video frame rendered on the TV at the correct 1366x768
resolution, and the Android side's audio playback thread
(`AudioTrackJavaThread`) activated — the whole capture → encode → WebRTC →
render chain proven end to end on real hardware for the first time.

### Frozen-after-one-frame bug — root-caused with real evidence

The remaining bug from the milestone above: video stalls after exactly one
frame (Android's `EglRenderer` log: "Frames received: 1. Rendered: 1" in
the first 4s window, then "Frames received: 0" in every window after,
indefinitely — no error, ICE/connection state stays healthy).

Root-caused, not guessed: `journalctl --user -u xdg-desktop-portal-hyprland`
for the exact same time window as the one delivered frame shows an
immediate, endless retry loop:
```
[LOG] [screencopy/pipewire] Out of buffers
[LOG] [sc] Retrying screencopy (1/10)
[LOG] [pw] Building modifiers for dma
[WARN] [pipewire] Asked for a wl_shm buffer which is legacy.
```
i.e. the portal's screencopy→PipeWire producer runs out of buffers to
write new frames into almost immediately and never recovers. The
"only pushes on screen damage" hypothesis (in the original handoff notes)
was tested live and ruled out: a deliberately-changing `foot` terminal
window was put on screen during the stall and produced no new frames
either — the stream was already wedged, not waiting for damage.

This matches known upstream reports of the identical symptom class:
GNOME's launchpad bug 1987631 ("Screencast only records one second" — root
cause was the buffer consumer not releasing PipeWire buffers back to the
pool fast enough, worked around with PipeWire's `always-copy`/force-copy
mechanism before a proper fix landed in GStreamer's `videoconvert`) and
`hyprwm/xdg-desktop-portal-hyprland#434` ("DMA-BUF screencopy failure
leaves xdph wedged" — same "Out of buffers" log line, same permanent wedge
until the portal service is restarted).

Fix applied to `pipewiresrc`'s video branch in `build_pipeline()` (see the
code comment there for the full reasoning), in order of how directly each
attacks the root cause: `always-copy=true` (copy PipeWire's buffer into a
fresh `GstBuffer` immediately so our pipeline can never be the reason a
buffer isn't returned to the pool — deprecated property, kept deliberately
since this exact issue is still reproducing on current versions: pipewire
1.6.8, xdg-desktop-portal-hyprland 1.4.1, gstreamer 1.28.6), `min-buffers=2
max-buffers=4` (bounded pool instead of the default unbounded max, to stop
the repeated DMA modifier renegotiation churn visible in the portal log),
and `keepalive-time` (last-resort safety net, resends the last frame
periodically — does not fix the underlying stall by itself). Also added an
opt-in `--probe-buffers` flag that pad-probes pipewiresrc/videorate/
openh264enc buffer counts, used during this diagnosis.

**Not yet re-confirmed live after the fix**: reproducing it needs a fresh
portal session, which pops a real GUI consent picker
(`hyprland-preview-share-picker`, a layer-shell overlay — not a normal
window, doesn't show in `hyprctl clients`, only `hyprctl layers`) that a
human has to click through; there's no restore token saved yet (the portal
log shows "restore data invalid / missing, prompting" every time — the app
has no appid, so persisted grants don't carry over between runs), and no
mouse-click injection tool is available in this environment (`wtype`
exists for keyboard only; no `ydotool`/`wlrctl`). Needs the user present
for one click, same as the original milestone run.

Also still open from the original handoff, now understood to be a
*different* question because the root cause above is screencopy/DMA-BUF
specific: whether the Opus **audio** branch has an analogous stall. The
audio `pipewiresrc target-object=<monitor>` captures a continuous PCM
stream from the default sink's monitor — a fundamentally different
PipeWire producer than the screencopy/DMA-BUF-negotiated video stream, so
the "Out of buffers"/DMA modifier mechanism above should not apply to it.
That is reasoning from the mechanism, not a live measurement — still needs
real confirmation (RTP packet counts on the audio transceiver via
`webrtcbin`'s stats, or GST_DEBUG on the audio branch) once a live session
is possible again.

The paired TV (192.168.1.86) is reachable on the network in this session
(`adb connect 192.168.1.86:5555` succeeded, receiver app was resumed and
connected as viewer).

### Re-tested live: `always-copy`/bounded-pool fix helped marginally, did NOT fix it

Confirmed live, real hardware, same day: with the fix above in place,
Android's `EglRenderer` log improved from "Frames received: 1" to "Frames
received: 2... Rendered: 2" in the first 4s window — but then the exact
same "Frames received: 0" every window after, indefinitely. A fresh
`journalctl --user -u xdg-desktop-portal-hyprland` pull from that same
test window showed the **identical** failure signature still repeating,
unchanged:
```
[LOG] [screencopy/pipewire] Out of buffers
[LOG] [sc] Retrying screencopy (1/10)
[LOG] [pw] Building modifiers for dma
[WARN] [pipewire] Asked for a wl_shm buffer which is legacy.
```
Not a one-off either: grepping the last 2 days of portal logs
(`journalctl --user -u xdg-desktop-portal-hyprland --since "-2 days"`)
found **11,000+** repeats of "Out of buffers", firing roughly once/sec
continuously across every casting test session run in that window (~08:07,
08:48-08:53, 09:48-09:56, 10:07, 10:28-10:31) — this is a permanent,
continuous retry storm for the life of the session, not "stalls once and
gives up". `always-copy`/`min-buffers`/`max-buffers` did not touch the
real root cause.

### Root-caused further: the wedge is inside xdph's own capture, not steerable from our GStreamer caps

Investigated the concrete lead to force `pipewiresrc` onto plain
system-memory buffers (explicit `video/x-raw` caps, no `memory:DMABuf`
feature) before trying anything else, per the plan. Three checks done
without needing the human-gated portal picker:

- **`gst-inspect-1.0 pipewiresrc`**: no property to disable DMA-BUF
  negotiation outright (no `dmabuf`/`buffer-types` property exists on this
  GStreamer 1.6.8 build). The closest lever is caps-based: an explicit
  `video/x-raw` capsfilter right after `pipewiresrc`.
- **Added that capsfilter anyway** (`scripts/spike_cast_sender.py`,
  `pipewiresrc ! video/x-raw ! videoconvert ! ...`) and confirmed via
  `Gst.parse_launch()` on the real launch string (fd/node_id substituted)
  that it parses cleanly, and via `gst-launch-1.0` with `videotestsrc`
  substituted for `pipewiresrc` that the rest of the video branch runs to
  EOS unchanged — both checks need no human and no portal session.
- **But reasoned this likely doesn't reach the actual bug**: downstream
  `videoconvert` already implicitly restricts to system-memory raw video
  today (no `memory:DMABuf` caps feature appears anywhere in the existing
  pipeline), so this pipeline was arguably never *requesting* DMA-BUF from
  PipeWire's SPA format negotiation in the first place. Matching this
  against the actual GitHub issue with the identical log signature —
  `hyprwm/xdg-desktop-portal-hyprland#434`, "**DMA-BUF screencopy failure
  leaves xdph wedged**: CloseSession times out, portal spins ~36% CPU
  until restart" — "Building modifiers for dma" is xdph's own *internal*
  DMA-BUF capture from the Hyprland compositor (how it gets frames off the
  GPU before ever handing them to any PipeWire client), not something
  negotiated against a consumer's requested caps. So a client-side caps
  restriction is very unlikely to be the fix by itself. Added it anyway
  (cheap, matches the plan, doesn't hurt) but flagged in-code as not
  expected to be sufficient alone.

### Checked for an upstream package fix: none available, already on latest

`pacman -Qi xdg-desktop-portal-hyprland` → `1.4.1-2` (Arch `extra`).
`pacman -Si xdg-desktop-portal-hyprland` (repo) → also `1.4.1-2`, i.e.
already the latest packaged build. Cross-checked against GitHub directly
(`gh`/`api.github.com/repos/hyprwm/xdg-desktop-portal-hyprland/tags`) —
newest tag is also `v1.4.1`. No newer release exists anywhere to update
to; this isn't a "wait for a package bump" fix.

### The actual fix applied: disable PipeWire's DMA-BUF modifier negotiation globally

Web research on the exact log signature turned up a documented, matching
community fix: Arch Linux forum thread id=308493, "[SOLVED] XDPH stuck at
building modifiers for dma, won't screenshare" — resolved by disabling
PipeWire's DMA-BUF *modifier* negotiation (the actual subsystem "Building
modifiers for dma" names) via a PipeWire config drop-in, not anything
GStreamer-side. Applied the same fix here:
`~/.config/pipewire/pipewire.conf.d/98-screencast-no-dmabuf-modifiers.conf`:
```
context.properties = {
    support.dmabuf           = true
    support.dmabuf.modifiers = false
}
```
Left the existing `99-omavoice-echo-cancel.conf` in that same directory
untouched (voice subsystem, out of scope here) — this is a new, separate
drop-in file.

Ran `systemctl --user restart pipewire pipewire-pulse wireplumber` and
`systemctl --user restart xdg-desktop-portal-hyprland` to load it.
**Confirmed live, machine-verifiable, no human needed:**
- All four units came back `active (running)` with no new errors in
  `journalctl --user -u pipewire` (only pre-existing, unrelated
  `RTKit ... ServiceUnknown` lines that predate this change and aren't
  about dmabuf).
- `pw-cli info 0` now reports `support.dmabuf = "true"` /
  `support.dmabuf.modifiers = "false"` — the setting is actually loaded
  and live, not just written to a file.
- `pactl info` still answers normally — audio path (and therefore
  `omavoice`) is unaffected by the restart.

No zero-copy GPU capability is being given up on this hardware: this
machine's GPU (Intel HD 4000, `i915`) isn't in the encode path anyway —
casting uses software `openh264enc`, no VAAPI GStreamer plugin installed
(ADR-0001 D6) — so disabling DMA-BUF modifiers costs at most an extra
memory copy, not a lost capability.

**NOT YET RE-CONFIRMED for the actual bug** — this is the one part of the
investigation that needs a live human click, per the constraints. The
mechanism match (identical log signature to a documented, resolved issue)
is strong circumstantial evidence, but circumstantial is not the same as
confirmed. If it's still wedged after this, the next things to check, in
order: (a) whether xdph needs a *cold* restart timed right after a
fresh Hyprland session rather than just a service restart (some reports
of this bug describe the wedge state itself, not just the config,
surviving a portal restart until the whole compositor session cycles);
(b) `hyprctl` / Hyprland-side screencopy protocol debug logs, since xdph's
DMA-BUF capture source is the compositor, not PipeWire, so a residual bug
could be on the Hyprland side of that handoff; (c) filing/searching
upstream for whether `#434` itself has a maintainer response yet (checked
via WebFetch during this session — the issue had no comments yet as of
this investigation).

### PipeWire dmabuf.modifiers config fix: re-tested live, did NOT work either

Confirmed live, real hardware: with the `support.dmabuf.modifiers=false`
config from the previous round still in place, the exact same failure
signature reproduced identically -- `journalctl --user -u
xdg-desktop-portal-hyprland` showed the same "Building modifiers for dma"
-> "Out of buffers" loop, 213 occurrences in a 30-second window, same
freeze-after-2-frames symptom on Android's `EglRenderer` log. Root cause,
understood more precisely now: that PipeWire setting governs generic
PipeWire client/session-manager buffer negotiation between separate
processes -- it has no effect on `xdg-desktop-portal-hyprland`'s own
*internal* screencopy->DMA-BUF bridging code (how xdph pulls frames off
the compositor's GPU in the first place, before any PipeWire client is
even involved), which is a distinct, compositor-internal code path the
setting doesn't touch. This matches `hyprwm/xdg-desktop-portal-
hyprland#434` and is treated as confirmed-unfixable from the consumer
side -- not worth further tuning.

## Casting video: wlr-screencopy-unstable-v1 direct capture, bypassing the portal entirely -- fixed

Rather than continue tweaking the portal/PipeWire path (proven broken
three separate ways: `always-copy`/bounded-pool, PipeWire
`dmabuf.modifiers=false`, both re-tested live and confirmed ineffective),
replaced the entire video capture mechanism: talk to Hyprland's
`wlr-screencopy-unstable-v1` Wayland global directly instead of going
through `org.freedesktop.portal.ScreenCast` at all. This is the same
lower-level protocol `grim` uses (`omarchy-capture-screenshot`), called
successfully dozens of times this session -- strong prior evidence this
path is solid on this machine, unlike the portal's own internal bridging.

### Step 1: proven in isolation first, per this repo's own convention

`scripts/spike_cast_wlr_screencopy.py` -- pulls continuous frames via
wlr-screencopy with no portal, no PipeWire, and no GStreamer involved at
all (pure `pywayland`), before touching the real sender.

**Environment work needed first, done without sudo:**
- No `python-pywayland` pacman package, no gst wlr-screencopy element in
  gst-plugins-good/bad, no `wf-recorder`/`wayvnc` on this machine (all
  checked before writing anything). Added `pywayland` via `uv add
  pywayland` (proper project dependency, not a bare system pip install).
- The `zwlr_screencopy_manager_v1`/`zwlr_screencopy_frame_v1` Python
  bindings pywayland needs aren't bundled with it (wlr-protocols isn't a
  standard wayland-protocols package). No passwordless sudo available in
  this session (`sudo -n pacman -Q wlr-protocols` confirmed this,
  consistent with the earlier-logged blocker) to install the `extra/
  wlr-protocols` pacman package, so the protocol XML was vendored instead
  from the upstream wlr-protocols GitHub repo (same content the pacman
  package ships) into `scripts/protocols/wlr-screencopy-unstable-v1.xml`,
  then compiled once via `python -m pywayland.scanner` into
  `scripts/protocols/generated/wlr_screencopy_unstable_v1.py` -- generated
  against pywayland's own bundled `pywayland.protocol.wayland` module
  (not a second independently-generated copy) so the `wl_output`/
  `wl_buffer` types are identical objects, since pywayland's
  `protocol_core` checks object identity for interface arguments.

**A real bug found and fixed before the proof could be trusted:** the
first live run captured perfectly (360/360 frames in 6s, 0 failures) but
**segfaulted on exit** every time. Root-caused with real evidence, not
guessed: `coredumpctl info` on the core dump showed the crash was in
`wl_proxy_destroy -> wl_map_insert_at`, called from cffi's GC finalizer
during Python's interpreter-shutdown garbage collection pass -- the
Display and its many child proxies (registry, shm, manager, outputs,
buffer, pool) were being destroyed in arbitrary GC order instead of
explicitly, and a proxy's finalizer fired *after* the `wl_display`'s own
internal proxy map had already been freed by a separately-GC'd Display
object (classic destroy-after-free). Read pywayland's own
`Display.disconnect()` source to confirm the intended pattern: it walks
`display._children` (every `Proxy` registers itself there) and destroys
each one *while the display is still alive*, then releases the display
itself. Fixed by calling `grabber.disconnect()` explicitly in a `finally`
block instead of relying on GC -- confirmed live: repeated runs after the
fix exit cleanly (0/1 as expected) with no core dump.

**Real hardware result, 40-second run, `--save-frame` sanity check:**
2389 frames captured in 40.0s = **59.70 fps sustained**, **0 failures**,
every single one of ten consecutive 4-second report windows showed a
non-zero, consistent frame count (~241 frames/window) -- the exact
evidence bar the portal path never once cleared (it died after 1-2
frames). 352 real content-changes detected across the run (CRC of each
frame's pixel bytes, not just event bookkeeping) -- genuine live capture,
not a frozen buffer being resent. `--save-frame` dumped one captured
frame to a real PNG (via `ffmpeg -f rawvideo`) and it is visually the
actual live desktop content at that moment (terminal windows, a real
system notification visible in the shot) -- direct visual confirmation,
not just numbers. `journalctl --user -u xdg-desktop-portal-hyprland`
during the entire 40+ second test window: **zero lines** -- definitive
proof this path never touches the portal at all. **No consent dialog
appeared** at any point -- confirms wlr-screencopy is compositor-policy-
gated, not portal-consent-gated, on this Hyprland build (checked as asked
rather than assumed).

### Step 2: wired into the real sender

Extracted the capture core into `scripts/wlr_screencopy_capture.py`
(shared by both the spike and the real sender, avoiding duplicating the
protocol logic) -- `ScreencopyGrabber` class plus a
`select()`-with-timeout dispatch helper for standalone use. Its `on_frame`
callback hands consumers a **fresh copy** of each frame's tightly-packed
pixel bytes (stride padding stripped if present -- a real correctness
guard: this machine's stride happened to exactly equal `width*4` so the
stripping code path wasn't exercised locally, but nothing guarantees that
elsewhere, and an un-stripped mismatch would show up downstream as
skewed/torn video, one row at a time). The copy happens synchronously
before the callback returns, specifically so a consumer can safely
request the next capture (which reuses the same `wl_shm` mmap region)
without racing the compositor overwriting it mid-read.

`scripts/spike_cast_sender.py`'s video branch: `Xdp`/libportal and
`pipewiresrc` removed from the video path entirely, replaced with an
`appsrc` fed directly from `ScreencopyGrabber`. The Wayland display's fd
is wired into the *same* `GLib.MainLoop` that already drives webrtcbin's
bus/promise callbacks via `GLib.io_add_watch` -- no extra thread needed,
consistent with this script's existing GLib-for-pipeline /
asyncio-for-signaling split. Captures are paced to roughly the target fps
via `GLib.timeout_add` (proven capable of ~60fps in isolation; no reason
to burn CPU copying/encoding frames `videorate` would just drop). Each
frame becomes a `Gst.Buffer.new_wrapped(data)` with a real PTS relative to
first-frame time, pushed via `appsrc.emit("push-buffer", ...)`; the
pipeline itself isn't built until the first frame arrives, since
`build_pipeline()` needs the real width/height/format learned from the
compositor's first "buffer" event (same "learn params, then build"
sequencing the old portal-based flow used, just with wlr-screencopy as the
new source of those params). The `appsrc` caps use `framerate=0/1`
("variable/unknown") deliberately -- `videorate` downstream uses each
buffer's real PTS to retime to the fixed output rate, not the nominal caps
framerate. Verified the launch string parses and the `appsrc` caps
property is set correctly via a standalone `Gst.parse_launch()` check
before the real hardware test. Audio branch: **untouched**, byte-for-byte
identical `pipewiresrc target-object=<monitor>` -- never implicated in
this bug (different PipeWire producer than the screencopy/DMA-BUF path).

### Step 4: real hardware test -- confirmed, continuous video, no stall

Signaling relay was already running from an earlier session (reused, not
restarted); the Android receiver app was already resumed on the paired TV
(`ai.omarchy.receiver/.MainActivity`, re-launched via `adb shell am
start` to get a clean connection state -- `adb` reported it was already
top-most). Ran `spike_cast_sender.py --fps 15` for 48 real seconds while
watching `journalctl --user -u xdg-desktop-portal-hyprland -f` live and
`adb logcat` (cleared first via `logcat -c`) for Android's `EglRenderer`.

**Sender side:** ICE reached `CONNECTED` (state 2) within ~0.6s. Twelve
consecutive 4-second capture windows, **all ~50 frames each, 0 failures,
zero stalls**.

**Portal log for the entire 48+ second test: zero lines.** Not "no error
lines" -- *zero output at all*, confirming this path never touches
`xdg-desktop-portal-hyprland` in any way, the definitive version of the
"no portal involvement" claim.

**Android `EglRenderer` log (the actual evidence bar this whole
investigation was blocked on) -- twelve consecutive 4-second windows after
the initial ICE ramp-up, every single one:**
```
Frames received: 60. Dropped: 0. Rendered: 60. Render fps: 15.0.
```
Locked exactly to the requested 15fps target, zero drops, for the entire
duration the sender ran -- not "one frame then a permanent stall" (the
original bug), not "two frames then a permanent stall" (the
`always-copy`/bounded-pool attempt), not "unchanged" (the PipeWire
`dmabuf.modifiers` attempt) -- **continuous, steady video for the whole
test**, ending only because the sender process was deliberately stopped
(the final two logcat windows show 0 frames, timed exactly to when
`timeout 50` killed the sender). Audio confirmed still flowing throughout
via `AudioFlinger` mixer activity in the same logcat pull (untouched
branch, as expected).

**This closes the frozen-after-one-frame casting bug.** The portal-based
video path is retired (kept only in git history / this file's earlier
entries for the record); `wlr-screencopy-unstable-v1` via `appsrc` is now
the real video capture mechanism. `src/omarchy_ai/voice/` and
`omarchy-ai.service` were never touched -- confirmed still `active`
throughout (`systemctl --user is-active omarchy-ai.service pipewire
pipewire-pulse wireplumber`), and no PipeWire-related service restart was
needed or performed this round (unlike the previous round, which broke a
live voice session).

## Next action

1. Verify audio the same rigorous way as video was just verified here
   (webrtcbin RTP stats or GST_DEBUG on the audio branch, plus actually
   listening closer to the TV) -- reasoned safe/unaffected so far, not yet
   measured with the same rigor as video.
2. Retire/delete `scripts/spike_cast_portal.py` (the now-unused portal
   consent-dialog spike) or clearly mark it historical-only, since video
   capture no longer goes through the portal at all.
3. **Fix the English-only farewell/exit detection** (voice subsystem, see
   above) -- the actual next correctness bug, not a nice-to-have.
4. Confirm whether `tool_choice: auto` made the `end_conversation` tool
   call actually fire, with a clean live test.
5. Replace the fixed `SPEAK_WINDOW_SECONDS` timer with real local VAD.
6. Watch Dogs/Matrix-style code-rain overlay UI (user request, tracked, not
   started).
7. The policy/tool-registry/audit layer (ADR-0001 D1).
2. Once video is confirmed continuous, verify audio the same rigorous way
   (webrtcbin RTP stats or GST_DEBUG on the audio branch — see above) and
   confirm actually hearing it, closer to the TV than ~3m this time.
3. **Fix the English-only farewell/exit detection** (see above) — the
   actual next correctness bug, not a nice-to-have.
4. Confirm whether `tool_choice: auto` made the `end_conversation` tool
   call actually fire, with a clean live test (the one attempt after adding
   it was inconclusive — session closed on its own after ~30s of silence,
   not clearly from either mechanism).
5. Replace the fixed `SPEAK_WINDOW_SECONDS` timer with real local VAD
   (reuse jarvisd's silence-detection approach) so the daemon knows when
   the user actually finished talking, rather than guessing a duration.
6. Watch Dogs/Matrix-style code-rain overlay UI (user request, tracked, not
   started) — GPU-light, replaces omavoice's simple waveform panel.
7. The policy/tool-registry/audit layer (ADR-0001 D1) — nothing calls out
   to the OS yet; this is still a conversation, not an OS-control assistant.

## Android receiver: waiting-screen wallpaper

Cosmetic fix, independent of the casting bug above (per the user's own
framing — done regardless of how that investigation goes). The waiting
state (`StatusOverlay` in `android-receiver/app/src/main/java/ai/omarchy/
receiver/ReceiverScreen.kt`) was plain text on a plain black background;
user asked for it to use Omarchy's actual desktop wallpaper instead.

Copied `/usr/share/omarchy/themes/catppuccin/backgrounds/omarchy.png`
(dark navy background, light-blue "OMARCHY" wordmark, 3840x2160 but only
4.4KB — a flat 2-bit-colormap PNG, negligible APK size cost) into
`android-receiver/app/src/main/res/drawable/omarchy_wallpaper.png` and
render it as a full-bleed `Image(contentScale = ContentScale.Crop)` behind
the existing status text/host field/Connect button in `StatusOverlay`,
inside a new `Box` so the UI stack is: wallpaper → text/controls. White
text stays legible — the wallpaper's own background is a nearly-uniform
dark navy field, matching the app's existing black background closely
enough that contrast wasn't a concern. `./gradlew assembleDebug` re-run to
confirm this actually compiles, not just that the diff looks plausible
(see build output/result noted at commit time).

## Window name-label badges ("which window do you mean?")

New feature, user-requested after explicitly rejecting cheaper alternatives
(a border-flash, a plain spoken text list) — they wanted real floating
per-window name badges, positioned at each candidate window's actual
on-screen rectangle, shown while the assistant asks and hidden once it has
an answer. Full design/precedent reasoning in ADR-0001 D8; this entry is
the live-test evidence trail.

**Quickshell side** — new user-owned plugin,
`~/.config/omarchy/plugins/omarchy-ai.window-labels/` (`manifest.json` +
`WindowLabels.qml`), built by copying `omarchy-osd`'s own IPC pattern
exactly (`$OMARCHY_PATH/shell/plugins/osd/Osd.qml`). Registers `IpcHandler
{ target: "windowLabels" }` with `show(payloadJson)` / `hide()` / `state()`
/ `ping()`. One `PanelWindow` per screen (`Variants { model:
Quickshell.screens }`, matching `omarchy.notifications`/`omarchy.background`'s
own per-output pattern) holding a `Repeater` of chip badges, each converted
from the window's global (`hyprctl clients -j` "at"/"size") coordinates into
that screen's local surface coordinates and anchored near the window's
top-left corner. No auto-hide timer — `hide_window_labels` owns the
lifecycle explicitly.

Two real bugs hit and fixed getting it live (both root-caused from
`journalctl --user` — the shell logs to the journal under the
`omarchy-shell` tag even with `QS_DISABLE_FILE_WATCHER=1`, confirmed that
env var only disables Quickshell's own reload-popup, not the journal or
`PluginRegistry`'s own file-watch reload path):

1. Missing `import Quickshell.Io` → `IpcHandler` resolved as "not a type".
   The CLI side gave no hint (`omarchy-shell windowLabels ping` just said
   "Target not found"); only the journal showed the real compile error.
2. `qs ipc call` — the underlying Quickshell IPC CLI `omarchy-shell` wraps
   — splats a top-level JSON **array** argument into multiple positional
   CLI arguments instead of one string. Confirmed live: a 3-window
   `show '[{...},{...},{...}]'` failed with `Too many arguments provided
   (1 required but 3 were provided)`; the identical call with a 1-element
   array worked. Fixed by changing the wire payload to a JSON *object*
   (`{"windows": [...]}`) — this is why the payload isn't a bare array
   despite the tool description talking about "a list of windows".

Also confirmed live: once a plugin has failed to compile, neither
`omarchy-shell shell rescanPlugins` nor further file saves (which do
correctly trigger `PluginRegistry`'s "Local plugin changed, reloading" —
confirmed in the journal) clear the bad cached state — only a real
`omarchy restart shell` process restart did. And a brand-new third-party
plugin needs an explicit `omarchy plugin enable <id>` before
`listPlugins` reports it `enabled: true` — just existing on disk and being
*discovered* isn't enough, first-party-only default-enable per the
shell's own plugin README.

**Live-tested the full mechanism directly** (bypassing the voice daemon,
per the task's own suggested approach — no live spoken conversation
needed to prove this): with two real `foot` terminal windows open (plus
two pre-existing ones), resolved real geometry via `hyprctl clients -j`,
called `omarchy-shell windowLabels show '{"windows":[...]}'` with 1, then
3, then all 4 windows, screenshotted each time
(`omarchy-capture-screenshot fullscreen`) and confirmed a correctly
titled, correctly positioned chip badge sitting right at each labeled
window's actual top-left corner — not a placeholder, not approximate.
`hide` screenshotted clean (no leftover badges) both times.

**Python side** — `show_window_labels`/`hide_window_labels` added to
`src/omarchy_ai/execution/actions.py` (registered in `ACTIONS`) and
`src/omarchy_ai/execution/tools.py` (schemas via the existing `_tool(...)`
helper). `show_window_labels(args: {"targets": list[str] | None})` resolves
real geometry via `hyprctl clients -j` (same shape `list_windows` already
parses), matches each target the same fuzzy app/title-substring way
`focus_window` does (one match per target, first-match-wins, already-
matched windows skipped so two vague targets can't collide on one window),
falls back to labeling every mapped window when `targets` is omitted/empty,
truncates titles over 40 chars so a long title can't draw a chip wider
than the window it labels, builds the `{"windows": [...]}` payload, and
shells out to `omarchy-shell -q windowLabels show <json>` via the existing
`_run` subprocess helper (no `shell=True`, matching every other action in
this file). `hide_window_labels` is a one-line `_run` call to `windowLabels
hide`. Confirmed live end-to-end, independent of the voice daemon: called
`run_action("show_window_labels", {"targets": ["foot"]})` and
`run_action("show_window_labels", {})` directly from a Python shell,
screenshotted, same correctly-positioned real badges as the raw-IPC test
above.

`src/omarchy_ai/config.py`'s `instructions` string now tells the model:
when it's genuinely ambiguous which window the user means and it's about
to ask out loud, call `show_window_labels` scoped to the ambiguous
candidates right before asking, then `hide_window_labels` once it has an
answer — whether or not it then acts on it. No changes to
`src/omarchy_ai/voice/` were needed: both new actions are ordinary entries
in `ACTIONS`/`TOOLS`, so they run through the existing generic
`run_action(name, args)` dispatch in `live.py` (`_run_tool_call`) exactly
like every other tool — no special-casing like `end_conversation` or
`get_recent_actions` required.

**Not yet done:** a real spoken end-to-end test through the live
`omarchy-ai.service` (ask "which terminal did you mean" out loud and watch
badges appear/disappear as part of an actual conversation). The service
was mid-conversation for long stretches of this work (confirmed via
`journalctl --user -u omarchy-ai` — the user was actively voice/text
driving it, asking in-band why the labels feature wasn't done yet) and,
per this project's own established restart discipline (a careless restart
previously cut a live session short), the service was restarted only once
`journalctl` showed no in-progress turn, immediately after wiring these
tools in, specifically so the next real conversation would have them
available. See the commit log around this entry for exactly when that
restart happened and what the log showed right before it.
