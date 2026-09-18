---
name: omarchy-expert
description: Expert operational knowledge for inspecting, controlling, customizing, and troubleshooting current Omarchy Quattro systems. Use for any Omarchy OS, Hyprland desktop, Omarchy shell, device, package, service, or configuration task.
---

# Omarchy OS Expert

> Pinned upstream: `quattro` commit `9c5482c58dbe4974de337450754885083c91eada` (2026-09-16T18:09:20+02:00); generated 2026-09-18T07:01:26+00:00.

This file is designed as an assistant skill, not as a human cheat sheet. The exact machine-readable inventory is in `omarchy_capabilities.json`.

## Mandatory operating contract

1. Inspect before acting. Never invent identifiers, paths, devices, process IDs, monitor names, workspaces, windows, audio nodes, Bluetooth addresses, or NetworkManager connection names.
2. Prefer a documented `omarchy …` route. Use the lower-level subsystem only when no native route exists or when observation is required.
3. Read the command help and, when behavior is unclear, its packaged source before running it: `omarchy <route> --help`; `sed -n '1,240p' "$(command -v omarchy-…)"`.
4. For user customization, never edit `/usr/share/omarchy/`. It is package-owned and updates overwrite it. Put overrides in `~/.config/`.
5. Back up a user config before a non-trivial edit, preserve comments and unrelated settings, apply the narrowest change, then validate.
6. Verify every state-changing action through an independent state query. A zero exit status is not sufficient when the result is observable.
7. Ask immediately before destructive, privilege-expanding, security/credential, system update, package removal, disk, reboot, shutdown, logout, or broad reset/reinstall actions.
8. Do not expose secrets in commands, logs, screenshots, or process arguments. Redact tokens, passwords, Wi-Fi credentials, and private keys.
9. Do not use `sudo` speculatively. In a visible interactive terminal use `sudo` when required; in a non-interactive graphical flow use `pkexec` only where appropriate. Never wrap commands that elevate internally.
10. If facts differ from this pinned reference, the installed system is authoritative: inspect `omarchy commands --all --json`, the command source, user config, and current state.

## Architecture and authority

| Layer | Role | Authoritative observation/config |
|---|---|---|
| Omarchy CLI | Stable user-facing command dispatcher | `omarchy commands --all --json`; `omarchy … --help` |
| Omarchy package | Scripts, defaults, migrations, shell source | `/usr/share/omarchy/` (read-only for customization) |
| User overrides | Durable personal customization | `~/.config/hypr/`, `~/.config/omarchy/`, terminal configs |
| Hyprland | Windows, workspaces, monitors, input | `hyprctl … -j`; `~/.config/hypr/` |
| Omarchy shell | Bar, panels, notifications, OSD | `~/.config/omarchy/shell.json` |
| PipeWire/WirePlumber | Audio routing and volume | `wpctl status` |
| NetworkManager | Network and Wi-Fi | `nmcli` |
| BlueZ | Bluetooth | `bluetoothctl` |
| systemd/journald | Services, sessions, logs | `systemctl`; `journalctl` |
| pacman/AUR | Packages | Prefer `omarchy pkg …`; inspect with `pacman -Q*` |

## Universal execution loop

1. Restate the requested end state and identify its subsystem.
2. Query current state in structured form (`-j`/JSON where available).
3. Resolve exact targets; if ambiguous, ask the user.
4. Check `omarchy commands --all --json` or the relevant group help.
5. Classify risk and request confirmation if required.
6. Execute the narrowest native command.
7. Query state again and compare against the intended result.
8. If verification fails: capture stderr/status, inspect logs/config errors, make at most one evidence-based correction, then report clearly.

## Observation recipes

```bash
omarchy commands --all --json
omarchy version
hyprctl monitors -j
hyprctl clients -j
hyprctl activewindow -j
hyprctl activeworkspace -j
hyprctl workspaces -j
hyprctl devices -j
hyprctl configerrors
wpctl status
nmcli -t general status
nmcli -t device status
nmcli -t connection show --active
bluetoothctl show
bluetoothctl devices Connected
systemctl --user --failed
systemctl --failed
```

## High-value playbooks

### Window / workspace / monitor

- Observe: `hyprctl activewindow -j`, `clients -j`, `monitors -j`, `workspaces -j`.
- Select by exact address after matching class/title; do not rely on “the first window.”
- Execute with an Omarchy/Hyprland-native action. For direct compositor dispatch, use documented `hyprctl dispatch …` syntax for the installed Hyprland version.
- Verify the window address now reports the requested workspace/monitor/fullscreen/floating state.

### Audio

- Observe nodes and defaults with `wpctl status`.
- Resolve the exact sink/source ID and distinguish output mute from microphone mute.
- Prefer the `omarchy audio …` routes listed below.
- Verify default node and volume/mute state with `wpctl status` and `wpctl get-volume …`.

### Network and Bluetooth

- Observe first with `nmcli` or `bluetoothctl`; never guess a connection name or MAC address.
- Connecting to a new network/device can disclose credentials or alter connectivity; obtain confirmation when credentials/pairing are involved.
- Verify active connection or `Connected: yes`; on failure inspect `journalctl -u NetworkManager -b` or Bluetooth logs.

### Configuration changes

- Read the active user file and matching packaged default.
- Back up only the file being changed.
- Edit the smallest scope under `~/.config/`.
- Hyprland: `hyprctl reload` then `hyprctl configerrors`.
- Shell JSON/plugin files normally hot-reload; verify the UI/state. Terminal config may require `omarchy restart terminal`.
- `omarchy refresh …` is a reset operation: it backs up then restores defaults, and requires user confirmation.

### Packages and updates

- Inspect installed state first (`pacman -Q`, `pacman -Qi`).
- Prefer `omarchy pkg …` and `omarchy update`; never run competing package managers concurrently.
- Package removal and full updates require confirmation. Do not bypass conflicts blindly.
- Verify package/version state and inspect pacman logs when needed.

### Troubleshooting ladder

1. Reproduce narrowly and capture exact stderr/exit status.
2. `omarchy debug --no-sudo --print` (these flags avoid hanging on an interactive prompt).
3. Check `hyprctl configerrors`, failed user/system services, and relevant boot logs.
4. Compare user config with packaged defaults; do not overwrite customizations.
5. Use a targeted restart before a reset.
6. Use `omarchy refresh <component>` only with confirmation; use reinstall only as a last resort.

## Complete Omarchy command catalog

Total: **456 routes** — 380 public and 76 hidden. Hidden means omitted from the default listing, not automatically safe or intended for direct use.

### `agent`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy agent` | `[--inline] [--pick]` | Launch the default coding agent in a terminal | no | no | medium |
| `omarchy agent crash` | `<pid> [comm] [exe] [signal]` | Diagnose a crashed process with the default coding agent | no | no | medium |
| `omarchy agent prompt` | `[--inline] <prompt...>` | Launch the default coding agent with a prompt | no | no | medium |
| `omarchy agent usage claude` | `[--force] [--limits-only]` | Print the Claude Code usage record as JSON | yes | no | read-only |
| `omarchy agent usage codex` | `[--force] [--limits-only]` | Print the Codex usage record as JSON | yes | no | read-only |
| `omarchy agent usage fireworks` | `[--force] [--limits-only]` | Print the Fireworks usage record as JSON | yes | no | read-only |
| `omarchy agent usage update` | `[--force] [--limits-only] [--except <agent>] [agent...]` | Regenerate the AI agent usage data files | no | no | high |

### `apply`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy apply hardware` | `` | Apply Omarchy hardware-specific packages and system configuration | yes | yes | low |
| `omarchy apply lock` | `` | Configure Quickshell lock screen authentication | yes | yes | medium |
| `omarchy apply system` | `` | Apply Omarchy system setup in the installed target | yes | yes | high |

### `ascii`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy ascii` | `[text...]` | Render text as ASCII art in the font the Omarchy logo is drawn in | no | no | low |

### `audio`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy audio input mute` | `` | Toggle microphone mute. Drives the hardware mic-mute LED on laptops that expose one. | no | no | high |
| `omarchy audio input set default` | `<node-id> <source-name>` | Set the default audio input and move active streams | no | no | medium |
| `omarchy audio output set default` | `<node-id> <sink-name>` | Set the default audio output and move active streams | no | no | medium |
| `omarchy audio output sink` | `[sink-name]` | Print the sink whose volume and mute a given output really uses | no | no | low |
| `omarchy audio output switch` | `` | Switch between audio outputs while preserving the mute status | no | no | read-only |
| `omarchy audio output volume` | `<raise\|lower\|mute-toggle\|+N\|-N>` | Adjust output volume and show the Omarchy OSD | no | no | read-only |
| `omarchy audio sink availability` | `` | Print PulseAudio sink availability for the shell | no | no | low |
| `omarchy audio source switch` | `[next\|previous]` | Cycle to the next media source and transfer playback when the current source is playing | no | no | low |
| `omarchy audio tuning` | `<on\|off\|status\|match\|fronted-sink> [--force]` | Manage the speaker tuning for this laptop | no | no | low |

### `bar`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy bar` | `use <id> \| reset \| defaults \| position <top\|bottom\|left\|right> \| transparent <true\|false\|toggle> \| put <id> [placement] \| move <id> [placement] \| set <id> <key> <value> [--json] [placement]` | Configure the bar and its widget layout | no | no | read-only |
| `omarchy bar text color` | `` | Choose a legible transparent bar text color | yes | no | low |

### `battery`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy battery low` | `<percentage>` | Send the low battery warning notification and run battery-low hooks. | yes | no | medium |
| `omarchy battery present` | `` | Returns true if a battery is present on the system. | no | no | read-only |
| `omarchy battery status` | `[--shell]` | Returns a formatted battery status string with percentage and power draw/charge. | no | no | high |

### `bluetooth`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy bluetooth device` | `[pair\|connect\|disconnect\|forget] <address>` | Control a Bluetooth device | no | no | low |
| `omarchy bluetooth power` | `<on\|off\|toggle\|is-on>` | Turn Bluetooth on or off, remembered across reboots | no | no | high |

### `branding`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy branding about` | `<image\|text\|reset>` | Edit, set, or reset About branding | no | no | high |
| `omarchy branding about-animation` | `` | Shared helpers for animating the About branding (source this, don't run it). | yes | no | low |
| `omarchy branding screensaver` | `<image\|text\|reset>` | Edit, set, or reset screensaver branding | no | no | high |

### `brightness`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy brightness display` | `[--no-osd] [--monitor name] [+N%\|N%-\|N%\|off\|on]` | Show or adjust brightness on the focused display. | no | no | read-only |
| `omarchy brightness display apple` | `[--no-osd] [+N%\|N%-\|N%]` | Show or adjust Apple Studio Display and Apple XDR Display brightness using asdcontrol. | no | no | read-only |
| `omarchy brightness display ddc` | `<monitor> [+N%\|N%-\|N%]` | Show or adjust DDC/CI display brightness for a Hyprland monitor. | no | no | read-only |
| `omarchy brightness keyboard` | `[--no-osd] <up\|down\|cycle\|off\|restore>` | Adjust keyboard backlight brightness using available steps. | no | no | low |
| `omarchy brightness keyboard mute` | `<on\|off>` | Set the mic-mute indicator LED on laptops that expose a platform::micmute LED node. | no | no | medium |

### `capture`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy capture qr` | `` | Decode a QR code from a screenshot region | no | no | low |
| `omarchy capture region` | `[region\|windows\|smart\|fullscreen] [--keep-freeze] [--match-monitor] \| --take-fullscreen \| --take-window \| --select-window <next\|prev\|left\|right\|up\|down>` | Pick a screen region over frozen screen content | yes | no | low |
| `omarchy capture screenrecording` | `[--fullscreen] [--with-desktop-audio] [--with-microphone-audio] [--with-webcam] [--webcam-device=<device>] [--webcam-size=<small\|medium\|large>] [--resolution=<size>] [--stop-recording]` | Start or stop screen recording | no | no | medium |
| `omarchy capture screenrecording with webcam` | `` | Pick a webcam and start a screen recording with it | no | no | low |
| `omarchy capture screenshot` | `[smart\|region\|windows\|fullscreen] [slurp\|copy\|save] [--editor=<name>]` | Take a screenshot | no | no | low |
| `omarchy capture text` | `` | Extract text from a screenshot region with OCR | no | no | low |
| `omarchy capture webcam list` | `` | List webcam devices that support video capture | yes | no | read-only |
| `omarchy capture webcam resize` | `<smaller\|larger\|reset\|small\|medium\|large>` | Resize the active webcam recording overlay | no | no | low |

### `channel`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy channel current` | `` | Print the active Omarchy package channel | no | no | medium |
| `omarchy channel set` | `<stable\|rc\|edge\|dev>` | Set the Omarchy package channel. | no | yes | medium |

### `chromium`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy chromium copy url host` | `` | Native messaging host: copy a Chromium tab URL to the clipboard | yes | no | low |
| `omarchy chromium ytdlp host` | `` | Native messaging host: download the URL sent by the yt-dlp Chromium extension | yes | no | low |

### `clipboard`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy clipboard open` | `--history-index <index>` | Open a clipboard history entry | yes | no | low |
| `omarchy clipboard paste file` | `[--copy-only] <mime-type> <path>` | Copy a file to the clipboard and paste it | yes | no | low |
| `omarchy clipboard paste text` | `[--shift-insert] [--copy-only] [--history-index <index>\|<text>]` | Copy text to the clipboard and type or paste it | yes | no | low |

### `cmd`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy cmd missing` | `` | Check whether any required commands are missing | no | no | read-only |
| `omarchy cmd present` | `` | Check whether all required commands are available | no | no | read-only |
| `omarchy cmd terminal cwd` | `` | Print the current working directory of the active terminal window | yes | no | low |

### `crash`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy crash mute` | `[--] [<program>] [on\|off\|toggle]` | Silence crash notifications for one program, or list what is silenced | no | no | read-only |
| `omarchy crash watch` | `` | Watch for process crashes and offer an AI diagnosis | yes | no | low |

### `debug`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy debug` | `[--no-sudo] [--print]` | Print debugging information | no | yes | high |
| `omarchy debug idle` | `[log-lines]` | Show idle, screensaver, and lock diagnostics | no | no | medium |

### `default`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy default agent` | `[pi\|omp\|opencode\|ori\|claude\|codex\|grok\|openclaw\|agy\|hermes\|copilot\|crush\|cursor-agent\|muse]` | Set and launch the default coding agent | no | no | medium |
| `omarchy default browser` | `[chromium\|chrome\|brave\|brave-origin\|edge\|firefox\|zen]` | Set the default browser for Omarchy and XDG handlers | no | no | medium |
| `omarchy default editor` | `[code\|cursor\|zed\|sublime_text\|helix\|vim\|emacs\|nvim]` | Set the default editor used by omarchy-launch-editor | no | no | medium |
| `omarchy default terminal` | `[alacritty\|foot\|ghostty\|kitty]` | Set the default terminal used by xdg-terminal-exec | no | no | medium |

### `dev`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy dev add migration` | `[--no-edit]` | Create a new Omarchy migration in the current source tree. | no | no | medium |
| `omarchy dev benchmark cli` | `[--repeat=<count>]` | Measure Omarchy CLI response times | no | no | low |
| `omarchy dev benchmark theme switcher` | `[--repeat=<count>] [--keep-cache]` | Measure theme switcher cache and selector prep times | no | no | medium |
| `omarchy dev font` | `[list\|add] <name> <svg-file-or-url> [--codepoint U+E9xx] [--font PATH]` | Add branded glyphs to the Omarchy icon font | no | no | medium |
| `omarchy dev install ydoo` | `` | Install and enable ydotool mouse automation for Omarchy development | no | yes | high |
| `omarchy dev link` | `<path-to-checkout> [--no-reboot]` | Point Omarchy at a local checkout after reboot | no | no | high |
| `omarchy dev pkg test` | `[package-name] [path-to-checkout]` | Build and install an Omarchy package from a local checkout | no | no | high |
| `omarchy dev status` | `` | Show the current Omarchy dev-link state | no | no | read-only |
| `omarchy dev theme preview` | `[theme-name\|theme-dir\|colors.toml] [--no-color] [--no-osc\|--osc]` | Preview an Omarchy theme palette in the terminal | no | no | medium |
| `omarchy dev ui preview` | `[section]` | Open the omarchy-shell dev gallery (qs.Ui kit preview) | no | no | low |
| `omarchy dev unlink` | `[--no-reboot]` | Restore Omarchy to the package install after reboot | no | no | high |

### `disk`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy disk speedtest` | `[target-dir]` | Measure live disk read and write speed | no | no | high |

### `display`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy display text size` | `[size\|reset]` | Scale text everywhere — omarchy shell, GTK apps, and terminals | no | no | low |

### `dns`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy dns` | `[Cloudflare\|Google\|DHCP\|Custom]` | Show or configure the system DNS provider | no | no | medium |

### `done`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy done` | `<check\|mark\|ensure> <name>` | Check or mark completed Omarchy setup tasks | yes | no | medium |

### `drive`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy drive info` | `<drive>` | Print drive information such as size, model, and mount details | no | no | high |
| `omarchy drive password` | `` | Set a new encryption password for a drive selected. | no | yes | high |
| `omarchy drive select` | `` | Select a drive from a list with info that includes space and brand. Used by omarchy-drive-password. | no | no | high |

### `file`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy file select` | `[--title <title>] [--multiple] [--directory] [--extensions "<ext ext...>"]` | Pick files with the desktop file chooser | no | no | low |

### `font`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy font current` | `` | Show current monospace font | no | no | read-only |
| `omarchy font list` | `` | List available monospace fonts | no | no | read-only |
| `omarchy font set` | `<font-name>` | Set the system monospace font | no | no | medium |

### `games`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy games retro cores` | `` | List installed RetroArch core names | no | no | high |
| `omarchy games retro install` | `[core path-to-game]` | Create a desktop launcher for a RetroArch game | no | no | high |

### `git`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy git url check` | `<git-url>` | Check that a git URL names a repository, not a transport helper | yes | no | low |

### `hibernation`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy hibernation available` | `` | Check if hibernation is supported | no | no | high |
| `omarchy hibernation remove` | `` | Remove hibernation setup including swap and boot resume settings | no | yes | high |
| `omarchy hibernation setup` | `[--force] [--no-rebuild]` | Set up hibernation with swap and boot resume configuration | no | yes | high |

### `hook`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy hook` | `[name] [args...]` | Run a named hook from ~/.config/omarchy/hooks/<name> and ~/.config/omarchy/hooks/<name>.d/. | no | no | medium |
| `omarchy hook install` | `<type> <file>` | Install a hook into ~/.config/omarchy/hooks/<type>.d/ | no | no | high |

### `hw`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy hw asus expertbook b9406` | `` | Detect ASUS ExpertBook B9406 series laptops on Intel Panther Lake. | no | no | low |
| `omarchy hw asus rog` | `` | Detect whether the computer is an Asus ROG machine. | no | no | low |
| `omarchy hw asus zenbook ux5406aa` | `` | Detect ASUS Zenbook UX5406AA series laptops on Intel Panther Lake. | no | no | low |
| `omarchy hw clamshell` | `` | Returns true when clamshell mode is active | yes | no | low |
| `omarchy hw dell xps haptic touchpad` | `` | Match Dell XPS systems with the Synaptics haptic touchpad. | no | no | low |
| `omarchy hw dell xps oled` | `` | Match Dell XPS systems with LG OLED panel on Intel Panther Lake (Xe3) GPU. | no | no | low |
| `omarchy hw dell xps13 sidecar amps` | `` | Match the Dell XPS 13 DX13260 that requires the sidecar amplifier workaround. | no | no | low |
| `omarchy hw display` | `` | Print the most likely display backlight device. | no | no | low |
| `omarchy hw elgato camlink 4k` | `` | Detect whether an Elgato Cam Link 4K is plugged in. | no | no | low |
| `omarchy hw external monitors` | `` | Returns true when an external monitor is physically connected. | no | no | medium |
| `omarchy hw fingerprint` | `` | Returns true when a fingerprint reader is present | yes | no | low |
| `omarchy hw framework16` | `` | Detect whether the computer is a Framework Laptop 16. | no | no | low |
| `omarchy hw hybrid gpu` | `` | Detect whether the system has an active hybrid GPU configuration | no | no | low |
| `omarchy hw intel` | `` | Detect whether the computer has an Intel CPU. | no | no | low |
| `omarchy hw intel ptl` | `` | Detect whether the computer has an Intel Panther Lake GPU. | no | no | low |
| `omarchy hw intel sof` | `` | Detect an Intel SOF-capable audio DSP | no | no | low |
| `omarchy hw laptop` | `` | Returns true when running on a laptop (has a lid or laptop chassis). | no | no | low |
| `omarchy hw laptop closed` | `` | Returns true when the laptop lid is closed | yes | no | medium |
| `omarchy hw match` | `<pattern>` | Match against the computer's DMI product name or product family (case-insensitive). | no | no | low |
| `omarchy hw nvidia` | `` | Detect whether the computer has an NVIDIA GPU. | no | no | low |
| `omarchy hw nvidia gsp` | `` | Detect whether the computer has an NVIDIA GPU with GSP firmware (Turing or newer). | no | no | low |
| `omarchy hw nvidia without gsp` | `` | Detect whether the computer has an NVIDIA GPU without GSP firmware (Maxwell/Pascal/Volta). | no | no | low |
| `omarchy hw recover internal monitor` | `` | Clear the internal-monitor-disable toggle if no external display is connected. | no | no | medium |
| `omarchy hw surface` | `` | Detect whether the computer is a Microsoft Surface device. | no | no | low |
| `omarchy hw touchpad` | `` | Print the detected Hyprland touchpad or trackpad device name | no | no | low |
| `omarchy hw touchscreen` | `` | Print the detected Hyprland touchscreen or tablet device name | no | no | low |
| `omarchy hw vulkan` | `` | Detect whether Vulkan is available. | no | no | low |
| `omarchy hw webcam` | `` | Check whether a webcam is available | no | no | low |

### `hyprland`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy hyprland focus app` | `<app-name>` | Focus a Hyprland window by application identity | no | no | low |
| `omarchy hyprland monitor clamshell` | `` | Apply clamshell display state to Hyprland monitors | yes | no | low |
| `omarchy hyprland monitor external active` | `` | Returns true when Hyprland has an active external monitor | yes | no | low |
| `omarchy hyprland monitor focused` | `` | Print the name of the currently focused Hyprland monitor. | no | no | low |
| `omarchy hyprland monitor focused apple` | `[monitor]` | Return success if the focused or named Hyprland monitor is an Apple display. | no | no | low |
| `omarchy hyprland monitor internal` | `<on\|off\|toggle\|recover>` | Enable, disable, toggle, or recover the internal laptop display | no | no | medium |
| `omarchy hyprland monitor internal mirror` | `<on\|off\|toggle\|recover>` | Enable, disable, toggle, or recover mirroring the internal display to an external monitor | no | no | medium |
| `omarchy hyprland monitor laptop` | `` | Print the name of the built-in laptop display, including disabled outputs. | no | no | medium |
| `omarchy hyprland monitor modeless` | `` | Returns true when Hyprland has an enabled monitor with no mode | yes | no | medium |
| `omarchy hyprland monitor scaling` | `[up\|down\|SCALE]` | Show, set, or adjust focused Hyprland monitor scaling | no | no | medium |
| `omarchy hyprland monitor watch` | `` | Watch Hyprland monitor events and recover monitor toggles when a monitor is removed | no | no | high |
| `omarchy hyprland reload guard` | `` | Pause or resume Hyprland config auto-reload around package transactions. | yes | no | low |
| `omarchy hyprland session locked` | `` | Returns true when the compositor holds a session lock | yes | no | medium |
| `omarchy hyprland toggle` | `<flag-name> [on\|off\|toggle]` | Toggle permanent Hyprland flags by copying them into a directory that's sourced entirely. | no | no | medium |
| `omarchy hyprland toggle disabled` | `<flag-name>` | Check if a Hyprland toggle is currently disabled (missing). | no | no | medium |
| `omarchy hyprland toggle enabled` | `<flag-name>` | Check if a Hyprland toggle is currently enabled. | no | no | medium |
| `omarchy hyprland window close all` | `` | Close all open windows | no | no | medium |
| `omarchy hyprland window gaps toggle` | `` | Toggles the window gaps globally between no gaps and the default. | no | no | medium |
| `omarchy hyprland window pop` | `[width height x y]` | Toggle to pop-out a tile to stay fixed on a display basis. | no | no | medium |
| `omarchy hyprland window single square aspect toggle` | `` | Toggle single-window square aspect ratio. | no | no | medium |
| `omarchy hyprland window tiled fullscreen toggle` | `` | Toggle tiled fullscreen for the focused Hyprland window | no | no | medium |
| `omarchy hyprland window transparency toggle` | `` | Toggles transparency for the currently focused window. | no | no | medium |
| `omarchy hyprland window width` | `<save\|restore>` | Save or restore the focused Hyprland window width | no | no | low |
| `omarchy hyprland workspace layout toggle` | `` | Toggle the layout on the current active workspace between dwindle and scrolling | no | no | medium |

### `install`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy install ai chatgpt` | `` | Install the ChatGPT desktop app | no | yes | high |
| `omarchy install ai claude` | `` | Install the Claude desktop app | no | yes | high |
| `omarchy install ai hermes` | `` | Install the Hermes desktop app | no | yes | high |
| `omarchy install ai openclaw` | `` | Install the OpenClaw agent platform and its Control UI web app | no | yes | high |
| `omarchy install ai t3 code` | `` | Install T3 Code and point it at the Omarchy palette | no | yes | high |
| `omarchy install and launch` | `<display-name> <packages> <desktop-id>` | Install a packaged app and launch it once it finishes | no | no | high |
| `omarchy install app` | `<display-name> <packages>` | Install a packaged app, surfacing the install in a floating terminal | no | no | high |
| `omarchy install browser` | `<chromium\|chrome\|brave\|brave-origin\|edge\|firefox\|zen>` | Install a supported browser | no | no | high |
| `omarchy install chromium claude` | `` | Install the Claude extension for Chromium-based browsers | no | yes | high |
| `omarchy install chromium copy url` | `` | Install the native messaging host for the Copy URL Chromium extension | no | no | high |
| `omarchy install chromium google account` | `` | Allow Chromium to sign in to Google accounts by adding the required OAuth credentials | no | no | high |
| `omarchy install chromium ytdlp` | `` | Install the native messaging host for the yt-dlp Chromium extension | no | no | high |
| `omarchy install dev-env` | `<ruby\|node\|bun\|deno\|go\|laravel\|symfony\|php\|python\|elixir\|phoenix\|rust\|java\|zig\|ocaml\|dotnet\|clojure\|scala>` | Install a supported development environment | no | yes | high |
| `omarchy install docker dbs` | `` | Install one of the supported databases in a Docker container with the suitable development options. | no | yes | high |
| `omarchy install editor emacs` | `` | Install Emacs with Omarchy theme and font integration via the omarchy-emacs AUR package | no | no | high |
| `omarchy install editor helix` | `` | Install Helix and configure it to use the current Omarchy theme | no | no | high |
| `omarchy install editor vscode` | `` | Install VS Code and configure Omarchy defaults for secrets, updates, and theme | no | no | high |
| `omarchy install editor zed` | `` | Install Zed Editor and configure it with the current Omarchy theme | no | no | high |
| `omarchy install font` | `<display-name> <package> <family>` | Install a Nerd Font package and switch the system to it | no | no | high |
| `omarchy install gaming battlenet` | `` | Install Battle.net standalone via umu-launcher + GE-Proton (no Steam, no Lutris, no Heroic). | no | yes | high |
| `omarchy install gaming geforce-now` | `` | Install and launch Geforce Now. | no | no | high |
| `omarchy install gaming gpu-lib32` | `` | Install lib32 graphics drivers (Vulkan + NVIDIA) for any detected GPUs. | no | yes | high |
| `omarchy install gaming heroic` | `` | Install Heroic Games Launcher (Epic, GOG, Amazon Prime Gaming) with graphics drivers. | no | yes | high |
| `omarchy install gaming lutris` | `` | Install Lutris with Wine + DXVK for running Windows games (Battle.net, EA, Ubisoft Connect, etc.) | no | yes | high |
| `omarchy install gaming retroarch` | `` | Install RetroArch with the full libretro core set plus FBNeo and a ~/Games ROM directory. | no | no | high |
| `omarchy install gaming steam` | `` | Install Steam and graphics drivers selected for this system | no | yes | high |
| `omarchy install gaming xbox-cloud` | `` | Install Xbox Cloud Gaming as a web app and launch it. | no | no | high |
| `omarchy install gaming xbox-controllers` | `` | Install support for using Xbox controllers with Steam/RetroArch/etc. | no | yes | high |
| `omarchy install hermes cli` | `[--check\|--now\|--owns\|--remove]` | Install the Hermes CLI as a mise-backed wrapper in ~/.local/bin | no | no | high |
| `omarchy install openclaw cli` | `[--check\|--now]` | Ensure the OpenClaw CLI is installed for the default agent | no | yes | high |
| `omarchy install preinstalls` | `` | Restore the preinstalled Omarchy applications (web apps, TUIs, and selected packages). | no | yes | high |
| `omarchy install service 1password` | `` | Install 1Password and its Chromium extension. | no | yes | high |
| `omarchy install service dropbox` | `` | Install and start the Dropbox service. Must then be authenticated via the web. | no | no | high |
| `omarchy install service nordvpn` | `` | Install the NordVPN service with optional GUI. | no | yes | high |
| `omarchy install service once` | `` | Install the ONCE service, enable its background service, and launch the TUI. | no | yes | high |
| `omarchy install service signal` | `` | Install Signal and launch it. | no | yes | high |
| `omarchy install service spotify` | `` | Install Spotify. | no | yes | high |
| `omarchy install service sunshine` | `` | Install Sunshine and open Moonlight streaming ports for LAN and Tailscale. | no | yes | high |
| `omarchy install service tailscale` | `` | Install the Tailscale mesh VPN service and a web app for the Tailscale Admin Console. | no | yes | high |
| `omarchy install terminal` | `<alacritty\|foot\|ghostty\|kitty>` | Install one of the approved terminals and set it as the default for Omarchy (Super + Return etc). | no | yes | high |

### `installed`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy installed service dropbox` | `` | Check whether Dropbox is installed and running | yes | no | high |
| `omarchy installed service tailscale` | `` | Check whether Tailscale is installed and running | yes | no | high |

### `launch`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy launch 1password` | `` | Launch 1Password or start its installer when missing. | no | no | high |
| `omarchy launch about` | `` | Launch the fastfetch TUI that gives information about the current system. | no | no | high |
| `omarchy launch battlenet` | `[--with-mangohud]` | Launch the installed Battle.net client via umu-launcher + GE-Proton. | no | no | high |
| `omarchy launch browser` | `[url]` | Launch the default browser as determined by xdg-settings. | no | no | medium |
| `omarchy launch config editor` | `<path>` | Open a config file in the user's editor and surface a toast | no | no | medium |
| `omarchy launch discord community` | `` | Open the Omarchy Discord community in the Discord app or a browser. | no | no | medium |
| `omarchy launch docker tui` | `` | Open the Docker TUI (lazydocker) with access to the Docker daemon | yes | no | medium |
| `omarchy launch editor` | `[--inline] <path>` | Launch the default editor selected via Omarchy defaults. | no | no | medium |
| `omarchy launch floating terminal with presentation` | `<command>` | Launch a floating terminal with the Omarchy presentation wrapper | no | no | medium |
| `omarchy launch nautilus` | `` | Launch Files | no | no | medium |
| `omarchy launch nautilus cwd` | `` | Launch Files in the active terminal's current directory | no | no | medium |
| `omarchy launch openclaw` | `[--tui [--message <text>]]` | Open the OpenClaw Control UI (or its terminal UI with --tui), onboarding or starting the gateway first when needed. | no | no | medium |
| `omarchy launch or focus` | `<window-pattern> <launch-command>` | Launch an app or focus an existing window matching a pattern | no | no | medium |
| `omarchy launch or focus tui` | `[--app-id=<app-id>] <command> [args...]` | Launch a TUI or focus an existing terminal window for it | no | no | medium |
| `omarchy launch or focus webapp` | `<window-pattern> <url-and-flags...>` | Launch or focus on a given web app identified by the window-pattern. | no | no | medium |
| `omarchy launch screensaver` | `` | Launch the Omarchy screensaver in the default terminal on the system with the correct font configuration. | no | no | medium |
| `omarchy launch shell` | `` | Launch the Omarchy shell with its log kept in the journal | yes | no | medium |
| `omarchy launch signal` | `` | Launch Signal or start its installer when missing. | no | no | high |
| `omarchy launch spotify` | `` | Launch Spotify or start its installer when missing. | no | no | high |
| `omarchy launch terminal` | `[command...]` | Launch a terminal in the active terminal's current directory | no | no | medium |
| `omarchy launch terminal herdr` | `` | Launch or attach to the persistent herdr session in a terminal | no | no | medium |
| `omarchy launch terminal tmux` | `` | Launch or attach to the Work tmux session in a terminal | no | no | medium |
| `omarchy launch tui` | `[--app-id=<app-id>] <command> [args...]` | Launch a TUI command in the default terminal with Omarchy styling | no | no | medium |
| `omarchy launch webapp` | `<url>` | Launch a URL as a web app in the default supported browser | no | no | medium |

### `menu`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy menu` | `[toggle\|summon\|close\|refresh\|ping] [route]` | Control the Omarchy menu (toggle / summon / close / refresh) | no | no | medium |
| `omarchy menu clipboard` | `` | Launch the clipboard manager | no | no | medium |
| `omarchy menu emoji` | `` | Launch emojis | no | no | medium |
| `omarchy menu emoji insert` | `<emoji>` | Insert an emoji into the focused application | yes | no | low |
| `omarchy menu file` | `label paths formats [menu args...]` | Pick a file from a menu | no | no | low |
| `omarchy menu herdr keybindings` | `[--print\|-p] [--config <path>]` | Display annotated Herdr keybindings using an interactive search menu. | no | no | low |
| `omarchy menu images` | `[--selected <image>] [--print-name] [--show-labels] [--filterable] [--lazy-thumbnails] [--preload] [--cache-only] <image-dir>...` | Open a generic image selector menu | no | no | low |
| `omarchy menu input` | `prompt [menu args...]` | Prompt for text input from a menu | no | no | low |
| `omarchy menu keybindings` | `` | Display Hyprland keybindings defined in your configuration using an interactive search menu. | no | no | low |
| `omarchy menu plugin` | `<enable\|disable\|clone\|remove>` | Pick a shell plugin to enable, disable, clone, or remove | no | no | high |
| `omarchy menu select` | `prompt [option...] [-- menu args...]` | Pick one option from a menu | no | no | low |
| `omarchy menu timezone` | `` | Select and set the system timezone | no | no | medium |
| `omarchy menu tmux keybindings` | `[--print\|-p] [--config <path>]` | Display annotated Tmux keybindings using an interactive search menu. | no | no | low |

### `migrate`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy migrate` | `[--pending]` | Run pending Omarchy migrations. | no | no | high |
| `omarchy migrate notify` | `` | Notify the user when Omarchy has pending migrations | no | no | high |

### `mise`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy mise install` | `<package> [command-name [bin-name]]` | Install a small mise-backed wrapper for a given tool. | no | no | high |

### `monitor`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy monitor state` | `` | Print monitor panel state for the shell | no | no | low |

### `network`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy network band` | `[auto\|2.4\|5\|6]` | Show or pin the Wi-Fi band for the active connection | no | no | medium |
| `omarchy network password` | `<interface>` | Print the active Wi-Fi connection's password | no | no | medium |
| `omarchy network qr` | `[--meta] [interface]` | Generate a Wi-Fi QR matrix for the shell | no | no | low |
| `omarchy network speedtest` | `[down\|up]` | Measure live internet speed for one direction | no | no | low |
| `omarchy network status` | `[--verbose]` | Print active network status for the shell | no | no | read-only |

### `notification`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy notification battery` | `` | Show the current battery status notification | no | no | read-only |
| `omarchy notification dismiss` | `<summary>` | Dismiss a notification by summary substring. Used by the first-run notifications to dismiss them after clicking for action. | no | no | low |
| `omarchy notification send` | `[--app-name <app-name>] [-g <glyph>] [-u <low\|normal\|critical>] [-i <icon>] [-t <ms>] [-r <id>] [-p] [--image <path-or-uri>] <headline> [description] [--exec <program> [args...]]` | Send an Omarchy desktop notification | no | no | low |
| `omarchy notification time` | `` | Show the current time and date notification | no | no | read-only |
| `omarchy notification wait` | `[timeout-seconds]` | Wait for the desktop notification server to accept notifications | yes | no | low |
| `omarchy notification weather` | `` | Toggle the current weather panel | no | no | medium |

### `openclaw`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy openclaw onboard` | `` | Run OpenClaw's setup wizard the way Omarchy needs it: in the terminal, installing the gateway as a user service, and returning when it is done. | no | no | high |

### `osd`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy osd` | `[-i\|--icon <icon>] [-m\|--message <text>] [-p\|--progress <0-100>] [-d\|--duration <ms>]` | Show the Omarchy Quickshell on-screen display | no | no | read-only |

### `pkg`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy pkg add` | `<packages...>` | Install Arch packages if they are missing | no | yes | high |
| `omarchy pkg aur accessible` | `` | Returns true if the AUR is up and available. | no | no | high |
| `omarchy pkg aur add` | `<packages...>` | Add the named packages to the system from the AUR if they're missing. Returns false if it couldn't be done. | no | no | high |
| `omarchy pkg aur install` | `` | Show a fuzzy-finder TUI for picking new AUR packages to install. | no | yes | high |
| `omarchy pkg drop` | `<packages...>` | Remove all the named packages from the system if they're installed (otherwise ignore). | no | yes | high |
| `omarchy pkg install` | `` | Show a fuzzy-finder TUI for picking new Arch and OPR packages to install. | no | yes | high |
| `omarchy pkg missing` | `<packages...>` | Returns true if any of the named packages are missing from the system (or false if they're all there). | no | no | high |
| `omarchy pkg present` | `<packages...>` | Returns true if all of the named packages are installed on the system (or false if any of them are missing). | no | no | high |
| `omarchy pkg remove` | `` | Show a fuzzy-finder TUI for picking packages installed on the system to be removed. | no | yes | high |

### `plugin`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy plugin add` | `[git-url] [--enable] [--yes]` | Add a shell plugin from git | no | no | medium |
| `omarchy plugin catalog` | `` | Emit every first-party and user plugin manifest as JSON | yes | no | medium |
| `omarchy plugin clone` | `<source-id> [--edit]` | Clone a built-in Omarchy shell plugin into your own config | no | no | medium |
| `omarchy plugin disable` | `<id>` | Disable a shell plugin | no | no | medium |
| `omarchy plugin enable` | `<id> [placement]` | Enable a shell plugin | no | no | medium |
| `omarchy plugin list` | `[--json]` | List discovered shell plugins | no | no | medium |
| `omarchy plugin remove` | `[id] [--yes]` | Remove an installed shell plugin | no | no | high |
| `omarchy plugin update` | `[id] [--yes]` | Update installed git-managed plugins | no | no | high |
| `omarchy plugin validate` | `<plugin-folder>` | Validate a plugin folder against the Omarchy plugin manifest schema | no | no | medium |

### `plymouth`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy plymouth current` | `` | Show which theme is styling the Plymouth boot screen | no | no | medium |
| `omarchy plymouth list` | `` | List themes that can style the Plymouth boot screen | no | no | medium |
| `omarchy plymouth preview` | `<background-hex> <text-hex> <path-to-logo.png> <output-path>` | Preview a Plymouth boot screen with custom colors and logo | no | no | low |
| `omarchy plymouth reset` | `` | Restore the default Omarchy Plymouth boot theme and SDDM login screen | no | yes | high |
| `omarchy plymouth set` | `<background-hex> <text-hex> <path-to-logo.png>` | Set the Plymouth boot theme colors and logo | no | yes | medium |
| `omarchy plymouth set by theme` | `<theme-name>` | Set the Plymouth boot theme from an Omarchy theme | no | yes | medium |
| `omarchy plymouth switcher` | `` | Open the Plymouth unlock screen switcher | no | no | medium |

### `power`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy power present` | `` | Returns true if external power is connected. | no | no | medium |

### `powerprofiles`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy powerprofiles init` | `` | Set the correct power profile on boot based on current AC/battery state. | no | no | medium |
| `omarchy powerprofiles list` | `[--active-state]` | Returns a list of all the available power profiles on the system. | no | no | read-only |
| `omarchy powerprofiles set` | `[autodetect\|ac\|battery] [power-saver\|balanced\|performance]` | Set and remember the power profile for AC or battery use | no | no | medium |

### `provision`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy provision first run` | `[--force]` | Finish first-login setup for Omarchy. | yes | no | medium |
| `omarchy provision owner` | `` | First-boot provisioning: create the user on a machine installed in deferred provisioning | yes | yes | high |
| `omarchy provision user` | `` | Finalize Omarchy user setup (runtime tweaks /etc/skel can't do) | yes | no | medium |

### `refresh`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy refresh applications` | `` | Ensure default application launchers and mise wrappers are installed. | no | no | high |
| `omarchy refresh chromium` | `` | Refresh the ~/.config/chromium-flags.conf file from the Omarchy defaults. | no | no | medium |
| `omarchy refresh config` | `<config-path>` | Copy a shipped user config from $OMARCHY_PATH/config into ~/.config (backs up your version). | no | no | medium |
| `omarchy refresh herdr` | `` | Overwrite the user herdr config with the Omarchy default and reload herdr. | no | no | medium |
| `omarchy refresh hyprland` | `` | Overwrite all the user Hyprland Lua configs in ~/.config/hypr with the Omarchy defaults. | no | no | medium |
| `omarchy refresh hyprsunset` | `` | Overwrite the user config for hyprsunset with the Omarchy default and restart the service. | no | no | medium |
| `omarchy refresh limine` | `` | Overwrite the user config for the Limine bootloader and rebuild it. | no | yes | medium |
| `omarchy refresh pacman` | `` | Overwrite the package configuration for /etc/pacman with the Omarchy default of using its dedicated mirrors and repositories, then update all packages. | no | yes | high |
| `omarchy refresh plymouth` | `` | Overwrite the user config for the Plymouth drive decryption and boot sequence with the Omarchy default and rebuild it. | no | yes | high |
| `omarchy refresh sddm` | `` | Refresh the SDDM theme from default | no | yes | medium |
| `omarchy refresh shell` | `` | Reset shell.json to Omarchy defaults | no | no | high |
| `omarchy refresh tmux` | `` | Overwrite the user tmux config with the Omarchy default and reload tmux. | no | no | medium |

### `reinstall`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy reinstall` | `` | Reinstall Omarchy packages and reset default configs | no | yes | high |
| `omarchy reinstall configs` | `` | Reset Omarchy user configs and shipped defaults in $HOME (destructive) | no | no | high |
| `omarchy reinstall pkgs` | `` | Reinstall all default Omarchy packages from the stable channel | no | yes | high |

### `reminder`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy reminder` | `[-i\|--interactive] \| <minutes> [message] \| show [-j\|--json] \| clear` | Set and show lightweight desktop notification reminders | no | no | medium |

### `remove`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy remove ai chatgpt` | `` | Remove the ChatGPT desktop app along with its configuration and caches. | no | yes | high |
| `omarchy remove ai claude` | `` | Remove the Claude desktop app along with its configuration and caches. | no | yes | high |
| `omarchy remove ai grok bot` | `` | Remove Grok Bot along with its settings and data. | no | yes | high |
| `omarchy remove ai hermes` | `` | Remove the Hermes desktop app along with the Hermes runtime it installed. | no | yes | high |
| `omarchy remove ai lm studio` | `` | Remove LM Studio along with its configuration and every model it downloaded. | no | yes | high |
| `omarchy remove ai ollama` | `` | Remove Ollama along with every model it pulled. | no | yes | high |
| `omarchy remove ai openclaw` | `` | Remove the OpenClaw agent platform along with its gateway service and web app. | no | yes | high |
| `omarchy remove ai perplexity` | `` | Remove the Perplexity desktop app along with its runtime caches. | no | yes | high |
| `omarchy remove ai t3 code` | `` | Remove T3 Code along with its configuration and workspaces. | no | yes | high |
| `omarchy remove browser` | `<chrome\|brave\|brave-origin\|edge\|firefox\|zen>` | Remove a supported browser and clean up Omarchy browser defaults | no | yes | high |
| `omarchy remove dev env` | `<ruby\|node\|bun\|deno\|go\|php\|laravel\|symfony\|python\|elixir\|phoenix\|zig\|rust\|java\|dotnet\|ocaml\|clojure\|scala>` | Remove a development environment that was previously installed via omarchy-install-dev-env. | no | yes | high |
| `omarchy remove gaming battlenet` | `` | Remove Battle.net, its Proton prefix, installed games, and desktop entry. | no | no | high |
| `omarchy remove gaming geforce-now` | `` | Remove the GeForce NOW Flatpak app and its data. | no | no | high |
| `omarchy remove gaming heroic` | `` | Remove Heroic Games Launcher and its game libraries, configs, and caches. | no | yes | high |
| `omarchy remove gaming lutris` | `` | Remove Lutris, Wine, umu-launcher, and all their configs and caches. | no | yes | high |
| `omarchy remove gaming minecraft` | `` | Remove the Minecraft launcher along with its worlds, mods, and caches. | no | yes | high |
| `omarchy remove gaming retroarch` | `` | Remove RetroArch, all libretro cores, and its config/saves. Leaves ~/Games/roms and ~/Games/bios alone. | no | yes | high |
| `omarchy remove gaming steam` | `` | Remove Steam and all of its game libraries, configs, and caches. | no | yes | high |
| `omarchy remove gaming xbox cloud` | `` | Remove the Xbox Cloud Gaming web app. | no | no | high |
| `omarchy remove gaming xbox-controllers` | `` | Remove the xpadneo Xbox controller driver and undo its module/blacklist config. | no | yes | high |
| `omarchy remove launcher entry` | `<desktop-id> <name>` | Remove or uninstall the selected launcher entry | yes | yes | high |
| `omarchy remove preinstalls` | `` | Remove preinstalled Omarchy applications (web apps, TUIs, and selected packages). | no | no | high |
| `omarchy remove security fido2` | `` | Remove FIDO2 authentication from sudo and polkit | no | yes | high |
| `omarchy remove security fingerprint` | `` | Remove fingerprint authentication from sudo, polkit, and lock screen | no | yes | high |
| `omarchy remove security sshd` | `` | Disable the OpenSSH server, close the firewall port, and optionally remove authorized keys | no | yes | high |
| `omarchy remove security sudoless docker` | `` | Disable sudoless Docker by removing your user from the docker group | no | yes | high |
| `omarchy remove service 1password` | `` | Remove 1Password and its Chromium extension. | no | yes | high |
| `omarchy remove service sunshine` | `` | Remove Sunshine and close Omarchy-managed Moonlight streaming ports. | no | yes | high |

### `restart`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy restart app` | `<application-name> [application-args...]` | Restart an application by killing it and relaunching via uwsm. | no | no | medium |
| `omarchy restart audio` | `` | Restart audio services and recover stuck USB audio devices. | no | no | medium |
| `omarchy restart bluetooth` | `` | Unblock and restart the bluetooth service. | no | no | medium |
| `omarchy restart btop` | `` | Reload btop configuration (used by the Omarchy theme switching). | no | no | medium |
| `omarchy restart gum` | `` | Export the current theme's gum styling into the environment | yes | no | medium |
| `omarchy restart helix` | `` | Reload Helix configuration | no | no | medium |
| `omarchy restart herdr` | `` | Reload herdr if running with the latest configuration | no | no | medium |
| `omarchy restart hyprctl` | `` | Reload hyprland configuration (used by the Omarchy theme switching). | no | no | medium |
| `omarchy restart hyprsunset` | `` | Restart the hyprsunset service (used for blue light filtering/night light). | no | no | medium |
| `omarchy restart opencode` | `` | Reload opencode configuration (used by the Omarchy theme switching). | no | no | medium |
| `omarchy restart shell` | `` | Restart the Omarchy shell | no | no | medium |
| `omarchy restart terminal` | `` | Reload supported terminal emulators after config changes | no | no | medium |
| `omarchy restart tmux` | `` | Restart tmux if running with the latest configuration | no | no | medium |
| `omarchy restart trackpad` | `` | Reset the trackpad by unbinding and rebinding its driver. | no | yes | high |
| `omarchy restart wifi` | `` | Unblock and restart the Wi-Fi service. | no | no | medium |
| `omarchy restart xcompose` | `` | Restart the XCompose input method service (fcitx5) to apply new compose key settings. | no | no | medium |

### `screensaver`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy screensaver` | `` | Run the Omarchy screensaver using random effects from TTE. | no | no | low |

### `setup`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy setup direct boot` | `` | Add or remove an EFI boot entry for the Omarchy UKI, allowing the system to boot directly | no | yes | high |
| `omarchy setup factory reset` | `` | Factory-reset this machine back to its freshly-installed state | no | yes | high |
| `omarchy setup security fido2` | `` | Set up FIDO2 authentication for sudo and polkit | no | yes | high |
| `omarchy setup security fingerprint` | `` | Set up fingerprint authentication for sudo, polkit, and lock screen | no | yes | high |
| `omarchy setup security sshd` | `[--key=<public-key>] [--gh-keys <github-username>]` | Set up the OpenSSH server, open the firewall, and authorize an SSH key | no | yes | high |
| `omarchy setup security sudoless docker` | `` | Enable sudoless Docker by adding your user to the docker group (root-equivalent!) | no | yes | high |

### `share`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy share` | `<clipboard\|file\|folder> [path...]` | Share clipboard, files, or folders with LocalSend | no | no | low |

### `shell`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy shell` | `[-q] <target> <method> [args...]` | Send an IPC call to the running Omarchy shell | no | no | low |
| `omarchy shell config` | `` | Shared helpers for editing ~/.config/omarchy/shell.json (source this, don't run it). | yes | no | low |

### `show`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy show done` | `[exit-code]` | Display a "Done!" or "Failed!" message and wait for user to press any key. | no | no | read-only |
| `omarchy show logo` | `` | Display the Omarchy logo in the terminal using green color. | no | no | read-only |

### `snapshot`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy snapshot` | `<create\|restore>` | Create or restore system snapshots with snapper | no | yes | low |

### `state`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy state` | `<set\|clear> <state-name-or-pattern>` | Manage persistent state files for Omarchy toggles and settings. | yes | no | medium |

### `sudo`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy sudo docker` | `[--configured]` | Succeed when Docker needs sudo, fail when it can be used directly | yes | no | high |
| `omarchy sudo keepalive` | `` | Prompt for sudo once and keep the credential alive in the background. | no | yes | high |
| `omarchy sudo passwordless` | `[MINUTES]` | Toggle passwordless sudo for the current user. | no | yes | high |

### `system`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy system factory reset finish` | `` | First-boot worker that finishes an omarchy-system-factory-reset reset | yes | yes | high |
| `omarchy system lid close` | `` | Lock and reconcile displays when the laptop lid closes | yes | no | medium |
| `omarchy system lock` | `` | Lock the computer and turn off the display | no | no | medium |
| `omarchy system logout` | `` | Log out after closing application windows | no | no | medium |
| `omarchy system reboot` | `` | Reboot after closing application windows | no | no | high |
| `omarchy system shutdown` | `` | Shut down after closing application windows | no | no | high |
| `omarchy system sleep lock` | `` | Lock before suspend and wait for the session lock to become secure | yes | no | medium |
| `omarchy system sleep monitor` | `` | Monitor sleep preparation and lock before suspend | yes | no | medium |
| `omarchy system stats` | `[--bar-widget]` | Print CPU and memory stats for the shell | no | no | low |
| `omarchy system wake` | `` | Wake displays and restore brightness after idle | no | no | low |

### `tailscale`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy tailscale receive` | `[--once] [directory]` | Save incoming Taildrop files and announce them | no | no | low |
| `omarchy tailscale send` | `<machine> [file...]` | Send files to a machine on your tailnet with Taildrop | no | no | low |

### `theme`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy theme bg cache` | `` | Cache background switcher thumbnails for the current theme | no | no | medium |
| `omarchy theme bg current` | `` | Show current background | no | no | medium |
| `omarchy theme bg install` | `` | Open the current theme's user background folder | no | no | high |
| `omarchy theme bg next` | `` | Cycle to the next background for the current theme | no | no | medium |
| `omarchy theme bg set` | `<path-to-media>` | Set the current background image or video | no | no | medium |
| `omarchy theme bg-switcher` | `` | Open the Omarchy background switcher | no | no | medium |
| `omarchy theme color` | `[--file <colors.toml>] (--all \| --raw \| <key> [fallback])` | Resolve semantic colors from an Omarchy theme colors.toml | yes | no | medium |
| `omarchy theme colors from alacritty` | `<theme-dir>` | Generate a theme's colors.toml from its alacritty.toml palette | yes | no | medium |
| `omarchy theme current` | `` | Show current theme | no | no | medium |
| `omarchy theme dir` | `<theme-name>` | Print the directory holding a theme, preferring a user-installed copy | no | no | high |
| `omarchy theme extras` | `` | List the user-installed themes that came from a git clone | no | no | high |
| `omarchy theme install` | `[git-repo-url]` | Install a theme from a git repository | no | no | high |
| `omarchy theme list` | `` | List available themes | no | no | medium |
| `omarchy theme osc` | `` | Print OSC sequences for an Omarchy color theme | yes | no | medium |
| `omarchy theme refresh` | `` | Refresh the current theme from its templates. | no | no | medium |
| `omarchy theme remove` | `[theme-name]` | Remove a user-installed theme | no | no | high |
| `omarchy theme set` | `<theme-name>` | Apply an Omarchy theme | no | no | medium |
| `omarchy theme set browser` | `` | Apply the current theme color to Chromium, Chrome, Edge, and Brave | yes | no | medium |
| `omarchy theme set browser policy` | `<rrggbb>` | Write the current theme color into the browser policy directories | yes | no | medium |
| `omarchy theme set claude` | `[--activate]` | Sync the generated Omarchy theme to Claude Code | yes | no | medium |
| `omarchy theme set foot` | `` | Apply current Omarchy theme colors to running Foot terminals | yes | no | medium |
| `omarchy theme set gnome` | `` | Apply the current theme to GNOME color mode and icon settings | yes | no | medium |
| `omarchy theme set hermes` | `[--activate] [--wait]` | Sync the generated Omarchy theme to Hermes as a skin | yes | no | medium |
| `omarchy theme set keyboard` | `` | Apply the current theme keyboard color to supported keyboards | yes | no | medium |
| `omarchy theme set keyboard asus rog` | `` | Apply the current theme keyboard color to ASUS ROG keyboards | yes | no | medium |
| `omarchy theme set keyboard f16` | `` | Apply the current theme keyboard color to Framework Laptop 16 keyboards | yes | no | medium |
| `omarchy theme set obsidian` | `` | Sync Omarchy theme to all Obsidian vaults | yes | no | medium |
| `omarchy theme set pi` | `[--activate]` | Sync the generated Omarchy Pi theme | yes | no | medium |
| `omarchy theme set t3code` | `` | Sync the generated Omarchy theme to T3 Code | yes | no | medium |
| `omarchy theme set templates` | `` | Generate themed config files from Omarchy templates | yes | no | medium |
| `omarchy theme set tmux` | `` | Sync current Omarchy theme environment into tmux | yes | no | medium |
| `omarchy theme set vscode` | `` | Sync Omarchy theme to VS Code, VSCodium, and Cursor | yes | no | medium |
| `omarchy theme switcher` | `` | Open the Omarchy theme switcher | no | no | medium |
| `omarchy theme update` | `` | Update user-installed git themes | no | no | high |

### `toggle`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy toggle` | `<flag-name> [toggle\|on\|off]` | Toggle Omarchy features between enabled and disabled | no | no | medium |
| `omarchy toggle bar` | `[toggle\|on\|off]` | Toggle bar visibility without killing the Omarchy shell | no | no | medium |
| `omarchy toggle crash capture` | `` | Toggle crash capture notifications | no | no | medium |
| `omarchy toggle enabled` | `<flag-name>` | Check if a toggle is enabled (flag file exists) | no | no | medium |
| `omarchy toggle fullscreen desktop` | `[toggle\|on\|off]` | Toggle a full screen desktop: hide the top bar and remove the window gaps together | no | no | high |
| `omarchy toggle hybrid gpu` | `` | Toggle dedicated vs integrated GPU mode via supergfxd (for hybrid gpu laptops, like Asus G14). | no | yes | medium |
| `omarchy toggle idle` | `[toggle\|stay-awake\|allow-idle\|status]` | Toggle idle behavior so the system either idles normally or stays awake | no | no | medium |
| `omarchy toggle input device` | `<touchpad\|touchscreen> [on\|off\|toggle]` | Enable, disable, or toggle a Hyprland input device | yes | no | medium |
| `omarchy toggle nightlight` | `[--status]` | Toggle nightlight screen temperature | no | no | medium |
| `omarchy toggle notification silencing` | `` | Toggle notification do-not-disturb mode | no | no | medium |
| `omarchy toggle screensaver` | `` | Toggle screensaver availability | no | no | medium |
| `omarchy toggle suspend` | `` | Toggle suspend availability in the system menu | no | no | medium |
| `omarchy toggle touchpad` | `[on\|off\|toggle]` | Enable, disable, or toggle the touchpad | no | no | medium |
| `omarchy toggle touchscreen` | `[on\|off\|toggle]` | Enable, disable, or toggle the touch functionality of the screen | no | no | medium |

### `transcode`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy transcode` | `[--path path] [input] [format] [resolution]` | Transcode pictures and videos for sharing | no | no | medium |
| `omarchy transcode ascii` | `<input-image.svg\|png> <output-path> [--width <columns>] [--height <rows>] [--mode <braille\|block>] [--threshold <percent>] [--invert]` | Transcode an image into ASCII/Unicode art text | no | no | medium |

### `tui`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy tui install` | `[name command window-style icon-url-or-name]` | Create a desktop launcher for a terminal UI app | no | no | high |
| `omarchy tui remove` | `[name]` | Remove a terminal UI desktop launcher | no | no | high |
| `omarchy tui remove all` | `` | Remove all TUIs installed via omarchy-tui-install. | no | no | high |

### `update`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy update` | `[-y]` | Update Omarchy and system packages | no | yes | high |
| `omarchy update analyze logs` | `` | Check the update log for known failure conditions | no | no | high |
| `omarchy update aur pkgs` | `` | Update AUR packages if any are installed | no | no | high |
| `omarchy update available` | `` | Check whether Omarchy updates are available. | no | no | high |
| `omarchy update confirm` | `` | Prompt for confirmation before starting an update | no | no | high |
| `omarchy update dev` | `` | Update the active Omarchy dev checkout | no | no | high |
| `omarchy update firmware` | `` | Update system firmware using fwupd. Ensures the fwupd EFI binary is installed | no | yes | high |
| `omarchy update keyring` | `` | Ensure the Omarchy and Arch keyring packages are installed and populated | no | yes | high |
| `omarchy update lock` | `<held\|run> [command] [args...]` | Run a command while holding the Omarchy update lock | yes | no | high |
| `omarchy update mise` | `` | Update mise-managed tools | no | no | high |
| `omarchy update orphan pkgs` | `` | Review and optionally remove orphaned system packages after updates | no | yes | high |
| `omarchy update pacman` | `<pacman-args>` | Run a pacman transaction for the Omarchy update flow, shielded from desktop session teardown. | yes | yes | high |
| `omarchy update pacman guard` | `` | Prevent direct pacman system upgrades from bypassing omarchy update. | yes | no | high |
| `omarchy update pkg prune` | `` | Prune superseded versions from the pacman package cache | no | yes | high |
| `omarchy update requires free space` | `` | Check free disk space required for an update | yes | no | high |
| `omarchy update restart` | `` | Prompt for required reboot or service restarts after updates | no | no | high |
| `omarchy update status` | `` | Refresh the shell update status | yes | no | high |
| `omarchy update stay awake` | `<start\|stop>` | Manage sleep and idle inhibition during an update | yes | no | high |
| `omarchy update system pkgs` | `` | Update system packages with pacman | no | yes | high |
| `omarchy update system pkgs when conflicted` | `` | Retry a system package update that hit a conflict | yes | no | high |
| `omarchy update time` | `` | Restart system time synchronization | no | yes | high |
| `omarchy update user notify` | `` | Compatibility wrapper for omarchy-migrate-notify. | yes | no | high |

### `upgrade`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy upgrade to quattro` | `[--yes] [--reboot] [--dev] [--channel stable\|rc\|edge] [--user USER]` | Upgrade a legacy Omarchy install to the package-backed Omarchy quattro layout. | no | yes | high |

### `upload`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy upload log` | `<log-file>` | Upload logs to logs.omarchy.org | yes | no | low |

### `version`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy version` | `` | Print the installed Omarchy version | no | no | high |
| `omarchy version branch` | `` | Print the active Omarchy dev-link git branch | yes | no | read-only |
| `omarchy version channel` | `` | Print the active Omarchy mirror and package channel | no | no | medium |
| `omarchy version pkgs` | `` | Print when system packages were last upgraded | no | no | high |

### `voxtype`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy voxtype config` | `` | Open Voxtype configuration | no | no | low |
| `omarchy voxtype install` | `` | Install and configure Voxtype dictation | no | yes | high |
| `omarchy voxtype model` | `` | Open Voxtype AI model setup | no | no | medium |
| `omarchy voxtype remove` | `` | Remove Voxtype dictation and its configuration | no | yes | high |
| `omarchy voxtype status` | `` | Stream voxtype --follow status as bar-friendly JSON | no | no | read-only |

### `weather`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy weather icon` | `` | Returns a weather condition icon, adjusted for live sunrise and sunset. | no | no | medium |
| `omarchy weather location` | `` | Show or set the location used for weather reports | no | no | medium |
| `omarchy weather status` | `` | Returns a formatted weather status string with temperature and wind speed. | no | no | high |

### `webapp`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy webapp handler hey` | `[url]` | Open HEY webmail and translate mailto links | no | no | low |
| `omarchy webapp handler zoom` | `[url]` | Open Zoom web meetings from browser protocol links | no | no | low |
| `omarchy webapp install` | `[name url icon-url-or-name [custom-exec] [mime-types]]` | Create a desktop launcher for a web app | no | no | high |
| `omarchy webapp remove` | `[name]` | Remove a web app desktop launcher | no | no | high |
| `omarchy webapp remove all` | `` | Remove all web apps installed via omarchy-webapp-install. | no | no | high |

### `windows`

| Route | Arguments | Summary | Hidden | Sudo | Risk |
|---|---|---|:---:|:---:|---|
| `omarchy windows key` | `` | Print the OEM Windows product key stored in firmware | no | yes | low |
| `omarchy windows vm` | `<install\|remove\|launch\|stop\|status> [options]` | Install, launch, stop, inspect, or remove the Windows VM | no | yes | high |

## Default keybindings

These are upstream defaults; the installed user’s bindings may override them. Inspect `~/.config/hypr/bindings.lua` before relying on a key.

| Key | Action | Implementation | Source |
|---|---|---|---|
| `ALT + PRINT` | Screenrecording | `"omarchy-capture-screenrecording --stop-recording \|\| omarchy-menu toggle trigger.capture.screenrecord")` | `default/hypr/bindings/utilities.lua:39` |
| `ALT + SHIFT + TAB` | Focus on previous window | `hl.dsp.window.cycle_next({ next = false }))` | `default/hypr/bindings/tiling.lua:48` |
| `ALT + SHIFT + TAB` | Reveal active window on top | `hl.dsp.window.bring_to_top())` | `default/hypr/bindings/tiling.lua:50` |
| `ALT + SHIFT + XF86AudioPlay` | Previous track | `"omarchy-shell media previous", { locked = true })` | `default/hypr/bindings/media.lua:29` |
| `ALT + TAB` | Focus on next window | `hl.dsp.window.cycle_next())` | `default/hypr/bindings/tiling.lua:47` |
| `ALT + TAB` | Reveal active window on top | `hl.dsp.window.bring_to_top())` | `default/hypr/bindings/tiling.lua:49` |
| `ALT + XF86AudioLowerVolume` | Volume down precise | `"omarchy-audio-output-volume -1", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:19` |
| `ALT + XF86AudioPlay` | Next track | `"omarchy-shell media next", { locked = true })` | `default/hypr/bindings/media.lua:25` |
| `ALT + XF86AudioRaiseVolume` | Volume up precise | `"omarchy-audio-output-volume +1", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:18` |
| `ALT + XF86MonBrightnessDown` | Brightness down precise | `"omarchy-brightness-display 1%-", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:21` |
| `ALT + XF86MonBrightnessUp` | Brightness up precise | `"omarchy-brightness-display +1%", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:20` |
| `CTRL + ALT + DELETE` | Close all windows | `"omarchy-hyprland-window-close-all")` | `default/hypr/bindings/tiling.lua:3` |
| `CTRL + ALT + SHIFT + TAB` | Focus on previous monitor | `hl.dsp.focus({ monitor = "-1" }))` | `default/hypr/bindings/tiling.lua:53` |
| `CTRL + ALT + TAB` | Focus on next monitor | `hl.dsp.focus({ monitor = "+1" }))` | `default/hypr/bindings/tiling.lua:52` |
| `F9` | Start dictation (push-to-talk) | `"voxtype record start")` | `default/hypr/bindings/voxtype.lua:3` |
| `F9` | Stop dictation (push-to-talk) | `"voxtype record stop", { release = true })` | `default/hypr/bindings/voxtype.lua:4` |
| `PRINT` | Screenshot | `"omarchy-capture-screenshot")` | `default/hypr/bindings/utilities.lua:38` |
| `SHIFT + XF86AudioMute` | Switch audio output | `"omarchy-audio-output-switch", { locked = true })` | `default/hypr/bindings/media.lua:32` |
| `SHIFT + XF86AudioPause` | Switch media source | `"omarchy-audio-source-switch", { locked = true })` | `default/hypr/bindings/media.lua:33` |
| `SHIFT + XF86AudioPlay` | Switch media source | `"omarchy-audio-source-switch", { locked = true })` | `default/hypr/bindings/media.lua:34` |
| `SHIFT + XF86MonBrightnessDown` | Brightness minimum | `"omarchy-brightness-display 1%", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:9` |
| `SHIFT + XF86MonBrightnessUp` | Brightness maximum | `"omarchy-brightness-display 100%", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:8` |
| `SUPER + ALT + DOWN` | Move window to group on bottom | `hl.dsp.window.move({ into_group = "d" }))` | `default/hypr/bindings/tiling.lua:82` |
| `SUPER + ALT + F` | Full width | `hl.dsp.window.fullscreen({ mode = "maximized" }))` | `default/hypr/bindings/tiling.lua:10` |
| `SUPER + ALT + G` | Move active window out of group | `hl.dsp.window.move({ out_of_group = true }))` | `default/hypr/bindings/tiling.lua:77` |
| `SUPER + ALT + Home` | Save window width | `"omarchy-hyprland-window-width save")` | `default/hypr/bindings/tiling.lua:12` |
| `SUPER + ALT + K` | Tmux keybindings | `"omarchy-menu-tmux-keybindings")` | `default/hypr/bindings/utilities.lua:11` |
| `SUPER + ALT + LEFT` | Move window to group on left | `hl.dsp.window.move({ into_group = "l" }))` | `default/hypr/bindings/tiling.lua:79` |
| `SUPER + ALT + RETURN` | Tmux | `{ omarchy = "terminal-tmux" })` | `default/hypr/bindings/applications.lua:12` |
| `SUPER + ALT + RIGHT` | Move window to group on right | `hl.dsp.window.move({ into_group = "r" }))` | `default/hypr/bindings/tiling.lua:80` |
| `SUPER + ALT + S` | Move window to scratchpad | `hl.dsp.window.move({ workspace = "special:scratchpad", follow = false }))` | `default/hypr/bindings/tiling.lua:29` |
| `SUPER + ALT + SHIFT + F` | File manager (cwd) | `{ omarchy = "nautilus-cwd" })` | `default/hypr/bindings/applications.lua:5` |
| `SUPER + ALT + SHIFT + TAB` | Previous window in group | `hl.dsp.group.prev())` | `default/hypr/bindings/tiling.lua:85` |
| `SUPER + ALT + SLASH` | Monitor scaling down | `"omarchy-hyprland-monitor-scaling down")` | `default/hypr/bindings/tiling.lua:98` |
| `SUPER + ALT + SPACE` | Apps menu | `"omarchy-menu toggle apps")` | `default/hypr/bindings/utilities.lua:2` |
| `SUPER + ALT + TAB` | Next window in group | `hl.dsp.group.next())` | `default/hypr/bindings/tiling.lua:84` |
| `SUPER + ALT + UP` | Move window to group on top | `hl.dsp.window.move({ into_group = "u" }))` | `default/hypr/bindings/tiling.lua:81` |
| `SUPER + ALT + code:10` | Switch to group window 1 | `hl.dsp.group.active({ index = 1 })` | `default/hypr/bindings/tiling.lua:93` |
| `SUPER + ALT + code:11` | Switch to group window 2 | `hl.dsp.group.active({ index = 2 })` | `default/hypr/bindings/tiling.lua:93` |
| `SUPER + ALT + code:12` | Switch to group window 3 | `hl.dsp.group.active({ index = 3 })` | `default/hypr/bindings/tiling.lua:93` |
| `SUPER + ALT + code:13` | Switch to group window 4 | `hl.dsp.group.active({ index = 4 })` | `default/hypr/bindings/tiling.lua:93` |
| `SUPER + ALT + code:14` | Switch to group window 5 | `hl.dsp.group.active({ index = 5 })` | `default/hypr/bindings/tiling.lua:93` |
| `SUPER + ALT + code:20` | Expand window left a little | `hl.dsp.window.resize({ x = -25, y = 0, relative = true }))` | `default/hypr/bindings/tiling.lua:60` |
| `SUPER + ALT + code:21` | Shrink window left a little | `hl.dsp.window.resize({ x = 25, y = 0, relative = true }))` | `default/hypr/bindings/tiling.lua:61` |
| `SUPER + ALT + code:34` | Make webcam overlay smaller | `"omarchy-capture-webcam-resize smaller")` | `default/hypr/bindings/utilities.lua:40` |
| `SUPER + ALT + code:35` | Make webcam overlay larger | `"omarchy-capture-webcam-resize larger")` | `default/hypr/bindings/utilities.lua:41` |
| `SUPER + ALT + comma` | Invoke last notification | `"omarchy-shell notifications invokeLast")` | `default/hypr/bindings/utilities.lua:28` |
| `SUPER + ALT + mouse_down` | Next window in group | `hl.dsp.group.next())` | `default/hypr/bindings/tiling.lua:90` |
| `SUPER + ALT + mouse_up` | Previous window in group | `hl.dsp.group.prev())` | `default/hypr/bindings/tiling.lua:91` |
| `SUPER + BACKSPACE` | Toggle window transparency | `"omarchy-hyprland-window-transparency-toggle")` | `default/hypr/bindings/utilities.lua:19` |
| `SUPER + C` | Universal copy | `universal_clipboard_shortcut("CTRL", "C", "CTRL", "Insert"))` | `default/hypr/bindings/clipboard.lua:45` |
| `SUPER + CTRL + A` | Audio | `"omarchy-shell shell toggle omarchy.audio")` | `default/hypr/bindings/utilities.lua:98` |
| `SUPER + CTRL + ALT + B` | Show battery remaining | `"omarchy-notification-battery")` | `default/hypr/bindings/utilities.lua:94` |
| `SUPER + CTRL + ALT + D` | Calendar | `"omarchy-shell shell toggle omarchy.clock")` | `default/hypr/bindings/utilities.lua:101` |
| `SUPER + CTRL + ALT + Delete` | Toggle laptop display mirroring | `"omarchy-hyprland-monitor-internal-mirror toggle")` | `default/hypr/bindings/utilities.lua:34` |
| `SUPER + CTRL + ALT + F` | Toggle full screen desktop | `"fullscreen-desktop")` | `default/hypr/bindings/utilities.lua:22` |
| `SUPER + CTRL + ALT + R` | Show reminders | `"omarchy-reminder show")` | `default/hypr/bindings/utilities.lua:90` |
| `SUPER + CTRL + ALT + T` | Show time | `"omarchy-notification-time")` | `default/hypr/bindings/utilities.lua:93` |
| `SUPER + CTRL + ALT + W` | Toggle weather | `"omarchy-notification-weather")` | `default/hypr/bindings/utilities.lua:95` |
| `SUPER + CTRL + ALT + Z` | Reset zoom | `function()` | `default/hypr/bindings/utilities.lua:123` |
| `SUPER + CTRL + B` | Bluetooth | `"omarchy-shell shell toggle omarchy.bluetooth")` | `default/hypr/bindings/utilities.lua:99` |
| `SUPER + CTRL + BACKSPACE` | Toggle single-window square aspect | `"omarchy-hyprland-window-single-square-aspect-toggle")` | `default/hypr/bindings/utilities.lua:21` |
| `SUPER + CTRL + C` | Capture menu | `"omarchy-menu toggle capture")` | `default/hypr/bindings/utilities.lua:4` |
| `SUPER + CTRL + D` | Display | `"omarchy-shell shell toggle omarchy.monitor")` | `default/hypr/bindings/utilities.lua:100` |
| `SUPER + CTRL + Delete` | Toggle laptop display | `"omarchy-hyprland-monitor-internal toggle")` | `default/hypr/bindings/utilities.lua:33` |
| `SUPER + CTRL + E` | Emojis | `"omarchy-shell shell toggle omarchy.emojis")` | `default/hypr/bindings/utilities.lua:3` |
| `SUPER + CTRL + F` | Tiled full screen | `"omarchy-hyprland-window-tiled-fullscreen-toggle")` | `default/hypr/bindings/tiling.lua:9` |
| `SUPER + CTRL + H` | Hardware menu | `"omarchy-menu toggle hardware")` | `default/hypr/bindings/utilities.lua:6` |
| `SUPER + CTRL + I` | Toggle locking on idle | `"idle")` | `default/hypr/bindings/utilities.lua:31` |
| `SUPER + CTRL + K` | Herdr keybindings | `"omarchy-menu-herdr-keybindings")` | `default/hypr/bindings/utilities.lua:12` |
| `SUPER + CTRL + L` | Lock system | `"omarchy-system-lock")` | `default/hypr/bindings/utilities.lua:127` |
| `SUPER + CTRL + LEFT` | Move grouped window focus left | `hl.dsp.group.prev())` | `default/hypr/bindings/tiling.lua:87` |
| `SUPER + CTRL + N` | Toggle nightlight | `"nightlight")` | `default/hypr/bindings/utilities.lua:32` |
| `SUPER + CTRL + O` | Toggle menu | `"omarchy-menu toggle toggle")` | `default/hypr/bindings/utilities.lua:5` |
| `SUPER + CTRL + P` | Power | `"omarchy-shell shell toggle omarchy.power")` | `default/hypr/bindings/utilities.lua:103` |
| `SUPER + CTRL + PERIOD` | Transcode | `"omarchy-transcode")` | `default/hypr/bindings/utilities.lua:87` |
| `SUPER + CTRL + PRINT` | Extract text (OCR) from screenshot | `"omarchy-capture-text")` | `default/hypr/bindings/utilities.lua:43` |
| `SUPER + CTRL + Q` | Calculator | `"omacalc")` | `default/hypr/bindings/utilities.lua:13` |
| `SUPER + CTRL + R` | Set reminder | `"omarchy-menu toggle reminder-set")` | `default/hypr/bindings/utilities.lua:89` |
| `SUPER + CTRL + RETURN` | Herdr | `{ omarchy = "terminal-herdr" })` | `default/hypr/bindings/applications.lua:13` |
| `SUPER + CTRL + RIGHT` | Move grouped window focus right | `hl.dsp.group.next())` | `default/hypr/bindings/tiling.lua:88` |
| `SUPER + CTRL + S` | Share | `"omarchy-menu toggle share")` | `default/hypr/bindings/utilities.lua:85` |
| `SUPER + CTRL + SHIFT + code:20` | Shrink window up a lot | `hl.dsp.window.resize({ x = 0, y = -300, relative = true }))` | `default/hypr/bindings/tiling.lua:67` |
| `SUPER + CTRL + SHIFT + code:21` | Expand window down a lot | `hl.dsp.window.resize({ x = 0, y = 300, relative = true }))` | `default/hypr/bindings/tiling.lua:68` |
| `SUPER + CTRL + SPACE` | Background switcher | `"omarchy-menu toggle background")` | `default/hypr/bindings/utilities.lua:17` |
| `SUPER + CTRL + T` | Activity | `{ tui = "btop" })` | `default/hypr/bindings/utilities.lua:104` |
| `SUPER + CTRL + TAB` | Former workspace | `hl.dsp.focus({ workspace = "previous" }))` | `default/hypr/bindings/tiling.lua:35` |
| `SUPER + CTRL + V` | Clipboard manager | `"omarchy-shell shell toggle omarchy.clipboard")` | `default/hypr/bindings/clipboard.lua:48` |
| `SUPER + CTRL + W` | Network | `"omarchy-shell shell toggle omarchy.network")` | `default/hypr/bindings/utilities.lua:102` |
| `SUPER + CTRL + X` | Toggle dictation | `"voxtype record toggle")` | `default/hypr/bindings/voxtype.lua:2` |
| `SUPER + CTRL + Z` | Zoom in | `function()` | `default/hypr/bindings/utilities.lua:118` |
| `SUPER + CTRL + code:10` | Bar panel 1 | `omarchy-shell -q shell togglePanelAt right 1` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:11` | Bar panel 2 | `omarchy-shell -q shell togglePanelAt right 2` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:12` | Bar panel 3 | `omarchy-shell -q shell togglePanelAt right 3` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:13` | Bar panel 4 | `omarchy-shell -q shell togglePanelAt right 4` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:14` | Bar panel 5 | `omarchy-shell -q shell togglePanelAt right 5` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:15` | Bar panel 6 | `omarchy-shell -q shell togglePanelAt right 6` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:16` | Bar panel 7 | `omarchy-shell -q shell togglePanelAt right 7` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:17` | Bar panel 8 | `omarchy-shell -q shell togglePanelAt right 8` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:18` | Bar panel 9 | `omarchy-shell -q shell togglePanelAt right 9` | `default/hypr/bindings/utilities.lua:105` |
| `SUPER + CTRL + code:20` | Expand window left a lot | `hl.dsp.window.resize({ x = -300, y = 0, relative = true }))` | `default/hypr/bindings/tiling.lua:65` |
| `SUPER + CTRL + code:21` | Shrink window left a lot | `hl.dsp.window.resize({ x = 300, y = 0, relative = true }))` | `default/hypr/bindings/tiling.lua:66` |
| `SUPER + CTRL + comma` | Toggle silencing notifications | `"notification-silencing")` | `default/hypr/bindings/utilities.lua:27` |
| `SUPER + DOWN` | Focus on below window | `hl.dsp.focus({ direction = "d" }))` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + ESCAPE` | System menu | `"omarchy-menu toggle system")` | `default/hypr/bindings/utilities.lua:8` |
| `SUPER + F` | Full screen | `hl.dsp.window.fullscreen({ mode = "fullscreen" }))` | `default/hypr/bindings/tiling.lua:8` |
| `SUPER + G` | Toggle window grouping | `hl.dsp.group.toggle())` | `default/hypr/bindings/tiling.lua:76` |
| `SUPER + Home` | Restore window width | `"omarchy-hyprland-window-width restore")` | `default/hypr/bindings/tiling.lua:13` |
| `SUPER + J` | Toggle window split | `hl.dsp.layout("togglesplit"))` | `default/hypr/bindings/tiling.lua:5` |
| `SUPER + K` | Keybindings | `"omarchy-menu-keybindings")` | `default/hypr/bindings/utilities.lua:10` |
| `SUPER + L` | Toggle workspace layout | `"omarchy-hyprland-workspace-layout-toggle")` | `default/hypr/bindings/tiling.lua:14` |
| `SUPER + LEFT` | Focus on left window | `hl.dsp.focus({ direction = "l" }))` | `default/hypr/bindings/tiling.lua:16` |
| `SUPER + O` | Pop window out (float & pin) | `"omarchy-hyprland-window-pop")` | `default/hypr/bindings/tiling.lua:11` |
| `SUPER + P` | Pseudo window | `hl.dsp.window.pseudo())` | `default/hypr/bindings/tiling.lua:6` |
| `SUPER + PRINT` | Color picker | `"pkill hyprpicker \|\| hyprpicker -a")` | `default/hypr/bindings/utilities.lua:42` |
| `SUPER + Q` | Close window | `hl.dsp.window.close())` | `default/hypr/bindings/tiling.lua:2` |
| `SUPER + RETURN` | Terminal | `{ omarchy = "terminal" })` | `default/hypr/bindings/applications.lua:2` |
| `SUPER + RIGHT` | Focus on right window | `hl.dsp.focus({ direction = "r" }))` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + S` | Toggle scratchpad | `hl.dsp.workspace.toggle_special("scratchpad"))` | `default/hypr/bindings/tiling.lua:28` |
| `SUPER + SHIFT + A` | ChatGPT | `{ webapp = "https://chatgpt.com" })` | `default/hypr/bindings/applications.lua:22` |
| `SUPER + SHIFT + ALT + A` | Grok | `{ webapp = "https://grok.com" })` | `default/hypr/bindings/applications.lua:23` |
| `SUPER + SHIFT + ALT + B` | Browser (private) | `{ omarchy = "browser --private" })` | `default/hypr/bindings/applications.lua:7` |
| `SUPER + SHIFT + ALT + DOWN` | Move workspace to down monitor | `hl.dsp.workspace.move({ monitor = "d" }))` | `default/hypr/bindings/tiling.lua:40` |
| `SUPER + SHIFT + ALT + E` | New email | `{ webapp = "https://app.hey.com/messages/new?display=standalone&new_window=true" })` | `default/hypr/bindings/applications.lua:26` |
| `SUPER + SHIFT + ALT + G` | WhatsApp | `{ webapp = "https://web.whatsapp.com/", focus = true })` | `default/hypr/bindings/applications.lua:28` |
| `SUPER + SHIFT + ALT + LEFT` | Move workspace to left monitor | `hl.dsp.workspace.move({ monitor = "l" }))` | `default/hypr/bindings/tiling.lua:37` |
| `SUPER + SHIFT + ALT + M` | Music TUI | `{ tui = "cliamp", focus = true })` | `default/hypr/bindings/applications.lua:15` |
| `SUPER + SHIFT + ALT + RIGHT` | Move workspace to right monitor | `hl.dsp.workspace.move({ monitor = "r" }))` | `default/hypr/bindings/tiling.lua:38` |
| `SUPER + SHIFT + ALT + UP` | Move workspace to up monitor | `hl.dsp.workspace.move({ monitor = "u" }))` | `default/hypr/bindings/tiling.lua:39` |
| `SUPER + SHIFT + ALT + X` | X Post | `{ webapp = "https://x.com/compose/post" })` | `default/hypr/bindings/applications.lua:33` |
| `SUPER + SHIFT + ALT + code:10` | Move window silently to workspace 1 | `hl.dsp.window.move({ workspace = "1", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:11` | Move window silently to workspace 2 | `hl.dsp.window.move({ workspace = "2", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:12` | Move window silently to workspace 3 | `hl.dsp.window.move({ workspace = "3", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:13` | Move window silently to workspace 4 | `hl.dsp.window.move({ workspace = "4", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:14` | Move window silently to workspace 5 | `hl.dsp.window.move({ workspace = "5", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:15` | Move window silently to workspace 6 | `hl.dsp.window.move({ workspace = "6", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:16` | Move window silently to workspace 7 | `hl.dsp.window.move({ workspace = "7", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:17` | Move window silently to workspace 8 | `hl.dsp.window.move({ workspace = "8", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:18` | Move window silently to workspace 9 | `hl.dsp.window.move({ workspace = "9", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:19` | Move window silently to workspace 10 | `hl.dsp.window.move({ workspace = "10", follow = false })` | `default/hypr/bindings/tiling.lua:19` |
| `SUPER + SHIFT + ALT + code:20` | Shrink window up a little | `hl.dsp.window.resize({ x = 0, y = -25, relative = true }))` | `default/hypr/bindings/tiling.lua:62` |
| `SUPER + SHIFT + ALT + code:21` | Expand window down a little | `hl.dsp.window.resize({ x = 0, y = 25, relative = true }))` | `default/hypr/bindings/tiling.lua:63` |
| `SUPER + SHIFT + ALT + comma` | Open notification history | `"omarchy-shell notifications showHistory")` | `default/hypr/bindings/utilities.lua:29` |
| `SUPER + SHIFT + B` | Browser | `{ omarchy = "browser" })` | `default/hypr/bindings/applications.lua:6` |
| `SUPER + SHIFT + BACKSPACE` | Toggle window gaps | `"omarchy-hyprland-window-gaps-toggle")` | `default/hypr/bindings/utilities.lua:20` |
| `SUPER + SHIFT + C` | Calendar | `{ webapp = "https://app.hey.com/calendar/weeks/" })` | `default/hypr/bindings/applications.lua:24` |
| `SUPER + SHIFT + CTRL + A` | Agent | `"omarchy-agent --pick")` | `default/hypr/bindings/utilities.lua:97` |
| `SUPER + SHIFT + CTRL + G` | Google Messages | `{ webapp = "https://messages.google.com/web/conversations", focus = true })` | `default/hypr/bindings/applications.lua:29` |
| `SUPER + SHIFT + CTRL + R` | Clear reminders | `"omarchy-reminder clear")` | `default/hypr/bindings/utilities.lua:91` |
| `SUPER + SHIFT + CTRL + SPACE` | Theme menu | `"omarchy-menu toggle theme")` | `default/hypr/bindings/utilities.lua:18` |
| `SUPER + SHIFT + D` | Docker | `{ tui = "omarchy-launch-docker-tui" })` | `default/hypr/bindings/applications.lua:16` |
| `SUPER + SHIFT + DOWN` | Swap window down | `hl.dsp.window.swap({ direction = "d" }))` | `default/hypr/bindings/tiling.lua:45` |
| `SUPER + SHIFT + E` | Email | `{ webapp = "https://app.hey.com" })` | `default/hypr/bindings/applications.lua:25` |
| `SUPER + SHIFT + F` | File manager | `{ omarchy = "nautilus" })` | `default/hypr/bindings/applications.lua:4` |
| `SUPER + SHIFT + G` | Signal | `{ omarchy = "signal" })` | `default/hypr/bindings/applications.lua:17` |
| `SUPER + SHIFT + LEFT` | Swap window to the left | `hl.dsp.window.swap({ direction = "l" }))` | `default/hypr/bindings/tiling.lua:42` |
| `SUPER + SHIFT + M` | Music | `{ omarchy = "spotify" })` | `default/hypr/bindings/applications.lua:14` |
| `SUPER + SHIFT + N` | Editor | `{ omarchy = "editor" })` | `default/hypr/bindings/applications.lua:8` |
| `SUPER + SHIFT + O` | Obsidian | `{ launch = "obsidian", focus = "^obsidian$" })` | `default/hypr/bindings/applications.lua:18` |
| `SUPER + SHIFT + P` | Google Photos | `{ webapp = "https://photos.google.com/", focus = true })` | `default/hypr/bindings/applications.lua:30` |
| `SUPER + SHIFT + RETURN` | Browser | `{ omarchy = "browser" })` | `default/hypr/bindings/applications.lua:3` |
| `SUPER + SHIFT + RIGHT` | Swap window to the right | `hl.dsp.window.swap({ direction = "r" }))` | `default/hypr/bindings/tiling.lua:43` |
| `SUPER + SHIFT + S` | Google Maps | `{ webapp = "https://maps.google.com/", focus = true })` | `default/hypr/bindings/applications.lua:31` |
| `SUPER + SHIFT + SLASH` | Passwords | `{ omarchy = "1password" })` | `default/hypr/bindings/applications.lua:20` |
| `SUPER + SHIFT + SPACE` | Toggle top bar | `"bar")` | `default/hypr/bindings/utilities.lua:16` |
| `SUPER + SHIFT + TAB` | Previous workspace | `hl.dsp.focus({ workspace = "e-1" }))` | `default/hypr/bindings/tiling.lua:34` |
| `SUPER + SHIFT + UP` | Swap window up | `hl.dsp.window.swap({ direction = "u" }))` | `default/hypr/bindings/tiling.lua:44` |
| `SUPER + SHIFT + W` | Omawrite | `{ launch = "omawrite" })` | `default/hypr/bindings/applications.lua:19` |
| `SUPER + SHIFT + X` | X | `{ webapp = "https://x.com/" })` | `default/hypr/bindings/applications.lua:32` |
| `SUPER + SHIFT + Y` | YouTube | `{ webapp = "https://youtube.com/" })` | `default/hypr/bindings/applications.lua:27` |
| `SUPER + SHIFT + code:10` | Move window to workspace 1 | `hl.dsp.window.move({ workspace = "1" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:11` | Move window to workspace 2 | `hl.dsp.window.move({ workspace = "2" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:12` | Move window to workspace 3 | `hl.dsp.window.move({ workspace = "3" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:13` | Move window to workspace 4 | `hl.dsp.window.move({ workspace = "4" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:14` | Move window to workspace 5 | `hl.dsp.window.move({ workspace = "5" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:15` | Move window to workspace 6 | `hl.dsp.window.move({ workspace = "6" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:16` | Move window to workspace 7 | `hl.dsp.window.move({ workspace = "7" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:17` | Move window to workspace 8 | `hl.dsp.window.move({ workspace = "8" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:18` | Move window to workspace 9 | `hl.dsp.window.move({ workspace = "9" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:19` | Move window to workspace 10 | `hl.dsp.window.move({ workspace = "10" })` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + SHIFT + code:20` | Shrink window up | `hl.dsp.window.resize({ x = 0, y = -100, relative = true }))` | `default/hypr/bindings/tiling.lua:57` |
| `SUPER + SHIFT + code:201` | Omarchy menu | `"omarchy-menu toggle root")` | `default/hypr/bindings/utilities.lua:7` |
| `SUPER + SHIFT + code:21` | Expand window down | `hl.dsp.window.resize({ x = 0, y = 100, relative = true }))` | `default/hypr/bindings/tiling.lua:58` |
| `SUPER + SHIFT + comma` | Dismiss all notifications | `"omarchy-shell notifications dismissAll")` | `default/hypr/bindings/utilities.lua:26` |
| `SUPER + SHIFT + grave` | Move window to scratchpad | `hl.dsp.window.move({ workspace = "special:scratchpad", follow = false }))` | `default/hypr/bindings/tiling.lua:31` |
| `SUPER + SLASH` | Monitor scaling up | `"omarchy-hyprland-monitor-scaling up")` | `default/hypr/bindings/tiling.lua:97` |
| `SUPER + SPACE` | Omarchy menu | `"omarchy-menu toggle")` | `default/hypr/bindings/utilities.lua:1` |
| `SUPER + T` | Toggle window floating/tiling | `hl.dsp.window.float({ action = "toggle" }))` | `default/hypr/bindings/tiling.lua:7` |
| `SUPER + TAB` | Next workspace | `hl.dsp.focus({ workspace = "e+1" }))` | `default/hypr/bindings/tiling.lua:33` |
| `SUPER + UP` | Focus on above window | `hl.dsp.focus({ direction = "u" }))` | `default/hypr/bindings/tiling.lua:18` |
| `SUPER + V` | Universal paste | `universal_clipboard_shortcut("CTRL", "V", "SHIFT", "Insert"))` | `default/hypr/bindings/clipboard.lua:46` |
| `SUPER + W` | Close window | `hl.dsp.window.close())` | `default/hypr/bindings/tiling.lua:1` |
| `SUPER + X` | Universal cut | `send_shortcut_once("CTRL", "X"))` | `default/hypr/bindings/clipboard.lua:47` |
| `SUPER + code:10` | Switch to workspace 1 | `hl.dsp.focus({ workspace = "1" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:11` | Switch to workspace 2 | `hl.dsp.focus({ workspace = "2" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:12` | Switch to workspace 3 | `hl.dsp.focus({ workspace = "3" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:13` | Switch to workspace 4 | `hl.dsp.focus({ workspace = "4" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:14` | Switch to workspace 5 | `hl.dsp.focus({ workspace = "5" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:15` | Switch to workspace 6 | `hl.dsp.focus({ workspace = "6" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:16` | Switch to workspace 7 | `hl.dsp.focus({ workspace = "7" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:17` | Switch to workspace 8 | `hl.dsp.focus({ workspace = "8" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:18` | Switch to workspace 9 | `hl.dsp.focus({ workspace = "9" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:19` | Switch to workspace 10 | `hl.dsp.focus({ workspace = "10" })` | `default/hypr/bindings/tiling.lua:17` |
| `SUPER + code:20` | Expand window left | `hl.dsp.window.resize({ x = -100, y = 0, relative = true }))` | `default/hypr/bindings/tiling.lua:55` |
| `SUPER + code:21` | Shrink window left | `hl.dsp.window.resize({ x = 100, y = 0, relative = true }))` | `default/hypr/bindings/tiling.lua:56` |
| `SUPER + comma` | Dismiss last notification | `"omarchy-shell notifications dismissOne")` | `default/hypr/bindings/utilities.lua:25` |
| `SUPER + grave` | Toggle scratchpad | `hl.dsp.workspace.toggle_special("scratchpad"))` | `default/hypr/bindings/tiling.lua:30` |
| `SUPER + mouse:272` | Move window | `hl.dsp.window.drag(), { mouse = true })` | `default/hypr/bindings/tiling.lua:73` |
| `SUPER + mouse:273` | Resize window | `hl.dsp.window.resize(), { mouse = true })` | `default/hypr/bindings/tiling.lua:74` |
| `SUPER + mouse_down` | Scroll active workspace forward | `hl.dsp.focus({ workspace = "e+1" }))` | `default/hypr/bindings/tiling.lua:70` |
| `SUPER + mouse_up` | Scroll active workspace backward | `hl.dsp.focus({ workspace = "e-1" }))` | `default/hypr/bindings/tiling.lua:71` |
| `XF86AudioLowerVolume` | Volume down | `"omarchy-audio-output-volume lower", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:3` |
| `XF86AudioMicMute` | Mute microphone | `"omarchy-audio-input-mute", { locked = true })` | `default/hypr/bindings/media.lua:5` |
| `XF86AudioMute` | Mute | `"omarchy-audio-output-volume mute-toggle", { locked = true })` | `default/hypr/bindings/media.lua:4` |
| `XF86AudioNext` | Next track | `"omarchy-shell media next", { locked = true })` | `default/hypr/bindings/media.lua:24` |
| `XF86AudioPause` | Pause | `"omarchy-shell media playPause", { locked = true })` | `default/hypr/bindings/media.lua:26` |
| `XF86AudioPlay` | Play | `"omarchy-shell media playPause", { locked = true })` | `default/hypr/bindings/media.lua:27` |
| `XF86AudioPrev` | Previous track | `"omarchy-shell media previous", { locked = true })` | `default/hypr/bindings/media.lua:28` |
| `XF86AudioRaiseVolume` | Volume up | `"omarchy-audio-output-volume raise", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:2` |
| `XF86Calculator` | Calculator | `"omacalc")` | `default/hypr/bindings/utilities.lua:14` |
| `XF86Eject` | Eject media | `"eject", { locked = true })` | `default/hypr/bindings/media.lua:30` |
| `XF86KbdBrightnessDown` | Keyboard brightness down | `"omarchy-brightness-keyboard down", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:11` |
| `XF86KbdBrightnessUp` | Keyboard brightness up | `"omarchy-brightness-keyboard up", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:10` |
| `XF86KbdLightOnOff` | Keyboard backlight cycle | `"omarchy-brightness-keyboard cycle", { locked = true })` | `default/hypr/bindings/media.lua:12` |
| `XF86MonBrightnessDown` | Brightness down | `"omarchy-brightness-display 5%-", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:7` |
| `XF86MonBrightnessUp` | Brightness up | `"omarchy-brightness-display +5%", { locked = true, repeating = true })` | `default/hypr/bindings/media.lua:6` |
| `XF86PowerOff` | Power menu | `"omarchy-menu toggle system", { locked = true })` | `default/hypr/bindings/utilities.lua:9` |
| `XF86TouchpadOff` | Disable touchpad | `"omarchy-toggle-touchpad off", { locked = true })` | `default/hypr/bindings/media.lua:15` |
| `XF86TouchpadOn` | Enable touchpad | `"omarchy-toggle-touchpad on", { locked = true })` | `default/hypr/bindings/media.lua:14` |
| `XF86TouchpadToggle` | Toggle touchpad | `"touchpad", { locked = true })` | `default/hypr/bindings/media.lua:13` |
| `switch:off:Lid Switch` |  | `"omarchy-hyprland-monitor-clamshell", { locked = true })` | `default/hypr/bindings/utilities.lua:36` |
| `switch:on:Lid Switch` |  | `"omarchy-system-lid-close", { locked = true })` | `default/hypr/bindings/utilities.lua:35` |

## Runtime freshness check

Run this on the target machine before relying on the pinned catalog:

```bash
omarchy commands --all --json > /tmp/omarchy-installed-commands.json
omarchy version
```

If the installed inventory differs, prefer its help/source and regenerate the registry. Do not assume a hidden/internal command is stable.

## Source basis

- Official repository: https://github.com/basecamp/omarchy
- Branch: `quattro`
- Commit: `9c5482c58dbe4974de337450754885083c91eada`
- Commit date: `2026-09-16T18:09:20+02:00`
- Primary extracted sources: `bin/omarchy`, all executable `bin/omarchy-*` metadata, `default/agents/skills/omarchy/`, and `default/hypr/bindings/*.lua`.
- The JSON includes a SHA-256 hash for every command implementation so downstream tooling can detect drift.
