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
6. ~~Watch Dogs/Matrix-style code-rain overlay UI~~ — done, see "Watch Dogs
   overlay" below.
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
6. ~~**Future settings idea (explicitly deferred, not started):** an
   ASCII/unicode audio-reactive visualizer...~~ **Done** — see "Settings
   menu + ASCII visualizer" below. The exact sketched pattern (throttled,
   non-blocking `watchdog.level()`, `Popen`-dispatched from
   `_play_remote_audio`) was picked back up essentially unchanged, now
   gated by the real settings panel's display-mode picker.
7. ~~**Also for that future settings menu:** a plain on/off toggle for the
   Watch Dogs overlay itself...~~ **Done** — same section below;
   `watchdog_enabled` gates the overlay, `watchdog_display_mode` picks
   feed/visualizer/both.
6. ~~Watch Dogs/Matrix-style code-rain overlay UI (user request, tracked,
   not started) — GPU-light, replaces omavoice's simple waveform panel.~~
   Done — see "Watch Dogs overlay" below for the live-test evidence trail.
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

## Watch Dogs overlay

The last remaining "not started" line in this file's own next-actions
list, tracked since early in the project, now done. User's own framing on
starting it, spoken to the assistant mid-conversation and captured verbatim
in `journalctl`: *"We'll work on the watchdog UI now."* Full design
reasoning is in `docs/ADR-0001-architecture.md` D9; this entry is the
live-test evidence trail, same convention as the window-labels entry above.

**Quickshell side** — new user-owned plugin,
`~/.config/omarchy/plugins/omarchy-ai.watchdog/` (`manifest.json` +
`Watchdog.qml`), built by copying `omarchy-ai.window-labels`'s own IPC
pattern (itself copied from `omarchy-osd`) — see D9 for the differences
this plugin needed on top of that shape (a stream of `event` calls instead
of one-shot show/hide, the hacking-terminal color/font skew, the `state`
QtQuick-reserved-name gotcha). Registered via `omarchy plugin enable
omarchy-ai.watchdog`, confirmed in `~/.config/omarchy/shell.json`'s
top-level `plugins[]` array alongside `omarchy-ai.window-labels`.

**Live-tested the mechanism directly**, same approach the window-labels
entry used and for the same reason — this agent has no way to actually
speak a wake word, so per the task's own explicit fallback, verification
went through direct IPC plus screenshots:

1. `omarchy plugin validate ~/.config/omarchy/plugins/omarchy-ai.watchdog`
   passed (exit 0). `omarchy-shell -q shell rescanPlugins` then
   `omarchy-shell watchdog ping` → `ok` and `omarchy-shell watchdog state`
   → `closed` confirmed the QML actually compiled — no repeat of the
   window-labels round's "not a type" failure mode (the journal showed only
   normal "Local plugin changed, reloading" lines, no QML errors).
2. Raw IPC end-to-end: `watchdog start '{}'`, then a sequence of `event`
   calls (`tool_call`/`tool_result` with `ok`/`err` tones, `state`
   transitions through connecting/listening/thinking/speaking),
   screenshotted with `omarchy-capture-screenshot fullscreen` after each
   batch. Confirmed: the panel renders as a compact bottom-right corner
   card (not a full-screen takeover), state header shows the current state
   in the right color with a pulsing dot on busy states
   (connecting/thinking) and a steady dot once settled
   (listening/speaking), and the feed shows real-looking lines in the exact
   format the task's own spec used as examples — `> execute_command
   ("Browser")` and `> volume_up() -> ok` appeared verbatim. A deliberately
   long window title and a deliberately long error message both truncated
   correctly ("…") instead of overflowing the card. `watchdog stop`
   screenshotted clean — no leftover overlay, `state` back to `closed`.
3. Python-level test, bypassing raw IPC: imported
   `omarchy_ai.voice.watchdog` directly in the project's own venv and
   called `start()`/`state()`/`tool_call()`/`tool_result()`/`stop()` with
   realistic arguments (including a failing tool result with a message),
   confirmed no exceptions and the same correct rendering via another
   screenshot — this is the same code path `live.py` calls, just invoked
   outside a real conversation.

**Not a fully spoken end-to-end test** (wake word → real conversation →
real tool calls → hangup), for the same structural reason as the
window-labels round: no way for this agent to produce real speech.
Additional real evidence beyond the direct-IPC tests above, though:
another agent was independently working in this same repo concurrently
(unrelated wake-word/ONNX model work — visible in `git status` as
uncommitted changes to `config.py`/`core/daemon.py`/`execution/
{actions,tools}.py`/`voice/wake.py`, none of which this task touched) and
restarted `omarchy-ai.service` on their own schedule mid-session. That
restart picked up this feature's `live.py`/`watchdog.py` changes too (same
file, same working tree), and the service came back up clean —
`journalctl` showed the normal ONNX-provider warning, "loaded wake word
model(s)", "omarchy-ai ready — say any of: ...", no traceback, no
crash-loop. That's real confirmation the integration (the new `from . import
watchdog` in `live.py`, the four call sites) loads and runs inside the
actual daemon process, not just in isolation. This agent deliberately did
**not** trigger that restart itself, and did not restart the service again
afterward — per this project's own restart discipline, and specifically
because a second concurrent process was already mid-task against the same
live service; forcing another restart on top of that would have risked
stepping on unrelated, unverified work rather than this feature's own.

**Python side** — `src/omarchy_ai/voice/watchdog.py` (new file) wraps the
IPC calls; `src/omarchy_ai/voice/live.py` gained one import and four call
sites (session connect, tool-call start/end in `_run_tool_call`, the two
transcript-delta branches in `_on_data_message`, and the `finally:` block
in `run()`). No changes to `execution/actions.py`, `execution/tools.py`, or
`config.py`'s `instructions` — unlike window-labels, this feature is driven
entirely by the daemon's own lifecycle, not a tool the model chooses to
call, so there's nothing for the model to be told about it.

## Casting: auto-discovery, TV disambiguation, assisted receiver install

Extends the same-day `start_casting`/`stop_casting` (hardcoded
`_TV_ADB_ADDR = "192.168.1.86:5555"`, confirmed live: real 15fps, 60/60
frames, 0 dropped) with real mDNS auto-discovery, a spoken "which TV?"
flow, and a guided new-TV install. Full design reasoning in
`docs/ADR-0001-architecture.md` D10; this is the live-evidence trail.

**New file `src/omarchy_ai/display/discovery.py`** wraps `avahi-browse -r
-p -t <service>`, parsing its resolved (`=`) rows including avahi's
decimal-escape format (`\032` -> space, confirmed against real captured
output: `Philips\0324K\032A1` -> `Philips 4K A1`). Three functions:
`discover_androidtv_devices()` (`_androidtvremote2._tcp`, deduped IPv4-
preferred), `discover_adb_tls_pairing()`/`discover_adb_tls_connect()`
(the two Wireless-Debugging mDNS services, for new-TV setup). Real timing
measured directly on this network before picking the subprocess timeout:
`avahi-browse -r -p -t _androidtvremote2._tcp` takes ~5s wall-clock
whenever an unresolvable instance is present (one real device on this
network, "Philips 4K A1", intermittently fails to resolve -- confirmed
via the literal `avahi-browse` stderr: "Failed to resolve service
'Philips 4K A1' ... Timeout reached") -- `_BROWSE_TIMEOUT = 8.0` set above
that measured cost, not guessed. Runs happily off the asyncio event loop
already (all tool execution goes through `loop.run_in_executor`, per the
Phase 1 "blocking the event loop" fix above), so this real multi-second
mDNS latency never stalls the voice session.

**Confirmed live against the real network** (not mocked): `avahi-browse
-a -t` surfaced `_androidtvremote2._tcp` (three real devices: "Living
Room TV" 192.168.1.191, "Idol TV" 192.168.1.203, "Philips 4K A1"
unresolvable) and, separately, `_googlecast._tcp` (a broader set including
plain Chromecasts and non-Android-TV devices -- concretely, "Nest Audio",
"Google Home", and "Google Nest Hub" have no `_androidtvremote2._tcp`
counterpart at all, real proof they're not adb-installable cast targets).
Decision: only `_androidtvremote2._tcp` is browsed for cast targets --
full reasoning in ADR-0001 D10. `_adb-tls-connect._tcp`/
`_adb-tls-pairing._tcp` (Wireless Debugging's own services) both returned
zero results, confirmed directly, matching that no TV on this network
currently has Wireless debugging toggled on.

**`execution/actions.py`**: `_resolve_cast_target(target)` is the new
core logic -- discovers real devices, matches a given name substring
(1 match -> use it, >1 -> `ActionResult(False, ...)` naming the
candidates so the model can ask, 0 matches -> honest "not found,
available: ..." unless the target parses as a raw IP, in which case it's
used directly), or with no target picks the sole discovered device, asks
via `ActionResult(False, ...)` if there's more than one, and falls back to
the always-known `_TV_ADB_ADDR` if discovery finds nothing at all (so a
bad mDNS moment degrades to today's already-proven behavior rather than
failing outright). `start_casting(args)` now takes an optional `target`,
resolves it through this function first, and uses the resolved
`tv_addr` (not the old hardcoded constant) for `adb connect` and for a
new `-s tv_addr` on the `am start` launch call (targets the right device
if more than one TV is ever adb-connected at once). New `list_cast_targets`
action returns the same discovery as JSON, with the same last-known-TV
fallback if discovery is empty. New `install_receiver_on_tv` action
(detailed below) plus its `_ensure_receiver_apk_built()`/
`_pending_pair_target` support.

**Live-tested directly** (`run_action(...)`/`_resolve_cast_target(...)`
called from a Python shell in this project's own venv, bypassing voice --
same verification approach this repo has used for every visual/IPC
feature that can't be triggered by a spoken wake word):
- `list_cast_targets({})` -> `[{"name": "Living Room TV", "address":
  "192.168.1.191"}, {"name": "Idol TV", "address": "192.168.1.203"}]` --
  real discovered devices, not mocked, "Philips 4K A1" correctly absent
  (it never resolves to an address).
- `_resolve_cast_target(None)` with 2 real devices discovered -> correct
  ambiguous-refusal `ActionResult(False, "found 2 TVs on the network
  (Living Room TV, Idol TV) -- call list_cast_targets and ask...")`.
- `_resolve_cast_target("idol")` -> `("192.168.1.203:5555", None)` --
  correct single-match resolution by name.
- `_resolve_cast_target("nonexistent tv")` -> correct honest failure
  naming what IS available, not a silent fallback.
- `_resolve_cast_target("192.168.1.86")` and `("192.168.1.86:5555")` --
  both correctly pass the already-paired TV's raw IP straight through
  (this device isn't `_androidtvremote2._tcp`-discoverable, proving the
  raw-IP path works independently of discovery).
- `install_receiver_on_tv({})` -> correctly detected the already-built
  APK (55,940,636 bytes, unchanged from Phase 2) and, since
  `_adb-tls-pairing._tcp` is empty right now, returned the full narrated
  Developer-options/Wireless-debugging walkthrough text.
- `install_receiver_on_tv({"pairing_code": "123456"})` with no pairing
  target cached or discoverable -> correct honest failure ("no single TV
  is currently showing a pairing screen..."), not a crash or a guess.

**Not verified live**: the actual `adb pair`/`adb connect`/`adb install`
sequence in `install_receiver_on_tv` -- no unpaired TV was available this
session (every real TV already set up some other way, confirmed via the
empty `_adb-tls-pairing._tcp` discovery above). Reasoned through against
real `adb`/mDNS semantics and this project's own already-proven
`adb connect`/`adb install` calls elsewhere in this file, not exercised
end to end. Also not re-run: a full `start_casting(target=...)` live
cast -- the earlier-today `start_casting({})` call (`journalctl`,
15:08:11-15:08:14) left a real `spike_cast_sender.py` process (pid
190420) actively streaming to the paired TV throughout this work
(confirmed via `ss` showing live ESTABLISHED websocket connections, 8+
minutes elapsed at the time of checking), and the signaling relay is
deliberately single-sender -- starting a second one to test `target=`
would have kicked that live session off mid-stream. The only new code in
`start_casting`'s path (`_resolve_cast_target`) was verified independently
instead, as above; everything after target resolution is the same
sequence already proven live earlier today.

**A sharper version of this project's restart discipline, found while
checking whether it was safe to restart `omarchy-ai.service` to pick up
these changes**: `systemctl --user show omarchy-ai -p KillMode` is
`control-group`, and the live `spike_cast_sender.py` process above (pid
190420) is a member of that same cgroup
(`/user.slice/.../omarchy-ai.service`, confirmed via `/proc/190420/
cgroup`) despite being spawned with `start_new_session=True`. A service
restart right now would kill the live cast too, not just interrupt a
conversation -- previously this repo's restart discipline only accounted
for the latter. **Deliberately not restarted this session** -- the daemon
is `active`, idle (no in-progress conversation per `journalctl`), but
still driving a real live cast; these changes aren't loaded into the
running process yet. Restart once the current cast test is done, same
"only between conversations" discipline, now also "only when nothing is
actively streaming."

### Next action

1. Restart `omarchy-ai.service` once the live cast test above has wrapped
   up, so these tools are actually reachable by voice, then do a real
   spoken end-to-end test: "what TVs are available", "cast to the
   [name]", and (once a genuinely new/factory-reset TV is available)
   `install_receiver_on_tv`'s full pairing flow.
2. If `install_receiver_on_tv` gets a real unpaired TV to test against,
   confirm the `adb pair` -> `_adb-tls-connect._tcp` re-discovery ->
   `adb connect` -> `adb install -r` sequence end to end -- the one part
   of this feature reasoned through but not yet proven.
3. Consider persisting the last-used cast target (survives a daemon
   restart) instead of only the always-available `_TV_ADB_ADDR` fallback,
   once there's real multi-TV usage to learn a preference from.

## Settings menu + ASCII visualizer

Closes the two backlog items above (both tracked since the Watch Dogs
overlay's own "Next action" list — an on/off toggle for the overlay, and
the ASCII/unicode audio-reactive visualizer display mode, both explicitly
deferred earlier pending "a real settings system"). Two parts: a real
settings panel reachable from the bar, and the visualizer itself wired in
as a selectable display mode gated by it — not hardcoded on, per the
user's own explicit reason for reverting the earlier visualizer sketch.

### Part 1: `omarchy-ai.settings` bar panel

New user-owned plugin, `~/.config/omarchy/plugins/omarchy-ai.settings/`
(`manifest.json` + `Panel.qml`), but a different shape from the three
existing `omarchy-ai.*` plugins (window-labels/watchdog, both `IpcHandler`-
only overlays with no bar icon of their own). This one is a first-party-
style **bar-widget panel**: `kinds: ["bar-widget"]`, `entryPoints.barWidget:
"Panel.qml"`, built on `qs.Ui`'s `Panel` base component + `BarIconButton` +
`KeyboardPanel` — the exact shape `$OMARCHY_PATH/shell/plugins/panels/
power/Panel.qml` and `.../audio/Panel.qml` use (icon in the bar, click
opens a popup card anchored to it, `Panel` base's own `IpcHandler` gives
`open`/`close`/`show`/`hide`/`toggle` for free). Placed on the bar via
`omarchy plugin enable omarchy-ai.settings` + `omarchy bar put
omarchy-ai.settings --after omarchy.power` then `omarchy bar move
omarchy-ai.settings --section right` (a gear icon, ``, next to the
tray/audio/power icons — confirmed correctly positioned via screenshot).

**No Python service lives inside Quickshell**, unlike a first-party panel
that can query PipeWire/UPower services directly — reading/writing
`~/.config/omarchy-ai/config.yaml` needs `src/omarchy_ai/config.py`'s real
merge-over-defaults logic (duplicating it in QML/JS would drift out of
sync with the actual daemon), and the restart-safety check needs real
`journalctl`/`systemctl` access. So every control in the panel shells out
to a new `src/omarchy_ai/cli/settings.py` (installed as the
`omarchy-ai-settings` console script in this project's own uv venv) via a
Quickshell `Process` + `StdioCollector`, the same "shell out to a real
interpreter" pattern `$OMARCHY_PATH/shell/plugins/panels/dropbox/status.py`
already uses inside the shell itself. Always emits one line of JSON on
stdout and exits 0 regardless of outcome (`{"error": "..."}` on failure) —
deliberate, since a QML `StdioCollector` has one clean path for "read what
came back" and checking a JSON field is simpler than threading exit codes
through a `Process.onExited` callback.

`settings.py` commands:
- `get` — current values for a *curated whitelist* of `Config` fields
  (`custom_wake_model_paths`, `wake_threshold`, `watchdog_enabled`,
  `watchdog_display_mode`, `voice` — not every dataclass field; internal
  plumbing like `api_key_path`/`context_max_chars`/`instructions` stays
  off the whitelist entirely, matching the task's own "use judgment" ask)
  plus the live wake-model directory listing and voice options, for the
  panel to render itself from real data rather than anything hardcoded.
- `set <key> <json-value>` — validates (type/range/enum per field), merges
  into `config.yaml`, and **only writes keys that differ from `Config()`'s
  own defaults** (matching `config.py`'s own module docstring), removing a
  key from the file entirely if it's set back to the default. Wrote the
  file, read it back, and reverted every test value during this session —
  confirmed `~/.config/omarchy-ai/config.yaml` ends up byte-for-byte back
  to its original comment-only content.
- `restart-status` / `restart` — replays `daemon.py`'s own two log lines
  (`"wake word detected, starting live session"` / `"session ended, back
  to listening"`) over the last 300 lines of `journalctl --user -u
  omarchy-ai`, in order, to determine whether a conversation is currently
  in progress — the exact same discipline this whole project's agents have
  applied by hand all day, now codified so the settings panel can apply it
  itself before restarting. `restart` refuses (returns `{"restarted":
  false, "reason": ...}`, no `systemctl` call made) if busy.

**Design decision — who triggers the restart:** chose "the panel's Restart
button calls `restart`, which internally refuses if a conversation is
active" over "just tell the user to restart manually." Config changes are
otherwise inert until a restart (the daemon only calls `load_config()` once,
in `OmaDaemon.__init__` — confirmed by reading `core/daemon.py`, not
assumed), so *some* restart step is unavoidable either way; automating it
behind the same safety check this project already trusts is less
friction than a manual step without meaningfully more risk, and the panel
shows a "changes pending" hint plus the restart reason on refusal so the
user isn't left guessing either way.

**A real footgun caught during validation, not by inspection alone:**
`wake.py`'s `_resolve_model_paths` falls back to a bundled-openWakeWord
pretrained-model lookup when `custom_wake_model_paths` is empty — and
`"omachy"` isn't a bundled model, so an empty list would have crashed the
daemon on its next restart (`ValueError: wake word 'omachy' not found
among bundled models`). Confirmed live: `settings.py set
custom_wake_model_paths '[]'` is rejected (`{"error": "at least one wake
model must stay active"}`) rather than accepted and only failing later
inside the daemon. The `MultiSelect` control's own local state (it mutates
`values` directly before the backend call resolves) is force-resynced to
the server-confirmed list in every response callback, success or reject —
otherwise a rejected "deselect the last model" click would leave the
checkbox visually unchecked while the backend silently ignored it.

**Verified live, screenshots at each step** (`omarchy-capture-screenshot
fullscreen save`, per this project's own two-strikes-already lesson that
"no compile error" isn't proof of a working feature):
1. `omarchy plugin validate` passed; gear icon confirmed in the bar's
   right section (screenshot).
2. `omarchy-shell -q omarchy-ai.settings show` — panel opened and rendered
   real data on first paint: the actual three installed wake models
   (`omachy, omri, roni`, not hardcoded), `wake_threshold 0.50`, the
   Watch Dogs toggle correctly ON, "Text feed" display mode correctly
   selected, voice `marin` — proving the `Process`/`StdioCollector`/JSON
   round trip and every property binding actually work, not just render
   placeholder text (screenshot).
3. Drove a `set watchdog_display_mode "visualizer"` directly (the same
   call the "ASCII visualizer" segmented-button click makes), closed and
   reopened the panel to force a fresh fetch, and confirmed via a cropped
   screenshot that the "ASCII visualizer" segment now renders as the
   selected chip — proving the write-then-read-back cycle end to end
   through the real config file, not just the CLI's own stdout.
4. Reverted every test value; `restart-status` confirmed safe
   (`{"busy": false, ...}`) and `omarchy-ai.service` was restarted for
   real once (see below) to load all of this session's code changes.

### Part 2: ASCII/unicode audio visualizer, as a Watch Dogs display mode

Picked the deliberately-reverted sketch back up almost exactly as it was
described in this file's own backlog entry, now gated by
`watchdog_display_mode` instead of being unconditional.

**Python side** — `Config` gained `watchdog_enabled: bool = True` and
`watchdog_display_mode: str = "feed"` (`"feed" | "visualizer" | "both"`).
`LiveSession.__init__` reads both once per session into
`self._watchdog_on`/`self._watchdog_wants_levels` (config only changes on
a daemon restart anyway, so there's no reason to re-read per event).
Every existing `watchdog.*` call site in `live.py` (session-connect
`start()`, the four `state()` calls, `tool_call`/`tool_result` in
`_run_tool_call`, `stop()` in the `finally:` block) is now gated behind
`self._watchdog_on`, not just the `start()` call the task specifically
called out — disabling the overlay now means the daemon makes zero
`omarchy-shell` IPC calls for it all session, not just "never becomes
visible."

`watchdog.start()` now takes `display_mode` and sends it as
`{"displayMode": ...}` — a session-start-time decision, matching how
config itself only changes on restart.

New `watchdog.level(value)` in `watchdog.py`, called from `live.py`'s
`_play_remote_audio` — the real-time audio playback loop — right after
`boosted` (the actual output PCM) is computed, throttled to 10Hz (via a
`last_level_dispatch`/`level_interval` check, well inside the 8-12Hz ask)
and only computed at all when `self._watchdog_wants_levels` is true (skips
the RMS math entirely for feed-only/disabled users, not just the dispatch).
**Critical, and directly from this file's own earlier audio-jitter
debugging trail:** `level()` uses `subprocess.Popen(...,
start_new_session=True)` with no `.wait()` and no output capture — never
`subprocess.run`'s blocking wait — so a slow or hung `omarchy-shell`
process can never stall the tight loop feeding `pw-play`'s queue the way
an earlier blocking-call bug already did once for this exact audio path
(see "Audio bugs found and fixed" above). Amplitude is RMS of each
resampled chunk normalized by 12000 (headroom above the ~3856 RMS this
project measured for normal speech post-gain-fix, so loud passages can
still reach the top of the scale rather than pinning it constantly) —
a heuristic, not calibrated against real spoken audio yet (see below).

**QML side** — `Watchdog.qml` gained: a `displayMode` property (parsed
from `start()`'s payload, defaulting to `"feed"` for any caller that omits
it — keeps the OSD/window-labels-style bare `start '{}'` call working);
a `levels` rolling buffer (28 samples) plus `pushLevel`/`visualizerLine()`
(builds one string via `" ▁▂▃▄▅▆▇█"` indexed by amplitude, matching the
task's own suggested character set); a single `Text` element
(`visualizerRow`) rendering that string in cyan, sized larger when
`displayMode === "visualizer"` (no feed competing for space) and smaller
when `"both"`; the feed `ListView` hidden (and its height reclaimed) when
`displayMode === "visualizer"`. `handleEvent`'s new `"level"` branch only
calls `pushLevel` while `root.convState === "speaking"` (drops stray/late
samples that land just after a state flip), and the `"state"` branch
clears `levels` the instant the state isn't `"speaking"` — confirmed live
this produces the faint idle-dot baseline (`······...`) immediately on a
state change, not a frozen last frame, exactly per the ask.

**A real, pre-existing bug found and fixed along the way, not introduced by
this work:** `pushLine`'s original implementation called
`feedList.positionViewAtEnd()` directly — but `feedList` lives inside the
per-screen `Variants` delegate, a different QML id scope than `pushLine`
itself (declared on the outer `root: Item`). Confirmed via `journalctl`
that this threw `ReferenceError: feedList is not defined` on *every single
call*, in plain `"feed"` mode, on a freshly `omarchy restart shell`'d
process — i.e. this was never actually working, not a regression from the
visualizer changes; it only wasn't noticed before because `lines = next`
(the assignment that actually renders the new feed line) happens *before*
the broken scroll call, and `feedList`'s own `onCountChanged:
positionViewAtEnd()` already re-implements the same "scroll to bottom"
behavior redundantly, so the feature looked correct in every prior
screenshot despite the warning firing silently in the journal the whole
time. Fixed by deleting the redundant/broken call — `onCountChanged`
alone now owns the scroll, which it already did successfully.

**Verified live, screenshots at every state** (raw `omarchy-shell watchdog
...` IPC calls simulating exactly what `live.py`/`watchdog.py` would send,
per this project's own established fallback for a feature this agent
cannot trigger by actually speaking):
1. `"feed"` mode: `state speaking` + two `tool_call`/`tool_result` events
   → real lines rendered (`[speaking]`, `> execute_command("Browser")`,
   `> execute_command("Browser") -> ok`), zero warnings in `journalctl`
   after the `pushLine` fix (screenshot).
2. `"visualizer"` mode: `state speaking` + 16-18 `level` events at varied
   amplitudes → real cyan unicode block-character bars rendered, shaped
   like the fed amplitude sequence, feed list fully hidden (screenshot).
3. State changed to `"listening"` mid-visualizer → bars cleared to the
   faint idle-dot baseline immediately, not a frozen last frame
   (screenshot) — the decay behavior the task specifically asked for.
4. `"both"` mode: a small amplitude row above a live tool-call feed,
   both rendering simultaneously (screenshot).
5. `watchdog stop` → confirmed clean, no leftover overlay artifacts
   (screenshot), same as every prior round's own final check.

**Not yet done:** a real spoken conversation exercising `watchdog.level()`
from inside the actual real-time audio loop (this agent cannot produce
speech, same structural limitation as every prior watchdog-related round —
see the window-labels and original Watch Dogs overlay entries above for
the same caveat). The amplitude normalization divisor (12000) is a
reasoned estimate from this file's own earlier RMS measurement, not
calibrated against a real live session — worth revisiting once someone
can actually watch it react to real speech.

### Daemon restart

`omarchy-ai.service` was idle (`journalctl` confirmed "session ended, back
to listening" as the last lifecycle line, `restart-status` independently
agreed) when this work was ready to load, so it was restarted for real
(`systemctl --user restart omarchy-ai.service`) rather than left for next
time. Clean startup confirmed: `loaded wake word model(s): omachy, omri,
roni` and `omarchy-ai ready — say any of: ...`, no traceback.

**A known gap in `restart-status`'s safety check, caught by re-reading
this same file after acting, not before:** the concurrent TV-auto-discovery
work (running as a sibling background agent in this same session) had, a
few minutes earlier, added a sharper warning right above this section
("Also confirmed, a sharper version of this project's existing
'don't casually restart' discipline...") — a live cast subprocess
(`spike_cast_sender.py`, detached via `start_new_session=True`) is still a
member of `omarchy-ai.service`'s own cgroup (`KillMode=control-group`), so
a service restart kills an in-progress cast too, not just a voice
conversation. `settings.py`'s `_conversation_busy()` only replays
`daemon.py`'s wake/session lifecycle log lines — it has no idea a cast
subprocess exists at all, so it would have reported `busy: false` and let
the restart through even with a live cast running. This session's restart
did not actually interrupt anything: `journalctl` timestamps show the
other agent's own `wait_and_restart.sh` (a background watcher script,
polling `journalctl` for its own session-end marker) completed *its* own
restart at 15:24:03, fifteen seconds before this session's `systemctl
--user restart omarchy-ai.service` at 15:24:18 — the cast subprocess was
already gone by the time this restart ran, confirmed via `pgrep -af
spike_cast` returning nothing both before and after. Lucky timing, not a
property of the check itself. **Real gap worth fixing, not yet fixed:**
`_conversation_busy()` should also check whether anything is still running
in `omarchy-ai.service`'s cgroup beyond the daemon's own expected children
(`pw-record`, and while a conversation is live, `pw-play`) before calling a
restart safe — e.g. `systemctl --user show omarchy-ai -p
ControlGroup`/`systemd-cgls` cross-checked against a known-good process
list, refusing (like the conversation check already does) if something
unexpected is present.

## Visualizer moved out of the card; secure API key entry

Two pieces of direct user feedback on the settings/visualizer round, both
live-verified by screenshot.

**Visualizer is no longer inside the Watch Dogs card.** The ask, verbatim:
"outside of the watchdog box, frameless in the center bottom area of the
screen and use the current ascii char but also the one with the more
dotted texture (should look glitchy)". It's now a sibling of `card` rather
than a row in its Column — a bare `Text` with no surface/border of its
own, anchored bottom-center, still gated by the same
`watchdog_display_mode` setting and still only accumulating while
`convState === "speaking"`. In `visualizer`-only mode the card itself is
now hidden outright (`visible: displayMode !== "visualizer"`) rather than
left as an empty header/divider shell.

The glyph set is two density ramps mixed per column and reshuffled on
every level update (~10Hz): the original blocks (`▁▂▃▄▅▆▇█`) plus braille
dot-patterns (`⠁⠃⠇⡇⡏⡟⡿⣿`, dot counts 0..8 so density tracks amplitude the
same way bar height does), with rare full-column artifacts (`▓▒░╳┃╎`)
spliced in at ~4%. The mix, not any single set, is what reads as glitchy
rather than as a tidy audio meter. Width was cut 56 -> 30 columns after a
first screenshot showed it spanning nearly the whole display, then 30 ->
10 on the user's own follow-up ("perfect but I want it one third of its
current length"); the ask was a "small group", not a full-width
equalizer.

**Real gotcha, cost two screenshots:** the first attempt appeared to have
changed nothing — the card was still there with bars inside it. That was
the already-documented stale-plugin-cache behaviour (`omarchy restart
shell` clears it; hot-reload's "Local plugin changed, reloading" log line
appears either way and does *not* mean the running QML actually updated).
Trust the screenshot, not the reload log.

**API key entry (`OPENAI API KEY` section).** The key was previously read
from `~/.config/omavoice/key` — a different, older project's file, which
is not a real setup path for a fresh install. Now: `config.py` grows
`OMARCHY_KEY_PATH` (`~/.config/omarchy-ai/key`) preferred over
`LEGACY_KEY_PATH` (the omavoice one, kept purely so an existing install
doesn't have to re-enter a key it already has), and the settings panel has
a masked field + Save.

Security-relevant details, deliberate:
- The key travels to the helper in the `OMARCHY_AI_API_KEY` environment
  variable (or stdin), **never in argv** — `/proc/<pid>/cmdline` is
  world-readable, so a key passed as an argument is visible in `ps` to any
  other user with a shell here; `/proc/<pid>/environ` is 0400 owner-only.
  This is the whole reason it isn't just another `set <key> <value>` call.
- The file is created with `os.open(..., 0o600)` rather than written then
  chmod-ed, so there's no window where it sits world-readable.
- The value is **write-only from the UI's perspective**: the helper's
  snapshot returns only `{"set": bool, "source": ...}`, never the key, so
  nothing can re-display a stored key and it never reaches QML/JS state,
  a Process's stdout, or a log.
- Validation is deliberately loose (non-empty, no whitespace, >=20 chars,
  `sk-` prefix) — OpenAI has shipped several key formats and a strict
  check would reject a valid future one. No live API call to verify it:
  out of scope, and needlessly exercises a secret to answer a question the
  next real session answers anyway.

Verified: all four rejection paths, then the real existing key migrated
through the command (file lands 0600, content matches source, snapshot
flips `source` omavoice -> omarchy-ai, `load_config()` resolves to the new
path), then the panel screenshotted showing "A key is set."

**Non-bug worth recording, since it cost real debugging time:** the panel
first appeared to show "No key set" and "No trained *.onnx models found"
despite both being present. That was purely screenshot timing — the
Python helper takes ~2s to cold-start, and the screenshots were taken 1.5s
after opening the panel. Confirmed by temporarily logging the callback:
the correct JSON was arriving all along. When this panel looks empty, wait
longer before concluding anything.

## Speak-window timer outliving its own session

Found by reading the journal after an unusually short (4-second) live
session, not by a user report. `run()`'s `_delayed_response()` — the fixed
`speak_window_seconds` timer that fires `response.create` once the user
has had time to say something — was not guarded against the conversation
ending first. Sequence from the real log: session connected 17:52:15,
connection closed 17:52:18, daemon back to listening 17:52:19, and at
17:52:22 the orphaned timer woke up and called `dc.send()` on the closed
data channel:

    aiortc.exceptions.InvalidStateError
    Task exception was never retrieved
    future: <Task finished ... _delayed_response() ...>

Nothing downstream broke (asyncio swallows it, the next session is a fresh
`LiveSession` with its own channel), so it was invisible in use — one full
traceback per short session, buried in the log. Fixed by checking
`self._hangup.is_set() or dc.readyState != "open"` before sending, which
is the same condition `RTCDataChannel.send()` itself raises on.

Worth keeping in mind for the VAD work that eventually replaces this
timer: any deferred task in `run()` needs the same "is this session still
alive?" check, since a conversation can end at any point inside the
window.

## Settings panel: content rendering outside its own box

User report: "the settings box is having issues, some settings are
outside of te box." Real bug, screenshot-confirmed: `Panel.qml`'s
`KeyboardPanel` had a hardcoded `contentHeight: panel.fittedContentHeight
(column.implicitHeight, Style.space(560))` — a fixed 560px cap set when
the panel was first built, before the OpenAI API key section existed.
Once that section was added, the Column's real content grew past 560px,
but the drawn card stayed capped at it — the API key field, Save button,
Restart button, and config-path text all rendered *below* the visible
blue-bordered box instead of the box growing to contain them.

Fixed by dropping the hardcoded cap entirely: `fittedContentHeight`'s own
`availableCardHeight` (screen-relative, computed by `KeyboardPanel` itself)
is already the real safety bound against the panel growing off-screen, so
a second fixed number was redundant and, worse, silently wrong the moment
actual content outgrew it. `contentHeight: panel.fittedContentHeight
(column.implicitHeight)` now sizes the panel to its content, bounded only
by the screen. Screenshot-verified: everything now sits inside the box.

Worth remembering for the next addition to this panel: don't reintroduce
a fixed cap without checking it against real content height first, or
just leave it uncapped like this.

## Visualizer going dark after a tool call interleaved with speech

The real cause of "I can't see the voice visualization now": every
`watchdog.state()` call site had its OWN debounce logic, each based on a
different proxy for "is this actually a new state" rather than tracking
the real last-sent state:

- `_run_tool_call` fired `state("thinking")` unconditionally, every call.
- The output-transcript-delta handler only fired `state("speaking")` when
  `self._output_buffer` was empty — a proxy for "first delta of a new
  utterance", not "the overlay isn't already showing speaking".

Those two don't compose. `_output_buffer` only resets on the *next user
utterance*, not after a tool call. So the very pattern this project's own
`instructions` ask the model to do — "briefly confirm what you did after
calling a tool" (tool call, then a bit of speech, possibly another tool
call, then more speech) — sets `thinking` before the tool, then never
gets back to `speaking` after it, because the buffer is no longer empty
by the time speech resumes. The visualizer (and the Watch Dogs card's
state dot) go dark for the rest of that response and never recover until
the user's next utterance flips it back to `listening`. Given how much of
this session's real usage was exactly this pattern (`start_casting` /
`list_cast_targets` / narration, repeatedly, in single conversations —
see the TV-casting round above), this was very likely live for most of
today's testing without being caught until reported directly.

Fixed by replacing every ad hoc debounce with one real tracker:
`self._watchdog_state` plus a `_set_watchdog_state(label)` helper that
only calls `watchdog.state()` when `label` differs from the last one
actually sent, reset to `None` at each new session's connect. All four
call sites (session connect, tool-call start, input-delta, output-delta)
now route through it. Verified directly (not by screenshot luck this
time): a scripted reproduction of the exact interleaved sequence —
`listening -> thinking -> speaking -> thinking -> speaking` (tool call,
speech, another tool call, more speech) — now emits all five transitions
correctly; the old code would have silently dropped the second
`speaking`.

Separately, real bug this round: `Panel.qml`'s settings panel had a
hardcoded `Style.space(560)` content-height cap left over from before the
OpenAI API key section existed — once that section pushed real content
height past it, the extra content rendered outside the panel's own drawn
border instead of the border growing to contain it (user report: "some
settings are outside of the box"). Fixed by dropping the fixed cap
entirely and relying on `KeyboardPanel`'s own screen-relative
`availableCardHeight`, which was already the real safety bound.
Screenshot-verified.

## Settings bar icon: Omarchy logo instead of a generic gear

User's own request: "the settings should be a different icon, preferably
omarchy logo if not ai." Tried the obvious source first — `/usr/share/
omarchy/logo.svg`, the full wordmark (1215x285, spelling "Omarchy" in
blocky letterforms) — and it rendered as illegible noise at bar-icon
size (confirmed by screenshot: a wordmark's fine strokes don't survive
that much downscaling). Switched to `/usr/share/pixmaps/omarchy.png`
instead — the actual square app-icon mark (300x300, confirmed via
`magick ... -format %[pixel:...]` that the green maze pattern is opaque
alpha and everything else is fully transparent), which is already built
to read at icon scale. Rendered via `BarIconButton`'s `iconComponent`
slot (a `QtQuick.Effects`/`MultiEffect`-free plain `Image`, kept in its
native brand green rather than tinted to the bar's monochrome-glyph
convention — more recognizably "the Omarchy logo" this way, and this
project's own tray-adjacent icons in this bar already show real
per-app colors, not just Nerd Font glyphs). Screenshot-confirmed sharp
and recognizable in the bar.

## Language-agnostic conversation ending (Hebrew, confirmed live)

User report: asking to end the conversation in Hebrew never actually hung
up. Root cause matched the long-standing "next action" item in this file:
both mechanisms that can trigger a hangup — `_check_exit_phrase` (fuzzy-
matches the *user's* words against `config.exit_phrases`) and
`_check_farewell` (substring-matches the *model's own* farewell against
`live.py`'s `_FAREWELL_MARKERS`) — were English-only word lists. Neither
algorithm cares what language it's comparing (`fuzz.ratio` is plain edit
distance; the farewell check is plain substring `in`), so the fix is
adding real words, not new logic.

Two-part fix:
1. **Instructions strengthened**: the `end_conversation` tool call is the
   only genuinely language-agnostic mechanism (a structured function
   call, not a text pattern) — and it's been confirmed firing live for
   real tonight for the first time. Made calling it in the same turn as
   any goodbye explicitly non-optional in the instructions, regardless of
   language, rather than one clause in a longer list.
2. **Hebrew words added as a concrete safety net** to both lists —
   תפסיק/תפסיקי/מספיק/סיימנו/ביי/להתראות/זהו in `exit_phrases`,
   להתראות/נתראה/ביי in `_FAREWELL_MARKERS`. Deliberately excluded "שלום"
   from the farewell markers — it means both "hello" and "goodbye"/
   "peace", the same kind of real ambiguity that already excluded
   "take care" from the English list (the model saying "let me take care
   of that" mid-task had false-positived before).

Verified directly (not live-conversation-tested yet, daemon restart is
being held back — see below): `fuzz.ratio` against realistic Hebrew
exit phrases (exact short utterances match at 100; a longer phrase like
"תפסיקי בבקשה" only scores 66.7, below the 82 threshold — the same
whole-string-ratio limitation that already applies to the English list,
not a new regression) and `_check_farewell` against five realistic
sentences including the ambiguous "שלום, מה שלומך" (hello, how are you) —
correctly does NOT trigger, while "בסדר, להתראות!" / "ביי ביי, נדבר
בקרוב" / "תודה רבה, נתראה" / "Sure, goodbye!" all correctly do.

**Not yet loaded into the live daemon.** A real cast to the TV was active
when this was ready to ship, and `omarchy-ai.service` has no explicit
`KillMode` (defaults to `control-group`) — restarting it would kill that
cast too, directly contradicting the user's own fresh requirement that
casting survive everything except an explicit stop. Holding the restart
until either the cast finishes/is stopped, or the in-flight casting fix
decouples the sender from the daemon's cgroup (flagged to that work).

## Bar icon live/idle status dot

User's own request: the bar icon should signal whether the assistant is
actually live right now — red when idle, green during an actual
connected conversation. Added `src/omarchy_ai/voice/status_icon.py`
(same `omarchy-shell -q <target> <method>` IPC pattern as `watchdog.py`,
but a deliberately separate module/target: `omarchy-ai.settings`, not
`watchdog`) — `set_live(bool)` is called unconditionally at session
connect and in the `finally:` hangup block in `live.py`, NOT gated by
`watchdog_enabled`, since this indicator needs to reflect true connection
state regardless of whether the optional Watch Dogs overlay is on.

On the QML side, `omarchy-ai.settings/Panel.qml` gained its own
`IpcHandler` (same pattern `$OMARCHY_PATH/shell/plugins/agents/Panel.qml`
uses for its extra `refresh`/`next` methods — re-forward the base
open/close/show/hide/toggle, add the new method) with `setLive`, plus a
small colored status dot rendered in the bottom-right corner of the
existing Omarchy-logo icon (green `#39ff88/red `#ff5f5f`, matching the
Watch Dogs palette) — a corner dot rather than recoloring the whole icon,
so the actual logo (added earlier this session, user-requested) stays
fully visible rather than being replaced by a solid color block.

Verified live via direct IPC before wiring the real daemon calls in:
`omarchy-shell omarchy-ai.settings setLive '{"live":true}'` then
`'{"live":false}'`, screenshotted and cropped both times — dot renders
correctly in both colors at the right position. Daemon restarted
afterward to load the real connect/hangup wiring.

## Four user-reported casting bugs: HY300 invisible, ONN "not recognized", casting silently fails, receiver UI cleanup

### 1. HY300 projector invisible to `list_cast_targets` — fixed

Root cause, confirmed live: the HY300Pro projector (`192.168.1.86`, this
project's original Phase 0 test TV) does not advertise
`_androidtvremote2._tcp` at all (`avahi-browse -a -t` showed nothing for
it under that service type), so D10's discovery never saw it. It DOES
advertise plain `_adb._tcp` (Android's classic Wireless-Debugging-on
signal — a lower bar than the Google Android TV Remote Service, common on
non-GMS-certified/budget boxes and projectors):
```
avahi-browse -r -p -t _adb._tcp
=;enp0s25;IPv4;adb-52001089a69606f2054;_adb._tcp;local;Android-4.local;192.168.1.86;5555;
```
`src/omarchy_ai/display/discovery.py`'s `discover_androidtv_devices()` now
also browses `_adb._tcp` and folds in any device not already covered by an
`_androidtvremote2._tcp` result (deduped by IP). Its mDNS instance name
(`adb-<serial>`) isn't useful to read aloud, so a friendly name is
resolved via `adb connect <addr>` + `adb shell getprop ro.product.model`
(new `_resolve_adb_model_name`), each subprocess call capped at
`_ADB_MODEL_TIMEOUT = 3.0s` and the result cached in-process
(`_adb_model_name_cache`) since a model name never changes call to call —
this sits on the voice assistant's hot path (`list_cast_targets`/
`start_casting` can be called mid-conversation), so a slow/unreachable
adb-only device degrades to its raw mDNS name rather than stalling
discovery. On any timeout/error it falls back to the raw mDNS name rather
than raising.

**Confirmed live, real network, real device:**
```python
>>> discovery.discover_androidtv_devices()
[{'name': 'Living Room TV', 'address': '192.168.1.191'},
 {'name': 'Idol TV', 'address': '192.168.1.203'},
 {'name': 'HY300Pro', 'address': '192.168.1.86'}]
```
2.13s total (including the adb round trip), and 2.15s again after a
deliberate `adb disconnect 192.168.1.86:5555` to force a real
reconnect+resolve rather than reusing an already-open adb session —
both well inside acceptable hot-path latency. `actions.list_cast_targets({})`
called through the real production code returns the same three devices.

### 2. "ONN streamer not recognized" — not a discovery bug, no code change

Confirmed (per the task's own pre-verified diagnosis, not re-investigated
here): the ONN box and "Living Room TV" are the same physical device
(192.168.1.191) — the user named it "Living Room TV" in Google
Home/Cast, and that's the name `_androidtvremote2._tcp` advertises. It
was already discoverable; the user tried targeting it by its model name
("ONN-A") rather than its discovered name. No code needed — issue 1's
discovery.py work doesn't touch this path, and the optional
`_googlecast._tcp` TXT-record model cross-reference suggested as a
nicety was skipped as genuinely low-priority against the real bugs below.

### 3. Casting reports success but never mirrors — THREE stacked root causes found, two fixed, one needs the user

**Root cause A (the one already diagnosed before this task started) — the
signaling relay drops the sender's offer if the Android viewer hasn't
finished cold-starting yet.** `src/omarchy_ai/display/signaling.py`
relayed messages only while both sender and viewer were simultaneously
connected and silently dropped anything else
(`"sender sent offer but no viewer is connected -- dropped"`), confirmed
live across 7/7 real `start_casting` attempts logged in
`/tmp/omarchy-signaling.log` before this fix. Fixed by having `Relay`
buffer the sender's most recent offer and any ICE candidates while no
viewer is connected, and replay them (in order) the moment a viewer
registers; a *new* sender connection discards any stale buffered
offer/ICE from a previous attempt rather than accumulating or replaying
stale state (see `signaling.py`'s module docstring and `Relay` class for
the full reasoning, including why only the sender->viewer direction is
buffered — no evidence of the reverse race on this network).

**Root cause B (found while verifying the fix, not previously known) —
`ufw` only allows the signaling port from one hardcoded IP.** Real, hard
evidence: `journalctl -k` showed `[UFW BLOCK] ... DPT=8765` SYN packets
from Living Room TV (192.168.1.191) being dropped at the kernel firewall,
repeatedly, across every real cast attempt tonight — including the
*first* one made this session, at 22:44:23-22:44:31, well before any of
today's code changes were loaded, and 28 total blocked attempts logged
since this morning. `/etc/ufw/user.rules` (readable without root) shows
why: `-A ufw-user-input -p tcp --dport 8765 -s 192.168.1.86 -j ACCEPT` —
a rule scoped to exactly one IP, the original Phase 0 hardcoded TV,
almost certainly written before D10's multi-TV auto-discovery existed and
never widened afterward. This silently blocks the WebSocket handshake for
any TV other than 192.168.1.86, *regardless* of the signaling fix above —
confirmed by testing against 192.168.1.86 (which the rule does cover):
real, sustained video. **Not fixed by this session — this machine has no
passwordless sudo (`sudo -n ufw status` -> "a password is required") and
modifying firewall rules isn't something to script around that. Needs the
user to run, one time:**
```
sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp
```
(widens the existing single-IP rule to the whole LAN, matching D10's
multi-TV design; the old narrow rule can be left in place harmlessly or
deleted with `sudo ufw delete allow from 192.168.1.86 to any port 8765
proto tcp` — either works, ufw allow rules don't conflict). Until this
runs, casting to Living Room TV / Idol TV will keep silently failing at
the TCP level even with every other fix in this entry applied — this is
the one remaining piece of issue 3 not fully closed.

**Root cause C — not a real bug, a false alarm from this session's own
test churn.** Verifying root cause A against 192.168.1.86 (the only
device root cause B doesn't block) initially reproduced what looked like
a *new* stall: one real frame decoded then a real Android-side crash
(`FATAL EXCEPTION` on `decoder-texture-thread`,
`java.lang.IllegalStateException: Rendered texture metadata was null in
onTextureFrameAvailable`, `org.webrtc.AndroidVideoDecoder.onFrame` ->
`SurfaceTextureHelper.tryDeliverTextureFrame`, process died). Reasoned
through and then confirmed by retesting cleanly (letting the app settle
after one `am start` instead of the rapid force-stop/relaunch cycling
this session's own diagnosis had been doing to it) that this doesn't
reproduce — real evidence below. Not chased further as a real bug since
it didn't recur; noted here in case it ever does (the crash is inside
`org.webrtc`'s native decoder path, nothing this project's own code
touches).

**Fix A verified live, real hardware, through the actual production
`start_casting`/`stop_casting` code path (not the raw spike script)**,
against 192.168.1.86 (the one device not blocked by root cause B):
`actions.start_casting({"target": "HY300Pro"})` -> `ok=True`. Android
`EglRenderer` log, ten consecutive 4-second windows after ICE connected:
```
Frames received: 60. Dropped: 0. Rendered: 61. Render fps: 14.8. ...
Frames received: 60. Dropped: 0. Rendered: 60. Render fps: 15.0. ...
Frames received: 60. Dropped: 0. Rendered: 60. Render fps: 15.0. ...
Frames received: 60. Dropped: 0. Rendered: 60. Render fps: 15.0. ...
Frames received: 60. Dropped: 0. Rendered: 60. Render fps: 15.0. ...
Frames received: 61. Dropped: 0. Rendered: 61. Render fps: 15.2. ...
Frames received: 60. Dropped: 0. Rendered: 59. Render fps: 14.7. ...
Frames received: 60. Dropped: 0. Rendered: 61. Render fps: 15.2. ...
Frames received: 60. Dropped: 0. Rendered: 60. Render fps: 15.0. ...
Frames received: 60. Dropped: 0. Rendered: 60. Render fps: 15.0. ...
```
Locked to the requested 15fps, 0 drops, for the entire run — before this
fix, the exact same setup produced zero frames, ever, on 7/7 attempts.
Sender-side log confirms the mechanism directly: `local offer created ...
sending to signaling server` immediately followed (same run) by `got
remote answer` a few hundred ms after the Android app's own cold-start
delay — the offer was buffered and replayed, not dropped.

### Extending the fix: casting must survive conversation end AND daemon restarts, not just be independently timed

Two additional real requirements surfaced while verifying the above (the
second was an actual live near-miss caught during this session, not
theoretical):

**Casting already does not stop when a conversation ends** — verified,
not assumed. `grep -rn "stop_casting\|_cast_process" src/` shows
`stop_casting`/`_cast_process` referenced only inside
`execution/actions.py` itself (the tool implementation) and
`execution/tools.py`/`config.py` (the tool's schema/instructions) —
`voice/live.py`'s hangup path (`_hangup` event, both `finally:` blocks),
`core/daemon.py`, and `voice/watchdog.py` never reference either. Real
trace confirming this structurally-expected behavior actually held live:
a genuine user conversation tonight called `start_casting({'target':
'Living Room TV'})` at 22:45:22 (spawning `spike_cast_sender.py` pid
376026); the conversation hit a farewell and `journalctl` logged
`"session ended, back to listening"` at 22:45:59; pid 376026 was still
running, still connected to the signaling relay, 5+ minutes later when
checked (`ps -o pid,etimes,cmd`) — never touched by the hangup.

**Casting used to NOT survive a daemon restart — fixed.** Real problem
found while preparing to verify the above: `systemctl --user show
omarchy-ai -p KillMode` is the systemd default, `control-group`, and the
cast subprocesses (`_cast_process`/`_signaling_process`, spawned via plain
`subprocess.Popen(..., start_new_session=True)`) were still members of
that same cgroup despite `start_new_session=True` giving them their own
process group/session (confirmed originally back in the D10 work,
`/proc/<pid>/cgroup` showing the service's own path) — so restarting the
daemon to load *any* fix, including the ones in this very entry, would
have killed an active cast too. Fixed with a new `_spawn_own_cgroup`
helper in `actions.py`: both cast subprocesses now launch via `systemd-run
--user --scope --collect --quiet -- <argv>` instead of a plain `Popen`.
Confirmed live, empirically, before wiring it in: `systemd-run --scope`
execs straight into the target argv (no wrapper process) —
`Popen.pid` matched the payload's own real pid (checked via
`/proc/<pid>/cmdline`), and `poll()`/`terminate()` on the returned `Popen`
behaved exactly as on a plain one — while placing that pid in its own
transient scope unit (`/proc/<pid>/cgroup` ->
`.../app.slice/run-p<pid>-*.scope`), not the service's. Falls back to a
plain `Popen` (today's cgroup-coupled behavior) if `systemd-run` isn't on
PATH, rather than failing casting outright.

**Verified live, for real, through the actual production code path, with
a real daemon restart mid-cast:**
1. `actions.start_casting({"target": "HY300Pro"})` — confirmed both
   spawned processes in independent scopes: `run-p381206-i385325.scope`
   (signaling) and `run-p381226-i382073.scope` (sender), neither under
   `omarchy-ai.service`.
2. While video was actively flowing (`EglRenderer`: 60/60 frames, 15fps,
   confirmed in the window immediately before), ran `systemctl --user
   restart omarchy-ai.service` for real (the user explicitly authorized
   disruptive testing this session: *"you can kill what you need, I want
   to see the fixes"*). `journalctl` confirmed the daemon came back up
   clean (new PID, "omarchy-ai ready...").
3. Both cast subprocesses were still running afterward, untouched
   (`ps aux` showed the same two pids, same start times).
4. `EglRenderer` log straddling the restart moment: frames continued
   landing every ~4s, 59-61 per window, 0 drops, with no gap — the
   restart at 22:56:57 is invisible in the video stream entirely.

### 4. Android receiver UI cleanup — done, rebuilt, verified visually

Per the user's own words: *"on the receiver remove the IP fields and
connect button no need for them and also all the states and prints should
be at the bottom of the receiver's screen."* `ReceiverScreen.kt`'s
`StatusOverlay`: removed the `OutlinedTextField`(Sender IP)/`Button`
(Connect) entirely — the app already auto-connects on launch
(`ReceiverViewModel.init`) using the remembered/default host, so there was
never anything for a person to do with them; `hostInput`/`onHostChange`/
`onConnect` params dropped from `StatusOverlay`'s signature along with the
now-unused imports (`Row`, `OutlinedTextField`, `Button`, `widthIn`,
`width`). The status `Column`'s `verticalArrangement` changed from
`Arrangement.Center` to `Arrangement.Bottom` so the title/status text sits
near the bottom of the screen instead of dead center — the wallpaper
background (added in an earlier round, user-requested, kept as-is) is
unaffected.

`./gradlew assembleDebug` — `BUILD SUCCESSFUL`, real APK rebuilt
(`libjingle_peerconnection_so.so` still linking per the "unable to strip"
build-log line, same as every prior successful build). Installed on the
HY300Pro (`adb install -r`), force-stopped and relaunched for a clean
process, screenshotted (`adb shell screencap`): confirms no IP field, no
Connect button anywhere on screen, and the "Omarchy AI Receiver" title +
status line ("Failed: Failed to connect to /192.168.1.65:8765" — the
signaling relay was deliberately down for this specific screenshot, to
exercise the real status-text path rather than the streaming state) both
render near the bottom of the screen with the Omarchy wallpaper intact
behind them, exactly as asked.

### Still open

1. **Root cause B (the `ufw` rule scoped to one IP)** — needs the user to
   run the one `sudo ufw allow ...` command above; until then, casting to
   Living Room TV/Idol TV (anything other than 192.168.1.86) will still
   silently fail at the TCP level regardless of every fix in this entry.
2. Not independently re-verified this round: a full spoken, wake-word-triggered
   conversation that calls `start_casting` against Living Room TV specifically
   (blocked on open item 1) — everything here was verified either through
   the real production code path directly (bypassing voice, this
   project's established pattern for changes that can't be triggered by a
   spoken wake word in an unattended session) or through a real user
   conversation's own trace in `journalctl`.

## Receiver version tracking + irrelevant pairing narration — fixed, verified live

User's own report: *"right now the livingroom tv shows the old version
(lets also add a visible SW ver to the reciver on one of the corners of
the sw so we can follow on it) when I asked omarchy to install the newer
reciver she gave me instructions to how to oped deveoper settings which is
iirrelevant."*

Root causes, confirmed by reading the code before touching anything:

1. `android-receiver/app/build.gradle.kts` had `versionCode = 1` /
   `versionName = "1.0"` hardcoded since the very first build months ago
   — every build produced the same version, so nothing (human or code)
   could tell an old install apart from a new one.
2. `_ensure_receiver_apk_built()` (`execution/actions.py`) only rebuilt
   when the APK file was *missing*, never when source had changed.
3. `install_receiver_on_tv` had no branch for a target that's already
   adb-reachable — it always assumed a brand-new, never-paired TV and
   walked straight into the Developer-options narration, which is exactly
   what the user hit asking to update Living Room TV (already paired,
   already casting to for a while).

### What changed

- `versionName` now derives from `git rev-parse --short HEAD` (`-dirty`
  suffix for uncommitted local builds — this project builds locally, not
  via CI, so that's a real case); `versionCode` from `git rev-list --count
  HEAD` (Android requires a real monotonically increasing integer, a hash
  isn't one). Computed via `providers.exec {}` in `build.gradle.kts`, not
  a plain `project.exec {}`/`ProcessBuilder` — both of the latter were
  tried first and failed: a free top-level Kotlin function has no
  implicit `Project` receiver (`exec`/`rootDir` unresolved), and even once
  that was fixed with a captured `repoRoot` val, Gradle 9.1's
  configuration cache refuses a raw external process started directly at
  configuration time ("Starting an external process ... during
  configuration time is unsupported" — confirmed live). `providers.exec`
  registers the process as a tracked config-cache input instead.
- `ReceiverScreen.kt`: a small, dim `v${BuildConfig.VERSION_NAME}` label
  in the top-right corner, always visible (even mid-stream, unlike the
  status text below it which hides once mirroring starts) so it can be
  read via `adb shell screencap` without interrupting a live cast.
  `buildConfig = true` enabled in `build.gradle.kts` to generate it.
- `_ensure_receiver_apk_built()`: now hashes every file under
  `android-receiver/app/src` + `build.gradle.kts` (`_source_fingerprint`)
  and compares it against a marker file (`app-debug.built-from-hash`)
  written after the last successful build — rebuilds whenever they
  diverge, not just when the APK is missing. Exposed a real latent bug
  while testing this for the first time with a genuine rebuild: the
  `gradlew` subprocess call never set `cwd`, so it only ever worked by
  accident of the caller's own working directory — failed live with
  "Directory '/home/ben-ami/Git/omarchy-ai' does not contain a Gradle
  build" once actually exercised. Fixed by adding a `cwd` parameter to
  `_run()` and passing `gradlew.parent`.
- `install_receiver_on_tv` now takes an optional `target` (same
  name-or-IP matching as `start_casting`'s). When that (or the single
  unambiguous currently-reachable TV, if omitted) is already adb-reachable
  right now, it skips straight to `_install_or_update_receiver` — checks
  the installed version (`adb shell dumpsys package
  ai.omarchy.receiver`) against the local build's version (`aapt dump
  badging`, resolved from `$ANDROID_SDK_ROOT/build-tools/<highest>/aapt`)
  and only reinstalls if they differ. No pairing narration at all for
  this path. `start_casting` now runs the same check-and-update
  (`_ensure_receiver_current`) before every cast, fire-and-forget, so a
  cast never silently runs against a stale build; it's genuinely cheap in
  the common case (one dumpsys read + one aapt read once the fingerprint
  confirms the local APK itself doesn't need rebuilding).

### Verified live against Living Room TV (192.168.1.191:5555, real hardware)

1. Before: `_installed_receiver_version("192.168.1.191:5555")` → `"1.0"`.
2. Rebuilt the APK for real (`./gradlew assembleDebug`, `providers.exec`
   fix above) — `aapt dump badging` on the result: `versionName='d14b50d-dirty'`,
   `versionCode='43'`.
3. `install_receiver_on_tv({"target": "Living Room TV"})` through the
   real production function (not a mock) — no pairing_code, no
   Developer-options narration, went straight to reinstall: `"updated the
   receiver on 192.168.1.191:5555 from v1.0 to vd14b50d-dirty (it was
   already paired, so no Developer-options setup was needed)"`.
4. Re-running the same call reported `"receiver on 192.168.1.191:5555 is
   already up to date (vd14b50d-dirty)"` instead of reinstalling again.
5. `install_receiver_on_tv({})` (no target) with mDNS discovery finding 0
   devices at that instant *did* fall through to the pairing narration —
   expected and correct given the no-target path only takes the shortcut
   when there's exactly one unambiguous, currently-discoverable device;
   this is a real, known gap (a flaky/empty mDNS moment plus no named
   target still produces the old narration) rather than a silent one —
   noted below, not yet closed.

**Initially misdiagnosed as a live-cast disruption -- corrected after
checking `journalctl`.** A `spike_cast_sender.py` process was running
throughout step 3's `adb install -r`, and Living Room TV's receiver
wasn't in the foreground activity list afterward (confirmed via `adb
shell dumpsys activity activities`), which read at the time like the
reinstall had force-stopped an actively-streaming receiver mid-cast.
Checked `journalctl --user -u omarchy-ai` before writing this up, since
that's the standard this project holds evidence to: every `start_casting`
call in the live log that session (23:52-23:58) targeted HY300Pro
(192.168.1.86), never Living Room TV — the running sender process was
casting to HY300Pro the whole time, so Living Room TV's receiver wasn't
in the foreground for an unrelated reason (nothing was being streamed to
it), not because the reinstall interrupted anything. Relaunched it with
`adb shell am start -n ai.omarchy.receiver/.MainActivity` regardless, as
reasonable cleanup after a forced reinstall — just not the recovery from
a real disruption it first looked like.

### Separately added: `run_omarchy_command` (Claude Code's "omarchy" skill, ported to voice)

The user asked directly for this: *"I want you also to add the Omarchy
skill you have to omarchy assistant"* — i.e. give the voice assistant a
real slice of what the Claude Code "omarchy" skill
(`~/.claude/skills/omarchy/SKILL.md`) already lets an agent do on this
machine (themes, reminders, bar layout, toggles, and more, via the
`omarchy` CLI).

Scoped deliberately narrow, same bar as every other voice-reachable tool
in this file (`tools.py`'s own header comment: only Level 1/2 per
ADR-0001's policy table, nothing Level 3+ without a real confirm/policy
layer, which doesn't exist yet): only the `theme`, `toggle`, `reminder`,
`bar`, and `capture` command groups are runnable at all, and even within
`theme`, the `install`/`remove`/`update` subcommands are blocked
specifically — confirmed by reading `omarchy-theme-install`'s actual
source that `theme install <url>` runs a real `git clone` of that URL,
which on a voice assistant would come directly from an untrusted speech
transcript, not something typed by a person who can eyeball it first.
`pkg`/`update`/`reinstall`/`dev`/`system` (package/OS-level, Level 3+),
`refresh` (the skill's own instructions require human confirmation before
running it — no way to honor that from voice yet), and `hook`/`plugin`
(install code that runs automatically on future system events) are
excluded at the group level entirely.

Verified live: `run_omarchy_command({"args": ["pkg", "add", "evil"]})` →
refused with the allowed-groups list; `{"args": ["theme", "install",
"http://evil.example/x.git"]}` → refused specifically for that
subcommand; `{"args": ["theme", "current"]}` → real result (`"Tokyo
Night"`); `{"args": ["reminder", "show"]}` → real result.

### Still open

1. `install_receiver_on_tv` with no `target` and zero currently-discoverable
   TVs still falls through to the brand-new-device pairing narration, even
   for a TV that's genuinely already paired — it just can't tell the
   difference between "genuinely new" and "known TV, mDNS had a bad
   moment" without a name to check against. Not hit in the reported bug
   (the user was almost certainly naming Living Room TV, whether via an
   explicit target or conversational context), but worth tightening if it
   recurs.
2. The daemon needs a restart to pick up all of the Python-side changes
   above (`config.py`/`actions.py`/`tools.py`) — not yet done as of this
   entry; see the restart-discipline notes elsewhere in this file
   (`journalctl` check first, `settings.py`'s conversation-busy guard).

(Both items above resolved later the same session: daemon restarted
cleanly, the live HY300Pro cast survived it. See below for what came
next.)

## Remote mic button — investigated live, real negative result, closed for now

The user's own follow-up question was whether Living Room TV's remote mic
button could be harnessed to talk to the assistant, then, after being told
most Android TV platforms reserve it for the system assistant (ADR-0001's
own risk note #2), pushed back with a real point: *"thats not true...
apps use it for serch fields and such... we need to harness it for our
app."* That's true for a different mechanism (an app's own on-screen
search-box mic icon calling `RecognizerIntent` on tap) than the physical
remote button, but the pushback was fair enough to actually test rather
than defer to the ADR's original untested assumption.

**Round 1 -- initial button press, logcat captured:** launched
`com.google.android.katniss` (`SearchResultActivity`), `LAUNCH_SINGLE_INSTANCE`
from uid 10044 (katniss's own uid) with `BAL_ALLOW_SAW_PERMISSION`.
`settings list secure` showed `assistant` and `voice_interaction_service`
both set to `com.google.android.katniss/.search.serviceapi.KatnissVoiceInteractionService`
-- the completely standard Android Assist (`VoiceInteractionService`)
role, not something TV-specific. Device: "onn. Full HD Streaming Device"
(Walmart), Android 14.

**Feasibility looked real at this point:** `WRITE_SECURE_SETTINGS` is
grantable via `adb shell pm grant` once an app declares it in its
manifest (confirmed: it refuses to grant an undeclared permission, not a
deeper restriction). Built a minimal feasibility stub -- a
`VoiceInteractionService` + `VoiceInteractionSessionService` in the
receiver app, declared in the manifest, with a `res/xml` service
descriptor (two real build errors hit and fixed along the way: XML
comments can't contain `--` at all, anywhere, not just as delimiters; and
a free top-level Kotlin function in `build.gradle.kts` has no implicit
`Project`/`rootDir` receiver, unrelated to this stub but hit again here
confirming the earlier version-tracking fix's own note about it).
Installed, granted the permission, overrode `assistant` and
`voice_interaction_service` via `adb shell settings put secure` to point
at the new stub -- both read back correctly.

**Round 2 -- button pressed again with the override live:** identical
result. Katniss launched again, this time visibly via
`android.speech.action.WEB_SEARCH` with an *explicit* component
(`cmp=com.google.android.katniss/...SearchActivityTrampoline`) from uid
1000 (`system_server` itself, `BAL_ALLOW_ALLOWLISTED_UID`). The
`assistant`/`voice_interaction_service` override had zero effect.

**Root cause of the negative result:** `dumpsys package
com.google.android.katniss` shows Katniss registered as the sole
`android.intent.category.DEFAULT` handler for three separate actions --
`android.speech.action.WEB_SEARCH`, `android.intent.action.ASSIST`, and
`android.search.action.GLOBAL_SEARCH` -- all resolved through ordinary
implicit-intent `PackageManager` resolution, not through
`VoiceInteractionManagerService`/the assistant role at all. The physical
mic key on this remote triggers one of those three (most likely
`WEB_SEARCH`, matching the second capture), a completely different,
harder-to-override mechanism than the Assist role: Katniss is a
privileged system app with no competing registered handler, so it wins
resolution outright. Unseating it would need either root (to change the
underlying resource/config picking the default handler) or registering a
competing intent-filter and hoping Android doesn't just pop up a
disambiguation chooser on every press instead of picking one silently --
bad UX even if it technically worked, and not attempted.

**Verdict: real, tested, negative.** Not a "most platforms don't allow
this" assumption anymore -- an actual override was attempted, read back
as applied, and made no observable difference on a second real button
press. Closed for now; a paired phone/tablet mic remains the realistic
path to talk to the assistant while looking at the TV, not further
remote-button interception. Settings restored to Katniss afterward
(`assistant`/`voice_interaction_service` both set back); the feasibility
stub code was removed rather than left in unused (manifest reverted via
`git checkout`, stub `.kt`/`.xml` files deleted) since nothing currently
exercises it and it requested a sensitive permission for no active
benefit -- this write-up is the record of the attempt, not the code.

## Phone bridge (beta) — a phone on the LAN can now talk to Omarchy

User's request, while waiting on a USB mic for the TV remote-mic path:
*"in the meanwhile I want also to allow talking to omarchy through a
local server omarchy will run and can be connected locally through a
phone. lets do a beta test for such service and later on we will do some
sort of one time qrcode pairing through the PC."* Explicitly scoped as a
beta with no pairing yet -- that's future work.

### Design

The obvious-looking approach (relay raw audio through this Python process
between phone and OpenAI) was rejected in favor of something much
simpler once the real session-creation call was read closely: gpt-live-1
here isn't the classic OpenAI Realtime API's ephemeral-token pattern, it's
a custom `POST https://api.openai.com/v1/live/sessions` that takes a
*complete* SDP offer in the request body and returns a complete SDP
answer (see `voice/live.py`'s existing desktop flow) -- there's no
separate "mint a short-lived token, then the client negotiates on its
own" step to reuse directly, since the browser can't hold the real API
key to call that endpoint itself. So the phone's browser does its own
real WebRTC connection **directly to OpenAI** (audio never touches this
Python process at all) — this machine's only two jobs are relaying that
one SDP offer/answer exchange (needs the real key) and executing tool
calls server-side when the model makes them mid-conversation, the exact
same `execution.actions.run_action` the desktop's `LiveSession` already
uses. Much less code than a real audio relay would have needed, and no
new audio-jitter surface to debug.

Refactored `voice/live.py` first: extracted `build_session_config(config)`
(instructions + learned preferences + recent context + tools/model) out
of `LiveSession.run()` into a standalone function, so the desktop client
and the new phone bridge build the *exact* same session shape from one
place rather than two copies that could quietly drift.

### What was built

- `src/omarchy_ai/phone/server.py` — stdlib-only (`http.server`'s
  `ThreadingHTTPServer`, no new dependency for what's really just a
  handful of small JSON/static endpoints): `GET /` serves the mobile
  page; `POST /api/live/offer` relays the browser's SDP offer to
  `/v1/live/sessions` (via `build_session_config`) and returns the
  answer; `POST /api/tool` executes a tool call via `run_action` and
  returns the result — `get_recent_actions` special-cased to return an
  empty list rather than an unknown-action error, since the phone bridge
  has no per-session action log to draw one from (a real, known gap, not
  silently papered over).
- `src/omarchy_ai/phone/static/index.html` — single-file mobile page,
  dark theme matching the rest of this project's UI: a tap-to-talk/tap-to-
  hang-up circular button, live transcript, `<audio autoplay>` for
  playback (the browser handles this natively — no manual `pw-play`
  pipeline needed like the desktop side). JS mirrors `live.py`'s event
  handling: `session.input_transcript.delta`/`output_transcript.delta`
  for the transcript, `response.event` → `response.output_item.done` →
  `item.type == "function_call"` for tool calls (posted to `/api/tool`,
  result sent back as `response.item.create` + `response.create`, same
  two-message pattern as the desktop), `end_conversation` (either event
  shape) closes the connection. Same fixed
  `speak_window_seconds`-equivalent stopgap as the desktop client before
  firing the first `response.create`.
- `config.py`: `phone_bridge_enabled` (default `false` in source — this
  repo's own `~/.config/omarchy-ai/config.yaml` has it `true` for the
  beta test itself) and `phone_bridge_port` (`8766`, distinct from the
  casting signaling relay's `8765`).
- `core/daemon.py`: starts the phone bridge once at daemon startup (not
  per-conversation like `LiveSession` — a phone tap should work any time,
  no wake word needed), stops it in `stop()`.

### Verified live, for real

1. `build_session_config` produces the same session shape as before the
   refactor (42 tools, correct model/voice) — confirmed directly.
2. Started the server standalone: `GET /` returns the real page (200,
   contains the Talk button); `POST /api/tool` with `battery_status`
   returns a real result through the real `run_action` path.
3. **Real end-to-end proof, not just a shape check:** used `aiortc`
   (already a project dependency) to build a genuine SDP offer, POSTed it
   to `/api/live/offer`, got back a real answer SDP from OpenAI (1538
   bytes) through the relay, and successfully called
   `setRemoteDescription` with it — an actual Live API session was
   created through this exact code path, not simulated.
4. Enabled in the live daemon's config, restarted (`journalctl` checked
   first, no conversation in progress), confirmed listening on
   `0.0.0.0:8766` (`ss -tlnp`) and serving the real page from the running
   service, not just a standalone test.

### Known gaps (beta, by design or not yet done)

1. **No pairing/auth at all yet.** Anyone who can reach this machine on
   the LAN can open the page and drive the whole desktop through it while
   `phone_bridge_enabled` is true. The user explicitly framed this as
   future work (a one-time QR-code pairing flow through the PC) — noted
   here so it isn't mistaken for an oversight.
2. **Farewell-phrase/exit-phrase safety net not ported to the browser
   client.** The desktop's `_check_exit_phrase`/`_check_farewell` fuzzy
   matching exists because `end_conversation` was found unreliable on
   this API (a real, previously-documented bug: zero function_call events
   across a full session where the user clearly said goodbye). The phone
   client only hangs up on an actual `end_conversation` tool call or the
   manual button for now — if that tool call doesn't fire, the
   conversation can be left open until the user notices and taps the
   button themselves. Real gap, not fixed in this beta.
3. **Firewall unverified.** This session earlier found `ufw` blocking the
   casting signaling port (8765) from anything but one specific IP — the
   same could easily be true of 8766 for a phone on a different IP.
   Couldn't check `ufw status` (no passwordless sudo for it); if the
   phone can't reach `http://192.168.1.65:8766/`, this is the first thing
   to check (`sudo ufw allow from 192.168.1.0/24 to any port 8766 proto
   tcp`, same pattern as the earlier 8765 fix).
4. **No visual indicator wiring.** Unlike the desktop's `LiveSession`,
   the phone bridge doesn't call `status_icon.set_live()`/`watchdog.*` —
   the bar icon's live/idle dot and the Watch Dogs overlay don't reflect
   a phone-only conversation. Not attempted this round; would need a
   `/api/session-started`/`ended` round trip from the browser (best-effort
   only, since a phone can lose network or have its tab killed without
   ever calling "ended").

## Firewall rules needed — checklist for a future install/packaging script

User's own request: *"all this firewall rules remember them for the
package installation later on."* This machine's `ufw` has no general
"allow this LAN" rule — every port this project listens on needs its own
explicit allow, discovered the hard way twice now (8765 via the casting
mirroring-failure investigation, 8766 just now for the phone bridge, both
confirmed live via real `[UFW BLOCK]` kernel-log entries, not guessed).
No passwordless sudo for `ufw` on this machine (`sudo -n ufw status` ->
"a password is required"), so none of these can be run by an agent —
whatever eventually installs/packages this project needs to either run
them (with the user present for the sudo prompt) or document them as a
manual post-install step.

**Every port this project currently opens**, and the rule each one needs
(LAN-wide, `192.168.1.0/24` — narrower than the original single-IP
`-s 192.168.1.86` rule found for 8765, which broke the moment casting to
any *other* IP was attempted, see below):

1. **8765 — `display/signaling.py`'s WebRTC signaling relay** (casting
   to Android TVs/projectors, spawned on demand by `start_casting`).
   Original state found on this machine: `/etc/ufw/user.rules` had
   `-A ufw-user-input -p tcp --dport 8765 -s 192.168.1.86 -j ACCEPT` —
   allowed only the one TV already manually tested against, silently
   dropping every other TV's connection at the TCP level regardless of
   any application-level fix (root cause B of the mirroring-failure
   investigation, this file's casting-race entry above).
   ```
   sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp
   ```
2. **8766 — `phone/server.py`'s phone bridge** (beta, see above). Real
   blocked packets confirmed via `journalctl -k`: `SRC=192.168.1.58
   DST=192.168.1.65 ... DPT=8766 ... SYN` logged as `[UFW BLOCK]`.
   ```
   sudo ufw allow from 192.168.1.0/24 to any port 8766 proto tcp
   ```

An install script could reasonably loop over a small table of `{port,
proto, purpose}` and run one `ufw allow` per row rather than hardcoding
two calls — worth designing that way given a third port (or a port
becoming configurable, like `phone_bridge_port` already is) is a real
possibility, not a one-off.

## Phone bridge: QR pairing + Omarchy-styled page with a real visualizer

User's follow-up, right after confirming the phone bridge beta actually
worked end-to-end (real `start_casting`/`stop_casting` tool calls from a
phone, logged live): *"create a full QR code pairing mechanism which
without cant pair to the local website... that QR connection will be in
the settings"*, then, mid-build: *"and give the local sit an Omarchy
design and in the middle the ASCII voice visualizer."*

### Pairing — the real access boundary, not a UI nicety

`phone/server.py`:
- `mint_pairing_token(config)` — single-use, 5-minute-TTL token written
  to `~/.config/omarchy-ai/phone_bridge/pending_pair_token.json` (0600,
  same creation pattern as the API key file). Called from
  `cli/settings.py`'s new `pair-phone` command, a *separate, short-lived*
  process from the daemon — writing a shared file rather than adding an
  internal HTTP call between the two matches how this project already
  does cross-process communication (`config.yaml` itself).
- `GET /pair?token=...`: validates (exists, `secrets.compare_digest`
  match, unused, unexpired) → marks used, mints a real session id
  (`secrets.token_urlsafe(32)`), appends it to
  `paired_sessions.json` (0600), sets a `Secure; HttpOnly; SameSite=Strict`
  cookie with a 1-year `Max-Age` (pairing should survive phone restarts —
  revoke explicitly instead), 302s to `/`.
- `_is_paired()` checks that cookie against the sessions file on **every**
  request — `GET /` serves `not_paired.html` instead of the real app for
  an unpaired request, and `/api/live/offer`/`/api/tool` both return a
  flat 403 `{"error": "not paired"}`. This is the actual enforcement
  point, independent of whatever the page itself does — matches the
  user's own framing ("without which can't pair").
- `revoke_all_sessions()` (`cli/settings.py`'s `revoke-phones`) clears the
  sessions file, kicking every paired phone out at once.
- `cli/settings.py`: `phone_bridge_enabled` added to `SETTABLE` (a real
  toggle now, not just a config field with no UI); `pair-phone` shells
  out to `qrencode` (confirmed installed, `pacman`-provided) to turn the
  pairing URL into a PNG, returned as base64 for the panel to render
  directly via `Image { source: "data:image/png;base64,..." }` — no new
  Python dependency, no writing a temp file for the panel to read back.

**Verified live, real HTTP requests against the running daemon, not
mocked** — 8 assertions covering the full lifecycle: unpaired `GET /`
serves the not-paired page; unpaired tool call 403s; a minted token pairs
successfully (cookie + 302); *reusing the same token fails* (single-use
enforced, confirmed via the server's own rejection log line firing);
paired `GET /` serves the real app; a paired tool call executes for real
(`battery_status` → real result); `revoke-phones` immediately invalidates
the existing cookie. Also confirmed cross-process: a token minted by the
`cli/settings.py` CLI (a fresh process) validated correctly against the
already-running daemon's `phone/server.py` after a restart — the
shared-file handoff works as designed, not just within one process.

**Real side effect caught, not silently absorbed**: shipping this onto
the already-live (unauthenticated-beta) daemon immediately logged out the
phone that had been actively using it minutes earlier — expected and
correct (there was no session cookie to grandfather in, since pairing
didn't exist yet when that phone first connected), but worth naming
explicitly since it's a real, user-visible consequence of the restart,
not a bug.

### Settings panel UI (`Panel.qml`)

New "PHONE BRIDGE" section: an `Enable` toggle (`phone_bridge_enabled`,
same `Toggle` pattern as the Watch Dogs section), a paired-phone count,
"Pair a Phone" (calls `pair-phone`, renders the returned QR PNG inline,
180×180, with a "scan within 5 minutes, one scan only" caption) and
"Revoke All" buttons. Hot-reloaded cleanly (`DEBUG qml: Local plugin
changed, reloading: omarchy-ai.settings`, no errors in the shell's log)
and brace/paren-balance-checked. **Not independently screenshot-verified
this round** — opening the panel via `omarchy-shell -q omarchy-ai.settings
open`/`toggle` didn't produce a layer-shell surface in `hyprctl layers`
this time (no error either; possibly popup positioning needs a real
bar-icon click's event context rather than a bare IPC call — not
something chased further given the actual pairing logic is independently
verified through real HTTP requests either way). Needs a real look from
the user at the machine to confirm the section renders/behaves as
expected visually.

### Omarchy-styled page + real ASCII/braille visualizer

Redesigned `phone/static/index.html`: the Omarchy logo (copied from
`/usr/share/pixmaps/omarchy.png` into `phone/static/`, served through a
new small allowlisted static-file route in `do_GET` — resolves the
request path against `_STATIC_DIR` and rejects anything that resolves
outside it, so this can't be used to read arbitrary files) plus the exact
same green (`#39ff88`) accent and glow used everywhere else in this
project's UI (Watch Dogs overlay, the settings panel's live-status dot).

The visualizer itself is a faithful port of `Watchdog.qml`'s
`visualizerLine()`/`pushLevel()` — identical glyph sets (`blockChars`,
`brailleChars`, `glitchChars`) and identical mixing probabilities (4%
glitch, 62% braille, else block; the same two-independent-`Math.random()`
idle-baseline sparkle when there's nothing to show), so a phone
conversation reads visually consistent with the desktop overlay rather
than inventing a second visual language. Driven differently, though:
there's no server-side RMS to pipe over IPC here (the browser plays the
remote audio itself), so it reads the live amplitude directly client-side
via a Web Audio `AnalyserNode` attached to the incoming WebRTC track,
sampled at the same ~10Hz the desktop throttles `watchdog.level()` to,
accumulating only while `convState === 'speaking'` (mirrors the QML
property's own doc comment: "Only accumulated while convState ===
speaking... so a conversation that goes quiet doesn't leave a stale
frozen frame").

Verified: embedded JS parses cleanly (`node -e "new Function(...)"` on
the extracted `<script>` body — cheap syntax check, not a runtime test of
the audio-driven rendering, which needs a real phone/mic to exercise);
real HTTP request through the running daemon with a valid paired cookie
confirms the page serves with the `#visualizer` element and the logo
reference present, and `GET /omarchy-logo.png` returns the image with
the correct content type.

### Known gaps

1. Settings panel's new section not screenshot-verified (see above) —
   ask the user to confirm it visually.
2. The visualizer's audio-driven rendering itself (as opposed to its
   idle baseline, which is pure JS with no audio input) hasn't been
   exercised against a real phone conversation yet — the gain/headroom
   constant (`rms * 5`) is a first guess by ear, not measured the way the
   desktop's `OUTPUT_GAIN`/`/12000` normalization was (see STATUS.md's
   earlier residual-static entries) — may need tuning once actually
   watched live.
3. All test pairing sessions created while verifying this were revoked
   (`revoke-phones`) before handing off — `phone_bridge_paired_count: 0`
   confirmed — so the very first real pairing is still the user's own.
