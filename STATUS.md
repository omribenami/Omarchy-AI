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

**Not yet confirmed:** nothing has run on the TV itself this phase. The
paired TV (192.168.1.86) is currently off the network — `ping` returns
"Destination Host Unreachable" and `adb connect` returns "No route to
host", not an application-level failure. Needs the device powered back
on before install/launch/real-cast confirmation can happen, same
"needs the user/hardware present" pattern as the original wireless-ADB
pairing.

## Next action

1. **Once the TV is reachable again:** `adb connect`, install the debug
   APK, launch it, then run `spike_cast_sender.py` (with the user present
   for the one-time portal consent dialog) and confirm on camera/by eye
   that the desktop actually appears on the TV with audio — the real
   target this whole phase is building toward, not yet done.
2. **Fix the English-only farewell/exit detection** (see above) — the
   actual next correctness bug, not a nice-to-have.
3. Confirm whether `tool_choice: auto` made the `end_conversation` tool
   call actually fire, with a clean live test (the one attempt after adding
   it was inconclusive — session closed on its own after ~30s of silence,
   not clearly from either mechanism).
4. Replace the fixed `SPEAK_WINDOW_SECONDS` timer with real local VAD
   (reuse jarvisd's silence-detection approach) so the daemon knows when
   the user actually finished talking, rather than guessing a duration.
5. Watch Dogs/Matrix-style code-rain overlay UI (user request, tracked, not
   started) — GPU-light, replaces omavoice's simple waveform panel.
6. The policy/tool-registry/audit layer (ADR-0001 D1) — nothing calls out
   to the OS yet; this is still a conversation, not an OS-control assistant.
