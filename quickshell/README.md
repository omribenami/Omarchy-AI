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
| `omarchy-ai.quota-alert` | Red dollar signs popping over every screen, with the provider's name, when OpenAI, Gemini or Vercel AI Gateway credits/quota run out (`src/omarchy_ai/core/quota.py`). Click-through, closes itself after ~6.5s. Try it with `.venv/bin/omarchy-ai-settings test-quota-alert openai`. |

## Installing

Run `bash scripts/install-plugins.sh` from the repository root to install or update
all six plugins (with backups) and register the MyApi bar widget. `scripts/setup.sh`
also runs this installer. The MyApi icon stays hidden until enabled in settings.

For manual installation, copy each plugin folder into Omarchy's user plugin directory, then enable it:

```bash
cp -r quickshell/plugins/omarchy-ai.settings \
      quickshell/plugins/omarchy-ai.watchdog \
      quickshell/plugins/omarchy-ai.window-labels \
      quickshell/plugins/omarchy-ai.myapi \
      quickshell/plugins/omarchy-ai.tv-discovery \
      quickshell/plugins/omarchy-ai.quota-alert \
      ~/.config/omarchy/plugins/

omarchy plugin enable omarchy-ai.settings
omarchy plugin enable omarchy-ai.watchdog
omarchy plugin enable omarchy-ai.window-labels
omarchy plugin enable omarchy-ai.myapi
omarchy plugin enable omarchy-ai.tv-discovery
omarchy plugin enable omarchy-ai.quota-alert
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

## Settings CLI path

`omarchy-ai.settings` looks up `omarchy-ai-settings` when the panel opens
(`resolve-settings.sh`: the user systemd unit from `install.sh`, `PATH`,
or the known release directories). A marketplace install of that plugin
does not need a path written into `Panel.qml`. If the assistant is not
installed, the panel explains the Releases / `install.sh` install.

The other plugins that shell out to the same CLI (`omarchy-ai.myapi`,
`omarchy-ai.tv-discovery`, `omarchy-ai.assistant-huds`,
`omarchy-ai.chat-hud`) still carry an install-time path token.
`scripts/install-plugins.sh` rewrites it to
`.venv/bin/omarchy-ai-settings` for the checkout that ran `install.sh`.
Those plugins are not the marketplace listing. The systemd unit template
(`systemd/omarchy-ai.service`) is still rendered by `scripts/setup.sh`.
