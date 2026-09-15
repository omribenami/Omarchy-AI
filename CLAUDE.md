# Environment notes for agents working in this repo

This project runs on **Omarchy** (`ID=omarchy`, `ID_LIKE=arch` — an
Arch-based Hyprland desktop distro), not Debian/Ubuntu/Fedora. A real
mistake already happened here: an agent tried to install a package with
the wrong package manager. Before installing anything, use:

- **`pacman`** for official repo packages: `sudo pacman -S <pkg>` (never
  `apt`, `apt-get`, `dnf`, `yum`, `zypper`, `brew`, or bare `pip install`
  for system-level tools — they don't exist here or aren't how this
  system's packages are tracked).
- **`yay`** for AUR packages (confirmed installed): `yay -S <pkg>`.
- Check what's already installed before assuming something is missing —
  `pacman -Qi <pkg>` / `pacman -Qs <name>` / `command -v <bin>`. This
  machine has a lot already (GStreamer + gst-plugins-*, PipeWire,
  xdg-desktop-portal-hyprland, ffmpeg with VAAPI encoders, etc. — see
  docs/ADR-0001-architecture.md's "Confirmed available" section for the
  running list, keep it updated as you confirm more).
- Omarchy ships its own `omarchy-*` helper scripts for common
  desktop actions (`omarchy-capture-screenshot`, `omarchy-audio-*`,
  `omarchy-launch-*`, `omarchy-menu-keybindings`, etc.) and Hyprland
  0.56+ uses a **Lua dispatch API** for `hyprctl dispatch` (see
  `src/omarchy_ai/execution/actions.py`'s `_hyprctl_dispatch` for the
  pattern: try the Lua form, fall back to classic). Prefer these existing
  scripts/patterns over reinventing desktop control — grep this repo
  first, most of it has already been figured out once.
- Python: this repo uses `uv` for packaging. The venv is at `.venv`
  (`.venv/bin/python`, currently 3.14) — activate it
  (`source .venv/bin/activate`) or call it directly rather than assuming
  a bare `python3`/`pip` on PATH is the right one.
- Android/JDK tooling is pinned via `mise` (`mise.toml`: Temurin 21,
  `ANDROID_SDK_ROOT`/`ANDROID_HOME` under `~/Android/Sdk`) — make sure
  `mise`'s env is active before `./gradlew`/`adb`/`android` commands in
  `android-receiver/`.
- Don't casually restart system-wide services (`pipewire`,
  `pipewire-pulse`, `wireplumber`, etc.) — this machine runs a live voice
  daemon (`omarchy-ai.service`) that depends on PipeWire audio capture,
  and a careless restart has already broken a live voice session once
  (confirmed: `BrokenPipeError` mid-session). If a restart is genuinely
  needed, flag it clearly first rather than just doing it.
- `src/omarchy_ai/voice/` and `omarchy-ai.service` are a separate,
  currently-live subsystem (the voice assistant) — don't modify or
  restart them while working on anything else (e.g. the Android casting
  work) unless that is specifically the task.

For narrative history, prior debugging, and architecture decisions, read
`STATUS.md` and `docs/ADR-0001-architecture.md` first — this repo has been
built with an extensive evidence-driven-debugging convention (real
`journalctl`/log evidence over guesses, real hardware tests over
assumptions) and keeps a full trail of what was tried and why. Keep that
convention going, including documenting new findings there.
