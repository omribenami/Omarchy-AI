# Quickshell plugins

The five Omarchy shell (Quickshell) user plugins this project's desktop UI
is built from — developed and hot-reloaded in place under
`~/.config/omarchy/plugins/` for the whole life of this project, brought
into this repo so a fresh clone doesn't have to rebuild them from scratch:

| Plugin | What it is |
| --- | --- |
| `omarchy-ai.settings` | Bar-widget settings panel — wake word model picker, sensitivity, voice, OpenAI API key entry, Watch Dogs display mode, phone-bridge pairing (QR code), and the MyApi on/off switch. |
| `omarchy-ai.watchdog` | The "Watch Dogs" HUD overlay — live tool-call feed and/or an ASCII/braille audio visualizer while a conversation is active. |
| `omarchy-ai.window-labels` | Floating name-label badges dropped over candidate windows when the assistant needs you to point at (or name) the right one. |
| `omarchy-ai.myapi` | Separate bar-widget for the MyApi (myapiai.com) integration — connect/disconnect and live per-service usage. Hidden until "Enable" is switched on in `omarchy-ai.settings`. |
| `omarchy-ai.tv-discovery` | Small centered, click-through-except-itself device picker shown while a cast/mirror target is being resolved — reads/writes the same `display/registry.py` device state the voice agent itself uses, live-updates while open, and can be resolved by voice or by clicking a row. |

## Installing

Run `bash scripts/install-plugins.sh` from the repository root to install or update
all five plugins (with backups) and register the MyApi bar widget. `scripts/setup.sh`
also runs this installer. The MyApi icon stays hidden until enabled in settings.

For manual installation, copy each plugin folder into Omarchy's user plugin directory, then enable it:

```bash
cp -r quickshell/plugins/omarchy-ai.settings \
      quickshell/plugins/omarchy-ai.watchdog \
      quickshell/plugins/omarchy-ai.window-labels \
      quickshell/plugins/omarchy-ai.myapi \
      quickshell/plugins/omarchy-ai.tv-discovery \
      ~/.config/omarchy/plugins/

omarchy plugin enable omarchy-ai.settings
omarchy plugin enable omarchy-ai.watchdog
omarchy plugin enable omarchy-ai.window-labels
omarchy plugin enable omarchy-ai.myapi
omarchy plugin enable omarchy-ai.tv-discovery
```

`omarchy-ai.settings` and `omarchy-ai.myapi` are `bar-widget`s — if either
doesn't show up in the bar on its own after enabling, place it explicitly
(`omarchy-ai.myapi` only actually renders an icon once its "Enable" switch
in `omarchy-ai.settings` is on — see that plugin's Panel.qml):

```bash
omarchy bar move omarchy-ai.settings --section right
omarchy bar move omarchy-ai.myapi --section right
```

The other two are background panels (`keepLoaded: true`) driven entirely by
IPC calls from the Python daemon (`voice/watchdog.py`,
`voice/status_icon.py`, `execution/actions.py`'s `show_window_labels`) —
nothing to place in the bar, they just need to be enabled so Quickshell
loads them.

## A real, known limitation

`omarchy-ai.settings/Panel.qml` **and** `omarchy-ai.myapi/Panel.qml` both
shell out to this project's settings CLI via the same **hardcoded absolute
path**:

```qml
readonly property string py: "/home/ben-ami/Git/omarchy-ai/.venv/bin/omarchy-ai-settings"
```

This project has so far only ever run on one machine, at one checkout path
— if you clone this somewhere other than `~/Git/omarchy-ai` (or as a
different user), edit that line in **both** files to match your actual
path before enabling either plugin. Same story for the systemd unit
template (`systemd/omarchy-ai.service`) and its `@VENV@` substitution —
see the main [README](../README.md#manual-installation) for the install
steps that account for this.
