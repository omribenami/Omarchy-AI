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

## Next action

1. Replace the fixed `SPEAK_WINDOW_SECONDS` timer with real local VAD
   (reuse jarvisd's silence-detection approach) so the daemon knows when
   the user actually finished talking, rather than guessing a duration.
2. Wake word gating — nothing here opens a connection only on demand yet;
   every spike run is a live, billed ($0.05/min) session from the moment it
   starts. The real daemon must not connect until the wake word fires.
3. Watch Dogs/Matrix-style code-rain overlay UI (user request, tracked, not
   started) — GPU-light, replaces omavoice's simple waveform panel.
4. Fold this into `src/oma/voice/` and `core/` as the real module instead
   of a standalone script, with the policy/audit layer (ADR-0001 D1) sitting
   between what the backend model decides and what actually executes.
