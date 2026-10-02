# Voice satellite

A voice satellite is an ESPHome voice-assistant speaker (for example a Home
Assistant Voice Preview Edition) in another room. It wakes on the desktop's
own wake word and then holds a live conversation with the same assistant:
the same tools, context and announcements as a call at the computer, and
you can interrupt her while she speaks.

Code: `src/omarchy_ai/voice/satellite.py`.

## How it works

An ESPHome voice assistant is turn-based: wake word on the device, one
request, the microphone closes, one reply. Two things are changed:

1. **The wake word is found on the desktop.** The firmware streams its
   microphone continuously over the ESPHome native API. Omarchy AI runs the
   same openWakeWord model, threshold and trigger count on that stream as on
   the desktop microphone (`voice/wake.py`), so any wake word trained for
   the desktop works on the satellite. openWakeWord models cannot run on the
   device itself (it runs microWakeWord models).
2. **Her speech does not use the device's reply stage**, which closes the
   microphone. It is played by the device's media player as an announcement,
   an MP3 stream fetched from the desktop, while the microphone keeps
   streaming. The device's echo cancellation keeps her from hearing
   herself.

The microphone audio leaves the device all the time, to this desktop only.
It is sent to Gemini only between a wake word and the end of that
conversation. With the desktop off, the satellite shows "not connected".

## Firmware

Start from the device's stock ESPHome configuration and change:

```yaml
voice_assistant:
  use_wake_word: true            # the assistant finds the wake word
  on_client_connected:
    - voice_assistant.start_continuous:   # instead of micro_wake_word.start
```

With `start_continuous`, `voice_assistant.is_running` is always true. The
stock Voice PE configuration uses it to mean "a conversation is going on"
(the centre button, un-ducking the music, `on_end`). Replace those checks
with a flag that `on_start` sets (the assistant sends `RUN_START` at the
wake word) and `on_end` clears; in `on_end`, wait for
`media_player.is_announcing` to end instead of for the assistant to stop.
The on-device `micro_wake_word` must not start or stop the assistant any
more. For a button that starts a conversation without the wake word, call
`id(va).set_use_wake_word(false)` before `voice_assistant.start_continuous`
and set it back to `true` in `on_start`.

Keep the API encryption key the device already has, or set a new one.

## Setup

In `~/.config/omarchy-ai/config.yaml`:

```yaml
satellite_host: 192.0.2.10        # the device
satellite_http_port: 8767         # the device fetches her speech here
```

Put the device's API encryption key (base64) in
`~/.config/omarchy-ai/satellite-key`, mode 0600, and let the device's
network reach `satellite_http_port` on the desktop (firewall). Only one
client can hold a device's voice-assistant subscription: disable it in Home
Assistant, or in whatever else serves it. Restart `omarchy-ai.service`.

| Setting | Default | Meaning |
| --- | --- | --- |
| `satellite_silence_seconds` | 20 | The conversation ends after this long without anyone speaking |
| `satellite_mic_gain` | 1.0 | Multiplies the device's microphone level, for the wake word and the conversation |
| `satellite_speech_rms` | 800 | Level (after gain) that counts as the user speaking, for the stuck-turn guard |
| `satellite_talk_channel` | 1 | Which microphone channel goes to the model when the device sends two: 1 is the noise-suppressed one on a Voice PE, 0 the raw one. The wake word always uses the first |

### Showing the conversation somewhere else

`satellite_state_command` names a program of yours. It is started with the
assistant and reads one JSON object per line on its standard input:

```json
{"state": "listening"}
{"state": "speaking", "level": 0.42}
{"state": "idle"}
```

`level` is her voice level (0 to 1), ten times a second, timed to when the
speaker plays it. The state is repeated every few seconds while a
conversation is open, so a display can time out on its own if the lines
stop. Use it to drive a wall display, a light, or anything else.

`OMARCHY_AI_WAKE_DIAGNOSTICS=1` logs the satellite's best wake score and
level every 5 seconds, for choosing the gain.
