# Omarchy-AI

**Omarchy-AI** is a self-hosted, voice-driven **agentic assistant** for
[Omarchy](https://omarchy.org), the Arch-based Hyprland desktop. You say what
you want done, from the desk, from your phone, or from the TV across the
room. It plans the work, does it with real tools on your machine, checks the
result itself, and tells you when it is verified. It is not a chatbot bolted
onto a terminal.

Project home: <https://github.com/omribenami/Omarchy-AI>

<div align="center">

https://github.com/user-attachments/assets/ea736181-9cf3-423a-b7d5-91a895fe6589

</div>

This page is that product's marketplace front door. The marketplace lists
one Quattro plugin, `omarchy-ai.settings`. Enabling it installs the pinned
assistant release, starts that assistant, and loads this settings panel.

## Features

- **It operates the whole machine.** Desktop, windows and workspaces, its
  own terminals and yours, a real browser, files, system administration,
  and code through Claude Code or Codex, with about 90 typed tools and all
  of Omarchy's ~230 commands behind one voice.
- **Whole jobs, not single commands.** A background Task Runtime plans the
  job, routes each step to the right worker, and certifies the result from
  evidence, not from its own claims.
- **Wake word and overlays.** Say the wake word ("omachy"), press
  **Super + `**, or type with **Super + Ctrl + `**. Watchdog, task, and
  routine HUDs stay on screen, and a waiting approval floats as an envelope.
- **Asks before anything risky.** Installs, pushes, service restarts,
  deletes, and root need your OK. A waiting approval is announced out loud
  with Approve / Deny; root needs a click, not just a spoken yes.
- **Works alongside you.** Its commands run in its own terminals, so it
  never takes your keyboard. "Tell me when the build finishes" sets up a
  real watch, and the assistant wakes up to do the next step you asked for.
- **Phone and TV.** From a paired phone, talk or type, watch the desktop
  live, or send the picture to the TV. Cast screen and audio to a paired
  Android TV or projector. While casting, the TV's microphone can carry the
  conversation.

**Phone session**: mirror the screen to your phone and operate the PC by talking to Omarchy.

<div align="center">

https://github.com/user-attachments/assets/7abed3fa-ed55-4835-b77a-4d0a1ab85f1f

</div>

**Phone bridge**: talk from a paired phone to Omarchy while mirroring the PC to Android TVs in your network.

<div align="center">

https://github.com/user-attachments/assets/6d20a7b9-3806-4248-be12-83bdddf63f66

</div>

## Install

Project home: <https://github.com/omribenami/Omarchy-AI>

```bash
omarchy plugin add @LISTING_CLONE_URL@ --enable
```

Omarchy clones this repository into
`~/.config/omarchy/plugins/omarchy-ai.settings`, validates `manifest.json`,
and can enable the plugin in the same step. Plugins run as unsandboxed code
inside the shell; read the files before you confirm.

Enabling the plugin runs `start-assistant.sh`. That script downloads the
pinned Omarchy-AI release, checks the sha256 stored in the script, unpacks
it, and starts the assistant. The download is saved and checked before it
is unpacked. It is not piped into a shell. The settings panel is this same
plugin. No path in `Panel.qml` is edited.

Open **Omarchy-AI** on the bar after that setup finishes. The panel looks
up `omarchy-ai-settings` when it opens. Choose a provider, save a key, and
apply the change.

The widget has no default bar section. If the icon does not show up where
you want it, place it on the right:

```bash
omarchy bar move omarchy-ai.settings --section right
```

A source checkout can still be installed with `bash install.sh` for
development. If that checkout already owns `omarchy-ai.settings`, keep
that copy. It is the same plugin id, and Omarchy will refuse a second one.

## Remove

```bash
omarchy plugin remove omarchy-ai.settings
```

Removal disables the widget and deletes this git checkout. It does not
stop the assistant, and it does not delete your API keys or
`~/.config/omarchy-ai/`.

## How the panel finds the assistant

The widget does not bake an install path, and enabling it does not require
a path edit. `start-assistant.sh` puts the assistant where this lookup
already searches. `resolve-settings.sh` looks up `omarchy-ai-settings` when
the panel opens:

1. `OMARCHY_AI_SETTINGS`, when that variable is an executable file.
2. The user service `omarchy-ai.service` written by `install.sh`
   (`WorkingDirectory/.venv/bin/omarchy-ai-settings`, or the venv named
   on `ExecStart`).
3. `omarchy-ai-settings` on `PATH`.
4. The newest fast-install tree under
   `~/.local/share/omachy-ai-releases/omarchy-ai-<version>-linux-x86_64/`.
5. The newest self-update tree under
   `~/.local/share/omarchy-ai/releases/`.

`XDG_CONFIG_HOME` and `XDG_DATA_HOME` are honored. When none of those
locations has the command yet, the panel stays open and says that setup
has not finished.

`omarchy plugin update` checks this repository out again. The lookup ships
in the plugin, so an update does not put a path placeholder back.

## License

MIT. Copyright (c) 2026 Omri Ben-Ami. See [LICENSE](LICENSE).
