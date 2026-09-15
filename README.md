```
   ▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄▄▄▄▄▄▄▄▄  ▄▄▄  ▄
  ▐░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░▌▐░▌
  ▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀▀▀ ▐░▌░▌▐░▌
  ▐░▌       ▐░▌▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌▐░▌          ▐░▌          ▐░▌░▌▐░▌
  ▐░█▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░█▄▄▄▄▄▄▄█░▌▐░▌       ▐░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░▌▄▄▄▄▄▄▄▄▄ ▐░▌ ░▌▐░▌
  ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌       ▐░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌  ░▐░▌
   ▀▀▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░█▀▀▀▀▀▀▀█░▌▐░▌       ▐░▌ ▀▀▀▀▀▀▀▀▀█░▌▐░█▀▀▀▀▀▀▀▀▀ ▐░▌   ░▌
            █░▌▐░▌          ▐░▌       ▐░▌▐░▌       ▐░▌          █░▌▐░▌          ▐░▌   ▐░▌
   ▄▄▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░▌       ▐░▌▐░█▄▄▄▄▄▄▄█░▌ ▄▄▄▄▄▄▄▄▄█░▌▐░█▄▄▄▄▄▄▄▄▄ ▐░▌   ▐▐░▌
  ▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌       ▐░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░░░░░░░░░░░▌▐░▌    ░░▌
   ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀         ▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀▀▀▀▀▀▀▀▀▀▀  ▀      ▀▀

                          A I   —   v o i c e   f o r   t h e   d e s k t o p
```

<div align="center">

[![License: MIT](https://img.shields.io/badge/license-MIT-39ff88.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-39ff88.svg)](pyproject.toml)
[![Built for Omarchy](https://img.shields.io/badge/built%20for-Omarchy%20%2F%20Hyprland-39ff88.svg)](https://omarchy.org)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha%2C%20daily%20driver-39ff88.svg)](STATUS.md)

</div>

---

**Omarchy AI** is an independent, self-hosted voice assistant for
[Omarchy](https://omarchy.org) (the Arch-based Hyprland desktop distro). Say
a wake word, talk to it in plain language, and it actually *does things* on
your desktop — not a chatbot bolted onto a terminal. It's built directly on
OpenAI's realtime `gpt-live-1` model over WebRTC, with a typed tool-calling
layer that turns speech into real Hyprland/PipeWire/desktop actions.

It is not built on Home Assistant, Open Interpreter, or any other agent
framework — the conversation loop, the tool registry, the wake-word
pipeline, the casting subsystem, and the desktop UI are all this project's
own code.

> **Honest status check.** This is a real, working system — the daemon runs
> as a systemd service today, has controlled a real desktop over dozens of
> live voice sessions, and casts a real desktop to a real Android TV over
> WebRTC. It was also built in one long, evidence-driven session (see
> [`STATUS.md`](STATUS.md) for the blow-by-blow) and has no installer
> package yet. Manual install only, for now — see below.

---

## What it does

Say the wake word, then talk normally. A few real examples of what it's
been used for, live:

- *"Turn the volume up"* / *"set brightness to 40%"* — done immediately.
- *"Fullscreen the terminal, not the browser"* — it lists open windows,
  figures out which one you mean (asking you, with a floating label over
  the actual window, if it's still ambiguous), focuses it, then acts.
- *"Cast this to the living room TV"* — mirrors your screen and audio to a
  paired Android TV or projector over WebRTC, picks the right TV if more
  than one is on the network, and can walk you through pairing a brand new
  one it's never seen before.
- *"Set a reminder for 3pm"* / *"switch to the catppuccin theme"* — routed
  straight through Omarchy's own 228 built-in commands.
- *"What did that build in the terminal end up doing?"* — reads the real
  text a terminal it opened has printed, instead of taking (and paying for)
  a vision-model screenshot.

## Features

**Voice & wake word**
- Fully local wake-word detection (`openWakeWord`), listening continuously
  and opening a `gpt-live-1` conversation only when triggered — no
  connection (and no per-second billing) outside an active conversation.
- Any number of custom wake-word models can be loaded at once
  (`~/.config/omarchy-ai/wake_models/*.onnx`) — whichever one fires wakes
  it, configurable threshold/trigger-frame sensitivity.
- Two-way, low-latency realtime voice via `gpt-live-1` over WebRTC
  (`aiortc`), delegated to `gpt-5` for reasoning/tool-calling.
- Ends a conversation on "bye"/"stop"/"that's all" (and non-English
  equivalents — Hebrew is wired in as the concrete case) by watching the
  model's own spoken farewell, not just an English keyword match.
- Cross-session memory: a rolling window of recent conversation history is
  folded into the next session's context, and explicit standing
  preferences ("always confirm before muting") are remembered permanently
  via a dedicated tool call.

**Real desktop control** — ~25 typed tools plus Omarchy's full command set:
- Volume/mute, mic mute, brightness, night light, Bluetooth, battery,
  media playback.
- Workspace switching, window listing/focusing, fullscreen toggle, close,
  screenshots, screen lock.
- Launchers: terminal, browser, files, editor.
- Typing and key-press injection (`type_text`/`press_key`) into whatever
  window is focused — including modifier combos, so it can e.g. focus a
  browser's address bar before typing a URL.
- `list_commands`/`execute_command` reach every one of Omarchy's 228 bound
  keybinding commands (theme, reminders, bar layout, clipboard, emoji
  picker, capture tools, and more) by fuzzy description instead of needing
  a dedicated tool per command.
- `run_omarchy_command` runs a scoped allowlist of the `omarchy` CLI itself
  (theme/toggle/reminder/bar/capture) — package installs, system updates,
  reboots, and anything destructive are explicitly refused, not just
  undocumented.
- Arch-aware shell guidance: when it types a shell command into a terminal
  on your behalf, its instructions specifically steer it to `pacman`/`yay`
  syntax, never `apt`/`dnf`/`brew`, which don't exist on this distro.
- `describe_screen` — a screenshot plus a separate vision-model call, used
  only as a fallback when window state and conversation context aren't
  enough to tell what you mean.
- `read_tile_log` — every terminal the assistant itself opened has its real
  text output tracked (via `script(1)`), so it can answer "did that
  command finish?" by reading actual text instead of paying for a vision
  call.
- Floating window name-label badges (`show_window_labels`) — instead of
  reading a list of window titles out loud when it's not sure which window
  you mean, it drops a real on-screen badge over each candidate so you can
  just point at (or name) the right one.

**Android TV / projector casting**
- Mirrors the desktop's screen and system audio to a paired Android TV or
  projector over WebRTC — direct `wlr-screencopy-unstable-v1` capture
  (bypassing the portal/PipeWire ScreenCast path, which turned out to wedge
  permanently on this hardware — see `STATUS.md`), `openh264enc`/opus
  encode, relayed through a small local signaling server.
- Real-time mDNS auto-discovery of Android TV targets on the LAN
  (`_androidtvremote2._tcp`) — asks which TV you mean if more than one is
  found, remembers the last one otherwise.
- Guided pairing for a TV that's never been set up before: narrates the
  exact on-device steps (Developer options → Wireless debugging → pairing
  code), then completes ADB pairing/connect/install once you read the code
  back to it.
- A Kotlin/Jetpack Compose Android receiver app (`android-receiver/`) —
  receive-only WebRTC via `stream-webrtc-android`, auto-connects on launch,
  shows the Omarchy wallpaper while idle.

**Phone bridge**
- A local page (self-signed HTTPS, so mobile mic access works) that a
  paired phone can open to talk to Omarchy AI directly from a browser —
  the phone does its own WebRTC straight to OpenAI; this machine only
  relays the SDP handshake (it holds the API key) and executes tool calls.
- QR-code pairing, generated from the settings panel: single-use,
  5-minute-TTL pairing token, a signed session cookie good for a year once
  paired, and a hard 403 on every request without a valid paired session —
  this is a real access gate, not a cosmetic prompt. Pairings can be
  revoked all at once.
- The page shares this project's own visual identity (dark background,
  `#39ff88` green glow, the same ASCII/braille audio visualizer as the
  desktop HUD) rather than looking like a bolted-on debug page.

**"Watch Dogs" HUD overlay**
- A Quickshell/QML overlay (styled after the hacking-terminal HUD from
  *Watch Dogs*) that shows either a live feed of tool calls as the
  assistant makes them (`> execute_command("Browser")`, `> volume_up() ->
  ok`), a reactive ASCII/braille audio visualizer while it's speaking, or
  both — selectable per user preference.
- Driven entirely by the daemon's own lifecycle (connect/tool-call/state
  change/disconnect), not something the model has to remember to call.

**Settings panel**
- A Quickshell bar panel (gear icon next to the system tray) for picking
  the wake word/sensitivity, the response voice, entering the OpenAI API
  key, choosing the Watch Dogs display mode, and pairing/revoking phones —
  reads and writes the same `config.yaml` the daemon itself uses, and
  refuses to trigger a restart while a conversation is actually in
  progress.
- The bar's Omarchy icon also carries a live-status dot (green while an
  actual `gpt-live-1` session is connected, otherwise idle) independent of
  whether the HUD overlay itself is enabled.

## Architecture, briefly

```
src/omarchy_ai/
  core/       daemon loop (listen -> wake -> converse -> listen), cross-
              session history and learned-preference memory
  voice/      wake word (openWakeWord), the gpt-live-1 WebRTC client
              (aiortc), the Watch Dogs/status-icon IPC bridges
  execution/  Hyprland/PipeWire/desktop actions, the tool schemas exposed
              to the model, per-terminal output logs, the vision fallback
  display/    mDNS device discovery, the WebRTC signaling relay for casting
  phone/      the local HTTPS phone-bridge server + paired web page
  cli/        the omarchy-ai-settings CLI the settings panel shells out to
  policy/     scaffolded, not yet built (see "Known gaps" below)
android-receiver/   Kotlin/Compose Android TV receiver app (Gradle project)
systemd/            omarchy-ai.service unit template
docs/                architecture decision record (ADR-0001) + dependency list
scripts/             the original WebRTC/API reverse-engineering spikes,
                      setup.sh / uninstall.sh
```

- **Python**, managed with [`uv`](https://astral.sh/uv), targeting the
  system Python (needs `--system-site-packages` for `python-gobject`/
  GStreamer bindings — see below).
- **Quickshell/QML** for every on-screen desktop UI piece (the HUD overlay,
  the window-label badges, the settings panel) — these live as Omarchy
  *user plugins* under `~/.config/omarchy/plugins/`, not inside this Python
  package (see "Known gaps").
- **`aiortc`** (pure Python) for the desktop voice client's own WebRTC
  connection to `gpt-live-1`.
- **`pywayland`** talking to `wlr-screencopy-unstable-v1` directly for
  screen capture (not the `xdg-desktop-portal` ScreenCast path, which
  proved permanently broken on this Hyprland/PipeWire combination).
- **Kotlin + Jetpack Compose** for the Android TV receiver app, using
  `stream-webrtc-android` (Maven Central's maintained `org.webrtc` drop-in).

Full reasoning behind each of these choices — including the dead ends — is
in [`docs/ADR-0001-architecture.md`](docs/ADR-0001-architecture.md).

## Manual installation

There is no installer package yet — this is the real, current path a
technical user needs on a fresh Omarchy machine.

**1. System packages** (needs `sudo`; see
[`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) for the full list and why
each is needed):

```bash
sudo pacman -S --needed android-tools gst-plugins-bad gst-plugins-good \
  gradle qrencode
yay -S android-sdk-cmdline-tools-latest
```

GStreamer, PipeWire, `xdg-desktop-portal-hyprland`, `avahi-daemon`, and
`python-gobject` ship on a stock Omarchy install already — the setup script
below checks rather than assumes, but doesn't install them for you if
they're somehow missing.

**2. Clone and sync the Python environment:**

```bash
git clone https://github.com/omribenami/Omarchy-AI.git ~/Git/omarchy-ai
cd ~/Git/omarchy-ai
./scripts/setup.sh
```

`setup.sh` creates a `uv`-managed venv **with `--system-site-packages`**
(required so it can see the system's `python-gobject`/GStreamer bindings —
a plain `uv venv` will not work here), runs `uv sync`, writes a starter
`~/.config/omarchy-ai/config.yaml`, and installs the systemd user unit from
`systemd/omarchy-ai.service`.

**3. Set your OpenAI API key** (needs `gpt-live-1` access). The real,
secure path is the settings panel's key field, which writes it to
`~/.config/omarchy-ai/key` with `0600` permissions and never round-trips
the value back out anywhere. Without the Quickshell panel installed (see
the gap below), the same mechanism is reachable directly:

```bash
echo -n "sk-..." | .venv/bin/omarchy-ai-settings set-api-key
```

(Piped via stdin deliberately — never as a command-line argument, since
argv is readable by any process on the machine via `/proc/<pid>/cmdline`.)

**4. Wake word:** ships with openWakeWord's pretrained "hey jarvis" model
as a placeholder (no dedicated "omarchy" model has been trained yet — see
Known gaps). Drop any number of custom `.onnx` models into
`~/.config/omarchy-ai/wake_models/` to use your own instead; any of them
loaded will wake it.

**5. Enable and start the service:**

```bash
systemctl --user enable --now omarchy-ai
journalctl --user -u omarchy-ai -f
```

**6. Open firewall ports**, if you want casting or the phone bridge
reachable from other devices on your LAN — `ufw` (or your firewall of
choice) blocks these by default:

```bash
sudo ufw allow from 192.168.1.0/24 to any port 8765 proto tcp  # casting signaling
sudo ufw allow from 192.168.1.0/24 to any port 8766 proto tcp  # phone bridge
```

(Adjust the subnet to your own LAN.)

**7. Android receiver app** (only needed for TV/projector casting): built
automatically on first `install_receiver_on_tv` call, or manually via
`cd android-receiver && ./gradlew assembleDebug`. Needs the JDK/Android SDK
pinned in `mise.toml` — run `mise install` in that directory first.

### Known gaps

Documented honestly rather than papered over:

- **The Quickshell/QML UI plugins are not in this repository.** The Watch
  Dogs HUD overlay, the floating window-label badges, and the settings bar
  panel are real, working Omarchy user plugins
  (`omarchy-ai.watchdog`/`omarchy-ai.window-labels`/`omarchy-ai.settings`)
  that were developed directly under `~/.config/omarchy/plugins/` on the
  original machine — a separate location from this Python package that
  never got checked into this git history. Cloning this repo today gives
  you the full voice/execution/casting/phone-bridge daemon, but **not**
  those three visual pieces; the daemon degrades gracefully without them
  (the IPC calls just log a warning and continue), but you won't see the
  HUD, the window badges, or have a graphical settings panel until they're
  recreated and installed via `omarchy plugin enable <name>`. Bringing them
  into this repo (e.g. under a `quickshell/` directory) is a reasonable
  next step, not done here to avoid pushing untested, hastily-relocated
  code at 3am.
- **No installer package.** Manual steps above are the real, current path.
- **No dedicated "Omarchy" wake-word model.** Training one (openWakeWord
  supports custom training) is separate, not-yet-done work; the pretrained
  "hey jarvis" model is the practical default today.
- **The policy/permission layer described in `docs/ADR-0001-architecture.md`
  (read-only / reversible / confirm-required / denied-by-default tool
  tiers) is designed but not implemented as a separate enforcement layer**
  — today the tool list itself is hand-curated to only expose read-only and
  reversible actions (see `src/omarchy_ai/execution/tools.py`'s own
  docstring), which is a real but informal version of that same idea.
- **Phone bridge has no per-phone action history**, and doesn't (yet) drive
  the bar's live-status dot or the HUD overlay the way the desktop client
  does.
- **Audio quality on casting** is verified for video (steady 15fps, zero
  drops in testing) but not yet measured with the same rigor for the audio
  branch.

See [`STATUS.md`](STATUS.md) for the full, unabridged debugging history —
every bug found, how it was diagnosed, and how it was fixed — and
[`docs/ADR-0001-architecture.md`](docs/ADR-0001-architecture.md) for the
architecture decisions behind all of the above.

## License

MIT — see [LICENSE](LICENSE).
