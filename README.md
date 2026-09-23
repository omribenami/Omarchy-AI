<div align="center">

```
                    ██████╗ ███╗   ███╗ █████╗  ██████╗██╗  ██╗██╗   ██╗
                   ██╔═══██╗████╗ ████║██╔══██╗██╔════╝██║  ██║╚██╗ ██╔╝
                   ██║   ██║██╔████╔██║███████║██║     ███████║ ╚████╔╝ 
                   ██║   ██║██║╚██╔╝██║██╔══██║██║     ██╔══██║  ╚██╔╝  
                   ╚██████╔╝██║ ╚═╝ ██║██║  ██║╚██████╗██║  ██║   ██║   
                    ╚═════╝ ╚═╝     ╚═╝╚═╝  ╚═╝ ╚═════╝╚═╝  ╚═╝   ╚═╝   

                    A I  —  v o i c e   f o r   t h e   d e s k t o p

```

</div>
<div align="center">

[![License: MIT](https://img.shields.io/badge/license-MIT-39ff88.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-39ff88.svg)](pyproject.toml)
[![Built for Omarchy](https://img.shields.io/badge/built%20for-Omarchy%20%2F%20Hyprland-39ff88.svg)](https://omarchy.org)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha%2C%20daily%20driver-39ff88.svg)](STATUS.md)

</div>

---

## Demo

A condensed walkthrough of the assistant in action:

<div align="center">

https://github.com/user-attachments/assets/49467e18-0e00-4db6-ba63-bbaf9927b218

</div>

---

**Omarchy AI** is an independent, self-hosted voice assistant for
[Omarchy](https://omarchy.org) — the Arch-based Hyprland desktop. Say a wake
word, talk in plain language, and it *does things* on your machine: windows,
volume, themes, reminders, casting to a TV, phone bridge — not a chatbot
bolted onto a terminal.

It's powered by two layers of models, not one. The conversation itself —
hearing you, talking back, deciding which tool to call — runs on a realtime
voice model you choose: OpenAI's **Live** API (`gpt-live-1`, delegated to
`gpt-5` for reasoning) or Google's **Gemini Live**. Neither of those models
drives the mouse or DOM directly: for native desktop actions (workspaces,
window focus, volume, themes, bar panels) and for web browser navigation,
decisions are delegated instead to [**Typesafe AI's `jev`**](https://github.com/browser-use/jev-ultrafast)
— a small, typed evaluation model built for exactly this, via `desktop_task`
and `browser_task` respectively — rather than asking the conversational
voice model to reason about every click. The conversation loop, tool
registry, wake-word pipeline, casting subsystem, and desktop UI are all this
project's own code — not Open Interpreter or another agent framework.


---

## Feature gallery

**Phone session** — mirror the screen to your phone and operate the PC by talking to Omarchy.

<div align="center">

https://github.com/user-attachments/assets/7abed3fa-ed55-4835-b77a-4d0a1ab85f1f

</div>

**Phone bridge** — talk from a paired phone to Omarchy while mirroring the PC to Android TVs in your network.

<div align="center">

https://github.com/user-attachments/assets/6d20a7b9-3806-4248-be12-83bdddf63f66

</div>

**TV screen mirroring** — WebRTC cast to a paired Android TV / projector.

<div align="center">
<img src="docs/media/tv-mirroring-1.jpg" alt="TV screen mirroring 1" width="420" />
<img src="docs/media/tv-mirroring-2.jpg" alt="TV screen mirroring 2" width="420" />
</div>


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
- *"Remind me in 20 minutes to check the oven"* — a real desktop
  notification via Omarchy's own reminder mechanism, not a fake promise.
- *"Switch to the catppuccin theme"* — routed straight through Omarchy's
  own 228 built-in commands.
- *"What did that build in the terminal end up doing?"* — reads the real
  text a terminal it opened has printed, instead of taking (and paying for)
  a vision-model screenshot.

## Features

**Voice & wake word**
- Fully local wake-word detection (`openWakeWord`), listening continuously
  and opening a conversation with the selected provider only when triggered — no
  connection (and no per-second billing) outside an active conversation.
- Any number of custom wake-word models can be loaded at once
  (`~/.config/omarchy-ai/wake_models/*.onnx`) — whichever one fires wakes
  it, configurable threshold/trigger-frame sensitivity.
- Two-way, low-latency realtime voice via `gpt-live-1` over WebRTC
  (`aiortc`), delegated to `gpt-5` for reasoning/tool-calling.
- Selectable Gemini Live on desktop and phone, with non-blocking desktop
  actions. Desktop Gemini uses private PipeWire echo cancellation and noise
  suppression without changing other applications' default audio devices.
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
- `set_reminder`/`list_reminders`/`clear_reminders` — Omarchy's own
  lightweight notification-popup reminders, not a new mechanism. Only
  understands minutes from now, so the assistant converts whatever time
  you gave ("in 20 minutes", "at 3pm") into a minute count itself.
- Workspace switching, window listing/focusing, fullscreen toggle, close,
  screenshots, screen lock.
- Launchers: terminal, browser, files, editor.
- Local files: `list_files`, `read_file`, and `write_file` let Omarchy AI
  work with text files in your home directory and `/tmp`. It creates a new
  file by default and only replaces an existing file when you explicitly
  ask it to. Add other project locations with `file_access_roots` in
  `~/.config/omarchy-ai/config.yaml`.
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
- `read_tile_log` — terminals the assistant opens have a full real-text
  transcript (via `script(1)`). Every interactive Bash or Zsh terminal
  also writes a compact command, working-directory, and exit-status context
  log, so Omarchy AI can understand terminals you opened yourself too.
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
- Asking to cast opens a small centered, glitchy device-picker overlay
  (`omarchy-ai.tv-discovery`) that stays live-updated while it's open and
  can be resolved by saying a device's name or by clicking it — it reads
  and writes the exact same shared device registry (`display/registry.py`)
  the voice agent itself uses, so the UI and the assistant never disagree
  about what's online.
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
  the phone uses WebRTC directly with OpenAI, or through this computer's
  Gemini bridge when Gemini is selected. API keys remain on the computer.
  Browser microphone capture requests echo cancellation and noise suppression.
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

**Connect other services (MyApi)**
- An "Enable" switch in the settings panel turns on a separate MyApi bar
  icon/panel (off by default) — from there, one code pasted from your
  [MyApi](https://www.myapiai.com) dashboard connects Gmail, Calendar,
  Drive, Notion, Slack, and 200+ other services (no OAuth redirect, no
  token ever shown). Requires a MyApi Pro/Heavy/Enterprise plan.
- That panel also shows live per-service usage — a proportional-share
  breakdown styled after Omarchy's own Agents bar panel, so it reads as a
  sibling of it rather than a one-off look.
- Once connected, the assistant prefers a real API call through MyApi over
  opening a browser and taking a screenshot whenever a request is covered
  by a connected service ("check my email" reads Gmail directly rather
  than paying for a vision call) — read-only for now, it can't send,
  create, or delete anything yet.
- Gmail attachment requests have a dedicated flow: Omarchy AI searches the
  requested mail for attachment IDs, then downloads the chosen file to
  `~/Downloads/Omarchy_AI/`. MyApi transports those provider reads through
  its execute endpoint; the assistant only exposes the allowlisted read
  operations, never Gmail send or mailbox mutation.
- `omarchy-ai-dashboard` — a live terminal dashboard (built with `rich`)
  showing which services are connected and how much each has actually been
  used, refreshed in real time from a local call log.

## Architecture, briefly

```
src/omarchy_ai/
  core/       daemon loop (listen -> wake -> converse -> listen), cross-
              session history and learned-preference memory
  voice/      wake word (openWakeWord), the gpt-live-1 WebRTC client
              (aiortc), the Watch Dogs/status-icon IPC bridges
  execution/  Hyprland/PipeWire/desktop actions, the tool schemas exposed
              to the model, per-terminal output logs retained after a
              terminal closes or service restart, the vision fallback
  display/    mDNS device discovery, the WebRTC signaling relay for casting
  phone/      the local HTTPS phone-bridge server + paired web page
  myapi/      the MyApi (myapiai.com) client — ASC Quick Connect, signed
              requests, the local usage log the terminal dashboard reads
  cli/        the omarchy-ai-settings CLI the settings panel shells out to,
              and omarchy-ai-dashboard (live MyApi usage in the terminal)
  policy/     scaffolded, not yet built (see "Known gaps" below)
android-receiver/   Kotlin/Compose Android TV receiver app (Gradle project)
quickshell/         the 4 Quickshell/QML user plugins (HUD overlay, window
                    labels, settings panel, MyApi panel) — see
                    quickshell/README.md
systemd/            omarchy-ai.service unit template
docs/                architecture decision record (ADR-0001) + dependency list
scripts/             the original WebRTC/API reverse-engineering spikes,
                      setup.sh / uninstall.sh
```

- **Python**, managed with [`uv`](https://astral.sh/uv), targeting the
  system Python (needs `--system-site-packages` for `python-gobject`/
  GStreamer bindings — see below).
- **Quickshell/QML** for every on-screen desktop UI piece (the HUD overlay,
  the window-label badges, the settings panel) — these are Omarchy *user
  plugins*, developed in place under `~/.config/omarchy/plugins/` and kept
  in this repo under [`quickshell/`](quickshell/) (see
  [`quickshell/README.md`](quickshell/README.md) for installing them).
- **`aiortc`** (pure Python) for the desktop voice client's own WebRTC
  connection to `gpt-live-1`.
- **`pywayland`** talking to `wlr-screencopy-unstable-v1` directly for
  screen capture (not the `xdg-desktop-portal` ScreenCast path, which
  proved permanently broken on this Hyprland/PipeWire combination).
- **Kotlin + Jetpack Compose** for the Android TV receiver app, using
  `stream-webrtc-android` (Maven Central's maintained `org.webrtc` drop-in).

Full reasoning behind each of these choices — including the dead ends — is
in [`docs/ADR-0001-architecture.md`](docs/ADR-0001-architecture.md).

## Installation

### Fast install (copy and paste)

On an x86-64 Omarchy desktop, paste this whole block into a terminal.
Internet access is required; missing system packages ask for sudo approval.
Existing API keys and settings are preserved.

```bash
(
set -euo pipefail
mkdir -p "$HOME/.local/share/omachy-ai-releases"
cd "$HOME/.local/share/omachy-ai-releases"
# Newest stable vX.Y.Z GitHub Release (skips demo-media and non-version tags).
version="$(curl -fsSL "https://api.github.com/repos/omribenami/Omarchy-AI/releases?per_page=100" \
  | grep -o '"tag_name": *"v[0-9]*\.[0-9]*\.[0-9]*"' | grep -o '[0-9]*\.[0-9]*\.[0-9]*' \
  | sort -V | tail -1)"
[ -n "$version" ] || { echo "Could not find an Omarchy AI release" >&2; exit 1; }
echo "Installing Omarchy AI $version"
package="omarchy-ai-${version}-linux-x86_64.tar.gz"
base="https://github.com/omribenami/Omarchy-AI/releases/download/v${version}"
curl -fL "$base/$package" -o "$package"
curl -fL "$base/$package.sha256" -o "$package.sha256"
sha256sum -c "$package.sha256"
tar -xzf "$package"
cd "omarchy-ai-${version}-linux-x86_64"
bash install.sh
systemctl --user enable --now omarchy-ai.service
systemctl --user restart omarchy-ai.service
)
```

The block looks up the newest stable `vX.Y.Z` GitHub Release itself, so it
never needs editing when a new version is published. This repository also has
a non-package `demo-media` release, which the tag filter skips. To install a
specific version instead, replace the `version=...` lines with, for example,
`version=0.4.1`. The `.sha256` file is
checked with `sha256sum` before the archive is unpacked.

Keep the extracted directory: the service runs from it. The installer installs
missing native packages, creates the locked Python environment, installs the desktop plugins,
copies the bundled wake-word models, creates the user config, and installs the
systemd service. It does not guess or overwrite your API key. Open the
**Omarchy AI** settings panel on the right side of the bar, choose **OpenAI**,
**Gemini**, or **Gateway voice**, add that provider's key, then
press **Apply saved changes**.
For Jev desktop and browser actions while using OpenAI Live or Gemini Live,
also save a **Jev / Vercel AI Gateway key** in the same panel. This integration
uses the Gateway key for Jev; it does not accept a separate TypeSafe token.
Keys are saved with owner-only permissions in `~/.config/omarchy-ai/key`
or `~/.config/omarchy-ai/gemini-key`; Omarchy-ai stores its Vercel AI Gateway
key in `~/.config/omarchy-ai/vercel-ai-gateway-key`. Saved keys show an
**Edit key** button.
New installs default to the `omachy` wake word and ASCII visualizer; existing
preferences are not overwritten. Refresh paired phone pages after upgrading.

For an existing source checkout, pull the update with `git pull --ff-only`,
run `bash install.sh` there, then restart `omarchy-ai.service`.

### Voice updates

Omarchy checks GitHub Releases at startup and every 15 minutes. A release
counts when its tag is a stable `vX.Y.Z` (for example `v0.4.1`) and it has
both `omarchy-ai-X.Y.Z-linux-x86_64.tar.gz` and the matching `.sha256` asset.
The `demo-media` release, drafts, and prereleases are ignored. On wake, it
refreshes an expired check with a 2-second foreground limit and recommends a
newer version in its first spoken reply. Offline checks do not prevent
conversation.

Say **“Check for updates”**, **“Update yourself”**, or **“What's the update
status?”**. Only an explicit update request starts installation. The updater
downloads the archive and its `.sha256` from the same release, verifies the
checksum, unpacks the bundle, and prepares a new Python environment before
stopping the assistant. It keeps your API keys, settings, conversation history,
and existing source checkout. The conversation disconnects when the new version
starts.

The updater runs in the separate `omarchy-ai-update.service` user unit. It
retains the previous installation and backs up the service and shell integration;
if setup or startup fails, it restores them. Progress and errors are stored in
`~/.local/state/omarchy-ai/updates/install.json`; detailed output is available via
`journalctl --user -u omarchy-ai-update.service`. Updates require existing native
runtime dependencies and `uv`; missing system packages are reported rather than
prompting for sudo from a background voice session. Android receiver updates
remain separate from the desktop assistant update.

Historical bundles still committed under [`dist/`](dist/) stay in the version
list. The highest version wins. When that version is a GitHub Release, the
download URL is
`https://github.com/omribenami/Omarchy-AI/releases/download/vX.Y.Z/omarchy-ai-X.Y.Z-linux-x86_64.tar.gz`
(and the sibling `.sha256`). That is the download GitHub counts. A failure to
list releases aborts the check.

Publish a GitHub Release tagged `vX.Y.Z` with a **higher `pyproject.toml`
version**, and attach both the archive and its `.sha256`. Same-version source
commits are not updates. Updates download and run the project's installer.
Integrity is the SHA-256 file shipped as a Release asset (the checksum is not
a separate publisher signature). See [Build a release package](#build-a-release-package).
Leave the historical `dist/` archives in git; new tarballs are Release assets.

If a self-update fails and this machine has a GitHub issue token configured
(below), Omarchy automatically files a GitHub issue on this repo with the
failure details and reports the issue's URL via `get_update_status`; without
a token it reports honestly that no issue could be filed rather than pretending
one was. The same mechanism backs a general `report_issue` voice tool — say
something like *"file an issue about this"* for any problem, not just a failed
update. Set the token with:
```bash
GITHUB_ISSUE_TOKEN=<a fine-grained PAT, Issues: write only on omribenami/Omarchy-AI> \
  .venv/bin/python -m omarchy_ai.cli.settings set-github-issue-token
```
This is opt-in and per-machine — no install ships with a token, and nothing
tries to file an issue on a machine that hasn't set one. The token is stored
0600 at `~/.config/omarchy-ai/github-issue-token`;
`forget-github-issue-token` removes it.

### Jev desktop worker with live conversation

OpenAI and Gemini Live can delegate native OS goals to `desktop_task` while
remaining the conversational model. Jev selects typed operations and observed
targets; code checks freshness, executes existing actions and verifies native
state afterward. Supported areas are workspaces, window focus, output volume,
brightness, installed themes and bar panels. Vision, generated text, complex
planning and unsupported work remain with the live model and its other tools.

The worker uses the configured Vercel Gateway key. `search_os_knowledge` retrieves
the packaged Omarchy expert guide, capability registry and Arch operation notes.
Uncertain decisions and unverified outcomes return a trace and any verified
steps to the live model. See [research, architecture and measured limits](docs/JEV-DESKTOP.md).

Web tasks run through the upstream
[`browser-use/jev-ultrafast`](https://github.com/browser-use/jev-ultrafast)
`Agent` with screenshots disabled. Each DOM decision explicitly requests
`typesafe-ai/jev`; a small Gateway text model is used only when a field needs
generated text. Release bundles include a pinned Jev Ultrafast wheel and the
installer verifies that its `Agent` imports successfully. Browser runs stop at
20 actions or 45 seconds rather than continuing an unproductive loop.

### Omarchi-ai (Jev + Vercel AI Gateway)

Omarchi-ai is the third provider. It uses `typesafe-ai/jev` through Vercel AI
Gateway for typed, confidence-aware action/end-of-conversation/risk decisions;
Jev is an evaluation model, not a chat text generator. Gateway's
`openai/gpt-4o-mini-transcribe` performs automatic multilingual transcription,
`openai/tts-1` returns low-latency PCM speech, and a compact Gateway language
model supplies conversational wording and tool arguments only after Jev's
decision. The existing Watch Dogs visualizer remains live because it is driven
from the PCM samples sent to PipeWire.

This provider captures one spoken turn at a time (the configured speak window),
unlike the WebRTC full-duplex OpenAI and Gemini Live providers. The phone bridge
continues to listen securely on port 8766 for pairing, mirroring, and the two
realtime phone providers; Gateway turn-based phone voice is not exposed as a
misleading WebRTC Live session.

### Persistent preferences

You can give the assistant a standing instruction in conversation, for
example: “Always type terminal commands in English.” It saves explicit,
general preferences with its `remember_preference` tool and applies them in
future sessions. One-off requests are not saved as preferences.

### Release package

Download `omarchy-ai-<version>-linux-x86_64.tar.gz` and its `.sha256` file
from the GitHub Release `v<version>`
(`https://github.com/omribenami/Omarchy-AI/releases/download/v<version>/`),
verify it, then unpack and install:

```bash
sha256sum -c omarchy-ai-<version>-linux-x86_64.tar.gz.sha256
tar -xzf omarchy-ai-<version>-linux-x86_64.tar.gz
cd omarchy-ai-<version>-linux-x86_64
./install.sh
```

The bundle includes the Android receiver at
`android/omarchy-ai-receiver.apk`. The installer sets up the desktop service
and plugins, and requests installation of missing native packages via pacman.
It does not alter firewall rules. It prints the `adb install -r` command for
the receiver. The archive contains the app wheel/source, dependency lockfile,
plugins, wake models, and prebuilt APK; Python dependencies and native packages
are downloaded during installation, so this is not an offline installer.

### From source

**1. Native dependencies** (needs `sudo`; see
[`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) for the full list and why
each is needed):

```bash
sudo pacman -S --needed android-tools gst-plugins-bad gst-plugins-good \
  gradle qrencode python-gobject pipewire-audio pipewire-pulse libpulse uv
yay -S android-sdk-cmdline-tools-latest
```

GStreamer, PipeWire, `xdg-desktop-portal-hyprland`, `avahi-daemon`, and
`python-gobject` ship on a stock Omarchy install already — the setup script
checks them, and `install.sh` installs missing runtime packages. The Android
SDK and Gradle are needed only to rebuild the receiver, not to use the bundle.

**2. Clone and install:**

```bash
git clone https://github.com/omribenami/Omarchy-AI.git ~/Git/omarchy-ai
cd ~/Git/omarchy-ai
bash install.sh
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

**4. Wake word:** three real, trained openWakeWord models ship in
[`wake_models/`](wake_models/) — "omachy" (the default), "omri", and
"roni" — installed to `~/.config/omarchy-ai/wake_models/` automatically by
`setup.sh`. Any of the three wakes it; drop in more `.onnx` models of your
own there to add to the set, or remove these to replace them entirely.

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

**7. Android receiver app** (only needed for TV/projector casting): the
release package includes a ready-to-install APK. From source, it is built
automatically on first `install_receiver_on_tv` call, or manually via
`cd android-receiver && mise exec -- ./gradlew :app:assembleDebug`. Building
from source needs the JDK/Android SDK pinned in `mise.toml` — run `mise
install` in that directory first.

**8. Connect services via MyApi** (optional, needs a
[MyApi](https://www.myapiai.com) Pro/Heavy/Enterprise account): flip
"Enable" in the settings panel's "Connect services to Omarchy AI" section
— a separate MyApi bar icon appears — open it, click through to
myapiai.com to generate a one-time connection code from your dashboard,
paste it in, and click Connect. Without the Quickshell panels installed,
the same mechanism is reachable directly:

```bash
echo -n "MYAPI-XXXXXXXX-XXXXXXXX" | .venv/bin/omarchy-ai-settings connect-myapi
```

Then `omarchy-ai-dashboard` shows live per-service usage in a terminal.

### Build a release package

Maintainers build the archive from a clean, committed checkout, then publish
it as a GitHub Release. The tarball is not a new git commit.

1. Bump `version` in `pyproject.toml`, run `uv lock`, and set the same
   `version=` in the fast-install block above. Commit and push that source.
2. Build:

```bash
./scripts/build-install-package.sh
```

This writes `dist/omarchy-ai-<version>-linux-x86_64.tar.gz` and a SHA-256
checksum beside it. It builds the Python wheel and source distribution, a
fresh Android receiver APK, and packages all tracked runtime files. Native
Omarchy packages are verified by `scripts/check-dependencies.sh` during
installation and remain listed in [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md).
Pass `--skip-android` to omit the Gradle build (the slowest step) and ship a
bundle without the receiver APK — `install.sh` skips the optional `adb install`
step when it's absent. `dist/` is gitignored for files that are not already
tracked. Leave the historical archives in place; do not `git add` a new tarball.

3. Publish tag `v<version>` with those two files as Release assets. HEAD must
   be `origin/main` (push the version commit first). Dry-run prints the `gh`
   command and the REST API steps; `--publish` performs the upload. The
   fast-install block pins `version=0.3.10`. Publish tag `v0.3.10` from the
   archive already stored in `dist/` (no rebuild required if that file is the
   one you want to ship) so the install command has an asset to download.

```bash
.venv/bin/python scripts/publish-github-release.py          # inspect
.venv/bin/python scripts/publish-github-release.py --publish
```

The publisher uses the `gh` CLI when it is on `PATH`. Otherwise it uses
`GH_TOKEN` or `GITHUB_TOKEN` (contents: write) against the GitHub REST API:
create `POST /repos/omribenami/Omarchy-AI/releases` when tag `v<version>` has
no release yet, then upload each file to
`https://uploads.github.com/repos/omribenami/Omarchy-AI/releases/<id>/assets?name=<filename>`.
Re-running deletes and replaces those two assets. The exact headers and JSON
body are in the script's dry-run output and its module docstring.

GitHub counts downloads of each Release asset. On the release page the count
is beside the file. API:
`GET https://api.github.com/repos/omribenami/Omarchy-AI/releases/tags/v<version>`
field `assets[].download_count`. The `.tar.gz` count is the package download
count; the `.sha256` count is separate.

```bash
gh release view v<version> --repo omribenami/Omarchy-AI --json assets \
  --jq '.assets[] | {name, downloadCount}'
```

If the checkout has no local GitHub credential, a connected MyApi identity can
still publish the **source commit** through its GitHub connection. It refuses
a dirty checkout, refuses to advance `main` if the remote moved after it was
fetched, and refuses to push `dist/omarchy-ai-*.tar.gz` or its `.sha256`
(those bytes are the Release upload above, which MyApi's JSON GitHub proxy
does not send):

```bash
.venv/bin/python scripts/publish-via-myapi.py          # inspect the exact change set
.venv/bin/python scripts/publish-via-myapi.py --publish # create one commit on main
```

Run `scripts/publish-github-release.py` on a machine that has `gh` or a token
after that commit is on `main`.

### Known gaps

Documented honestly rather than papered over:

- **The daemon degrades gracefully without the Quickshell plugins enabled**
  (the IPC calls to the HUD/window-labels/status-dot just log a warning and
  continue) — but note that `omarchy-ai.settings/Panel.qml`'s path to the
  settings CLI is hardcoded to this project's original checkout location;
  see [`quickshell/README.md`](quickshell/README.md) if you cloned
  somewhere else.
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
- **MyApi calls are read-only** — Gmail attachment search/download uses
  MyApi's POST-based provider-read transport, while the generic service
  tool remains GET-only. Sending an email or creating a calendar event
  through MyApi isn't wired up.
  Disconnecting from the Omarchy AI settings panel only stops this machine
  from using the connection — no programmatic revoke was found on MyApi's
  side, so fully cutting access also means removing the device from your
  MyApi dashboard.

See [`STATUS.md`](STATUS.md) for the full, unabridged debugging history —
every bug found, how it was diagnosed, and how it was fixed — and
[`docs/ADR-0001-architecture.md`](docs/ADR-0001-architecture.md) for the
architecture decisions behind all of the above.

## License

MIT — see [LICENSE](LICENSE).

### Phone voice over Tailscale and mirrored-TV audio

The HTTPS phone bridge listens on port **8766** on LAN and Tailscale.
When Tailscale is connected, a newly generated **Pair phone** QR uses its
IPv4 address; otherwise it uses LAN. The TLS certificate includes both
addresses and the Tailscale DNS name. Pair again when changing hostname/IP:
browser pairing cookies belong to the address used. Restart the assistant
service if Tailscale is first connected after the service started, to refresh
the certificate. Both devices must be on the tailnet and its access rules
must permit TCP 8766. Direct access uses the existing self-signed certificate.

The mobile page is a full-screen glyph field: red when idle, green ripples
where you tap, and desktop cyan while live, responding to assistant audio.
Tap the field to start/end a conversation; transcripts are optional.

During a connected screen mirror, **Audio: phone → TV** routes assistant
speech into the casting sender's existing audio track. Tap **Audio: TV** to
return to the phone. Ending mirroring or a failed audio upload automatically
returns playback to the phone. This uses the existing Android receiver and
requires no new APK. Start a fresh mirror after updating the sender script.
The phone must support AudioWorklet (HTTPS); PCM uploads are bounded and drop
packets on slow links to avoid accumulating delayed speech.

Tap **Text** to type without microphone access. Replies stream into the
transcript and text mode stays silent. **Voice** switches the same connected
session back to microphone input. Connection failures preserve the draft.

### Mirror recovery and TV microphones

Mirroring runs in `omarchy-ai-cast.service`, independently of voice conversations
and assistant restarts. Ask the assistant to start/stop casting as usual.
Connection failures and bounded retries are recorded in:

```sh
journalctl --user -u omarchy-ai-cast.service -u omarchy-ai-signaling.service -f
```

A USB microphone or wired headset with a microphone plugged into the receiver
can supply the assistant's wake word and conversation audio. Grant the receiver's
microphone permission; **TV microphone active** appears when the external input
is available. Desktop and TV wake-word detection run independently. A conversation
uses the microphone that heard its wake word; losing TV audio falls back to the
desktop microphone. An attached TV microphone never disables desktop wake detection.
