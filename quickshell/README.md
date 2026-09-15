# Quickshell plugins

The three Omarchy shell (Quickshell) user plugins this project's desktop UI
is built from — developed and hot-reloaded in place under
`~/.config/omarchy/plugins/` for the whole life of this project, brought
into this repo so a fresh clone doesn't have to rebuild them from scratch:

| Plugin | What it is |
| --- | --- |
| `omarchy-ai.settings` | Bar-widget settings panel — wake word model picker, sensitivity, voice, OpenAI API key entry, Watch Dogs display mode, phone-bridge pairing (QR code). |
| `omarchy-ai.watchdog` | The "Watch Dogs" HUD overlay — live tool-call feed and/or an ASCII/braille audio visualizer while a conversation is active. |
| `omarchy-ai.window-labels` | Floating name-label badges dropped over candidate windows when the assistant needs you to point at (or name) the right one. |

## Installing

Copy each plugin folder into Omarchy's user plugin directory, then enable it:

```bash
cp -r quickshell/plugins/omarchy-ai.settings \
      quickshell/plugins/omarchy-ai.watchdog \
      quickshell/plugins/omarchy-ai.window-labels \
      ~/.config/omarchy/plugins/

omarchy plugin enable omarchy-ai.settings
omarchy plugin enable omarchy-ai.watchdog
omarchy plugin enable omarchy-ai.window-labels
```

`omarchy-ai.settings` is a `bar-widget` — if it doesn't show up in the bar
on its own after enabling, place it explicitly:

```bash
omarchy bar move omarchy-ai.settings --section right
```

The other two are background panels (`keepLoaded: true`) driven entirely by
IPC calls from the Python daemon (`voice/watchdog.py`,
`voice/status_icon.py`, `execution/actions.py`'s `show_window_labels`) —
nothing to place in the bar, they just need to be enabled so Quickshell
loads them.

## A real, known limitation

`omarchy-ai.settings/Panel.qml` shells out to this project's settings CLI
via a **hardcoded absolute path**:

```qml
readonly property string py: "/home/ben-ami/Git/omarchy-ai/.venv/bin/omarchy-ai-settings"
```

This project has so far only ever run on one machine, at one checkout path
— if you clone this somewhere other than `~/Git/omarchy-ai` (or as a
different user), edit that one line to match your actual path before
enabling the plugin. Same story for the systemd unit template
(`systemd/omarchy-ai.service`) and its `@VENV@` substitution — see the
main [README](../README.md#manual-installation) for the install steps that
account for this.
