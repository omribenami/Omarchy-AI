"""Screen, keyboard, lock-screen, launcher, bar and Omarchy CLI actions.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from ...config import load_config
from .. import sudo_approval
from .base import ActionResult, _TIMEOUT, _clamp_percent, _desktop_env, _hyprctl_dispatch, _run, _run_detached

log = logging.getLogger("omarchy_ai.execution.actions")


# --- brightness ----------------------------------------------------------

def brightness_up(args: dict) -> ActionResult:
    return _run(["omarchy-brightness-display", "+10%"])


def brightness_down(args: dict) -> ActionResult:
    return _run(["omarchy-brightness-display", "10%-"])


def brightness_set(args: dict) -> ActionResult:
    pct = _clamp_percent(args.get("percent"))
    return _run(["omarchy-brightness-display", f"{pct}%"])


# --- notifications -----------------------------------------------------------

def dismiss_notifications(args: dict) -> ActionResult:
    """Omarchy's own Super+Shift+comma / Super+comma. 2026-10-29 21:17-21:22:
    she had no way to clear a stack of stuck popups, and the shortcut never
    reached the desktop (no window focused)."""
    which = "dismissOne" if str(args.get("which") or "all").lower() == "last" else "dismissAll"
    result = _run(["omarchy-shell", "notifications", which])
    if not result.ok:
        return result
    return ActionResult(True, "Closed the latest notification" if which == "dismissOne"
                        else "Closed all notification popups (they stay in the notification history)")


# --- capture / launchers -----------------------------------------------------

def screenshot(args: dict) -> ActionResult:
    from ...config import load_config
    from ..vision import _capture, look

    path = _capture()
    if path is None:
        return ActionResult(False, "Screenshot capture failed; no saved image verified.")
    sent = _to_away_chat(path)
    cfg = load_config()
    if _gateway_key_configured(cfg):
        evidence = look(
            path,
            "Describe the visible desktop in this saved screenshot.",
            cfg,
            subject="the visible desktop screen",
            context=_screen_context(),
        )
        if evidence.startswith("error:"):
            return ActionResult(False, f"Screenshot saved at {path}, but visual inspection failed: {evidence}")
        return ActionResult(True, f"Screenshot saved at {path}{sent}. Visual evidence: {evidence}")
    return ActionResult(True, f"Screenshot saved at {path}{sent}")


def _to_away_chat(path) -> str:
    """The screenshot into the chat of a user who is not at this computer
    (typing on the phone, 2026-10-29 21:30: they could not see what she
    captured). Returns the note for the result, "" when not sent."""
    from ...core import conversations, origin, outbox
    cid = conversations.CURRENT.get()
    try:
        if not cid or origin.current(cid).get("at_this_computer") is not False:
            return ""
        used = outbox.attachment(str(path), origin_id=f"screenshot-{Path(path).name}", conversation=cid,
                                 channels=(outbox.CHAT,))
    except Exception:  # noqa: BLE001 -- the screenshot itself still worked
        log.warning("could not put the screenshot in the chat", exc_info=True)
        return ""
    return " and sent to the user's chat" if used else ""


def lock_screen(args: dict) -> ActionResult:
    return _run(["omarchy-system-lock"])


def _screen_locked() -> bool | None:
    r = _run(["omarchy-shell", "lock", "isLocked"])
    if r.ok and r.message.strip() in ("true", "false"):
        return r.message.strip() == "true"
    # The shell's IPC is down; the compositor still knows (exit 0 locked,
    # 1 unlocked, 2 undetermined).
    try:
        code = subprocess.run(["omarchy-hyprland-session-locked"], capture_output=True, timeout=2,
                              env=_desktop_env()).returncode
    except (OSError, subprocess.TimeoutExpired):
        return None
    return {0: True, 1: False}.get(code)


# The user, 2026-09-28: "she needs to make sure the screen is unlocked,
# otherwise everything she does ends up in the password field". Keystrokes on
# a locked screen go to the lock screen's password field, and failed
# attempts lock the account (pam_faillock here: 10 failures, 2 minutes).
SCREEN_LOCKED = ("Not run: the screen is locked. Anything typed now would go into the lock screen's password field, "
                 "and failed attempts can lock the account. Tell the user the screen is locked and ask them to unlock "
                 "it (from their paired phone: say unlock, or press its Unlock button), then do it again.")


def _keyboard_blocked() -> ActionResult | None:
    """A refusal when the screen is locked, else None (unknown state: allowed,
    as before this check existed)."""
    if _screen_locked():
        log.warning("keyboard input refused: the screen is locked")
        return ActionResult(False, SCREEN_LOCKED)
    return None


def unlock_screen(args: dict) -> ActionResult:
    """Refused here: only the paired phone may unlock (the user's decision,
    2026-09-27), through unlock_screen_for_paired_phone. Anyone near the room
    microphone could otherwise unlock the computer by talking."""
    return ActionResult(False, "Not run: unlocking works only from the user's paired phone (talking in the phone "
                               "app, or its Unlock button); the room microphone cannot unlock this computer. "
                               "Tell the user that.")


def unlock_screen_for_paired_phone() -> ActionResult:
    """Type the saved Sudo Access password into Omarchy's lock screen. Its
    IPC has lock/isLocked/status/preview and no unlock: PAM checks the typed
    password. 2026-09-27 09:05-09:06: asked from the phone to unlock, the
    catalog's nearest tool was lock_screen, and she locked it again twice."""
    locked = _screen_locked()
    if locked is False:
        return ActionResult(True, "the screen is not locked")
    if shutil.which("wtype") is None:
        return ActionResult(False, "wtype is not installed")
    if not load_config().sudo_access_enabled:
        return ActionResult(False, "unlocking needs the saved password: turn on Sudo Access in Assistant Settings")
    password = sudo_approval.retrieve()
    if password is None:
        return ActionResult(False, "no password is saved in GNOME Keyring; add it in Assistant Settings > Sudo Access")
    environment = _desktop_env()
    try:
        # Wake the panel (the lock powers it off), then type. The password
        # goes on stdin (`wtype -`), never on a command line.
        _hyprctl_dispatch('hl.dsp.dpms({ action = "enable" })', ["dpms", "on"])
        typed = subprocess.run(["wtype", "-"], input=password, capture_output=True, text=True,
                               timeout=_TIMEOUT, env=environment)
        entered = subprocess.run(["wtype", "-k", "return"], capture_output=True, text=True,
                                 timeout=_TIMEOUT, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        return ActionResult(False, "couldn't type into the lock screen")
    if typed.returncode or entered.returncode:
        return ActionResult(False, "couldn't type into the lock screen")
    for _ in range(20):
        time.sleep(0.25)
        if _screen_locked() is False:
            return ActionResult(True, "unlocked and verified: the lock screen is gone")
    return ActionResult(False, "typed the saved password but the screen is still locked (wrong saved password?)")


def _existing(args: dict) -> Path | None:
    raw = str((args or {}).get("path") or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    return path if path.exists() else None


def open_files(args: dict) -> ActionResult:
    """The file manager; with `path`, a window with that file selected (a file the
    assistant hands over, core/outbox.py)."""
    path = _existing(args)
    if path is None:
        return _run_detached(["omarchy-launch-nautilus"])
    argv = ["nautilus", "--new-window", "--select", str(path)] if path.is_file() else ["nautilus", "--new-window", str(path)]
    if shutil.which("uwsm-app"):
        argv = ["uwsm-app", "--", *argv]
    return _run_detached(argv)


def open_editor(args: dict) -> ActionResult:
    """The default editor; with `path`, that file opened in it."""
    path = _existing(args)
    return _run_detached(["omarchy-launch-editor"] + ([str(path)] if path is not None and path.is_file() else []))


def list_bar_icons(args: dict) -> ActionResult:
    from ..bar import entries
    try:
        return ActionResult(True, json.dumps(entries()))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return ActionResult(False, f"Could not discover top-bar icons: {exc}")


def close_bar_panel(args: dict) -> ActionResult:
    from ..bar import close_panel
    try:
        return ActionResult(*close_panel(args.get("id", "")))
    except Exception as exc:
        return ActionResult(False, f"Could not close top-bar panel: {exc}")


def open_bar_panel(args: dict) -> ActionResult:
    from ..bar import open_panel
    try:
        return ActionResult(*open_panel(args.get("id", "")))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return ActionResult(False, f"Could not open top-bar panel: {exc}")


def list_commands(args: dict) -> ActionResult:
    from ..keybindings import list_commands as _list_commands

    results = _list_commands(args.get("query"))
    return ActionResult(True, json.dumps(results))


def execute_command(args: dict) -> ActionResult:
    from ..keybindings import execute_command as _execute_command

    title = args.get("title") or ""
    ok, message = _execute_command(title)
    return ActionResult(ok, message)


_TERMINAL_CLASSES = ("foot", "kitty", "alacritty", "ghostty", "wezterm", "konsole", "terminal")


def _paste_text(text: str) -> ActionResult:
    """Paste as one block through the clipboard. wtype turns every newline
    into a real Return keystroke, so a multi-line prompt for Claude Code or
    Codex would be submitted after its first line and a multi-line shell
    snippet would run line by line. Pasting goes through the terminal's
    bracketed paste and lands as one unit; Return stays a separate,
    explicit press_key."""
    if shutil.which("wl-copy") is None:
        return ActionResult(False, "wl-copy is not installed")
    try:
        # wl-copy forks a child that keeps serving the clipboard; with the
        # output pipes captured subprocess.run would wait on that child.
        proc = subprocess.run(["wl-copy"], input=text, text=True, timeout=5, check=False,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=_desktop_env())
    except (OSError, subprocess.TimeoutExpired):
        return ActionResult(False, "wl-copy failed")
    if proc.returncode:
        return ActionResult(False, "wl-copy failed")
    active = _run(["hyprctl", "activewindow", "-j"], timeout=2)
    try:
        app = str(json.loads(active.message).get("class") or "").casefold() if active.ok else ""
    except ValueError:
        app = ""
    terminal = any(name in app for name in _TERMINAL_CLASSES)
    # Terminals reserve plain ctrl+v (literal-next in readline); every
    # terminal Omarchy ships binds ctrl+shift+v to clipboard paste.
    argv = ["wtype", "-M", "ctrl"] + (["-M", "shift"] if terminal else []) + ["-k", "v"]
    argv += (["-m", "shift"] if terminal else []) + ["-m", "ctrl"]
    result = _run(argv)
    if result.ok:
        return ActionResult(True, "pasted multi-line text as one block (clipboard now holds it)")
    return result


def type_text(args: dict) -> ActionResult:
    text = args.get("text")
    if not text:
        return ActionResult(False, "no text given")
    if shutil.which("wtype") is None:
        return ActionResult(False, "wtype is not installed")
    if blocked := _keyboard_blocked():
        return blocked
    # A trailing newline would submit implicitly; submission is press_key's job.
    text = text.rstrip("\r\n")
    if not text:
        return ActionResult(False, "no text given")
    if "\n" in text:
        return _paste_text(text)
    return _run(["wtype", "--", text])


_WTYPE_MODIFIERS = {"shift", "ctrl", "alt", "logo", "super", "win", "altgr", "capslock"}


# Hyprland's modmask bits for wtype's modifier names.
_MODMASK = {"shift": 1, "ctrl": 4, "alt": 8, "logo": 64, "super": 64, "win": 64}
# A binding with one of these in its description acts on the focused window
# (Super+W "Close window", Super+F "Full screen") or on the whole session
# (Super+Ctrl+L "Lock system"): it keeps the focus check.
_WINDOW_BINDING = re.compile(r"window|full ?screen|full width|float|pin|split|group|move|resize|swap|focus"
                             r"|lock|power|suspend|shut|reboot|log ?out|exit", re.I)


def desktop_shortcut(key, modifiers) -> str | None:
    """The description of the Hyprland binding this Super shortcut runs when
    it does not act on any window (Super+Shift+comma "Dismiss all
    notifications"), else None. Such a key goes to the desktop, not to the
    focused window, so it needs no verified focus (2026-10-29 21:19: the
    shortcut was refused four times, "focus is unverified")."""
    modifiers = [modifiers] if isinstance(modifiers, str) else list(modifiers or [])
    names = {str(m).strip().lower() for m in modifiers}
    if not key or not names & {"logo", "super", "win"} or not names <= set(_MODMASK):
        return None
    mask = 0
    for name in names:
        mask |= _MODMASK[name]
    try:
        binds = json.loads(subprocess.check_output(["hyprctl", "binds", "-j"], env=_desktop_env(), text=True,
                                                   timeout=5))
    except Exception:  # noqa: BLE001 -- unknown: it needs focus as before
        return None
    for bind in binds:
        if bind.get("modmask") == mask and str(bind.get("key") or "").lower() == str(key).lower() \
                and not bind.get("submap"):
            description = str(bind.get("description") or "")
            return description if description and not _WINDOW_BINDING.search(description) else None
    return None


def press_key(args: dict) -> ActionResult:
    key = args.get("key")
    if not key:
        return ActionResult(False, "no key given")
    if shutil.which("wtype") is None:
        return ActionResult(False, "wtype is not installed")
    if blocked := _keyboard_blocked():
        return blocked

    modifiers = args.get("modifiers") or []
    if isinstance(modifiers, str):
        modifiers = [modifiers]
    if title := desktop_shortcut(key, modifiers):
        # An Omarchy shortcut runs its own bound command, as execute_command
        # does: no synthetic keys, no window to reach. 2026-10-29 21:19:
        # Super+Shift+comma was pressed as keys and never got there.
        return execute_command({"title": title})
    # wtype's modifier names: shift/ctrl/alt/logo/altgr/capslock. "super"/
    # "win" are common aliases people (and the model) would reach for — map
    # them rather than fail on a technicality.
    normalized = []
    for m in modifiers:
        m = m.strip().lower()
        if m in ("super", "win"):
            m = "logo"
        if m not in _WTYPE_MODIFIERS:
            return ActionResult(False, f"unknown modifier '{m}'")
        normalized.append(m)

    argv = ["wtype"]
    for m in normalized:
        argv += ["-M", m]
    argv += ["-k", key]
    for m in reversed(normalized):
        argv += ["-m", m]
    probe = _browser_enter_probe(key, normalized)
    result = _run(argv)
    if probe is None or not result.ok:
        return result
    from ..browser_control import after_enter
    return after_enter(*probe)


def _browser_enter_probe(key: str, modifiers: list) -> tuple | None:
    """Enter going to the assistant's own browser: what to check afterwards
    (browser_control.after_enter), or None for any other key or window."""
    if modifiers or str(key).lower() not in ("return", "enter", "kp_enter"):
        return None
    try:
        from ..browser_jev import _dedicated_window
        env = _desktop_env()
        clients = json.loads(subprocess.check_output(["hyprctl", "clients", "-j"], env=env, text=True, timeout=5))
        active = json.loads(subprocess.check_output(["hyprctl", "activewindow", "-j"], env=env, text=True, timeout=5))
        window = _dedicated_window(clients)
    except Exception:  # noqa: BLE001 -- unknown: the key goes as before
        return None
    if not window or window.get("address") != active.get("address"):
        return None
    from ..browser_control import enter_probe
    return enter_probe()


def submit_sudo_password(_args: dict) -> ActionResult:
    """Type the user-enabled keyring password into the focused sudo prompt."""
    if shutil.which("wtype") is None:
        return ActionResult(False, "wtype is not installed")
    if not load_config().sudo_access_enabled:
        return ActionResult(False, "persistent Sudo Access is disabled in Assistant Settings")
    if blocked := _keyboard_blocked():
        return blocked
    password = sudo_approval.retrieve()
    if password is None:
        return ActionResult(False, "no sudo password is saved in GNOME Keyring; add it in Assistant Settings")
    try:
        environment = _desktop_env()
        typed = subprocess.run(
            ["wtype", "--", password], capture_output=True, text=True,
            timeout=_TIMEOUT, env=environment,
        )
        if typed.returncode != 0:
            return ActionResult(False, "couldn't enter the approved password")
        entered = subprocess.run(
            ["wtype", "-k", "return"], capture_output=True, text=True,
            timeout=_TIMEOUT, env=environment,
        )
        if entered.returncode != 0:
            return ActionResult(False, "password was entered but Return could not be sent")
    except (OSError, subprocess.TimeoutExpired):
        return ActionResult(False, "couldn't enter the approved password")
    return ActionResult(True, "approved sudo password submitted")


def describe_screen(args: dict) -> ActionResult:
    result = _describe_screen_raw(args)
    hint = _terminal_text_hint()
    return ActionResult(result.ok, result.message + hint) if hint else result


def _terminal_text_hint() -> str:
    """2026-09-24 01:16-01:20: ~40 screenshots of an ssh terminal instead of
    its log; a vision summary drops lines, and a docker-compose was then
    written from those summaries. When the focused window is a terminal
    with a readable log, say so in the result she is reading."""
    from .. import tile_logs
    focused = next((c for c in tile_logs._clients() if c.get("focusHistoryID") == 0), {})
    address = focused.get("address")
    if not address or focused.get("class") not in tile_logs._TERMINAL_CLASSES or not tile_logs.find_log(address):
        return ""
    return (f"\n[The focused window is a terminal with a readable log: for its exact text use "
            f"read_tile_log window='{address}', not screenshots.]")


def _describe_screen_raw(args: dict) -> ActionResult:
    from ...config import load_config
    from ..vision import _capture, look

    question = args.get("question") or "Briefly describe what's on the screen."
    cfg = load_config()
    if not _gateway_key_configured(cfg):
        return ActionResult(False, "error: a Vercel AI Gateway key is required for Jev image recognition")
    path = _capture()
    if path is None:
        return ActionResult(False, "Screenshot capture failed; screen contents unverified.")
    text = look(path, question, cfg, subject="the visible desktop screen", context=_screen_context())
    return ActionResult(not text.startswith("error:"), text)


def _screen_context() -> str:
    """Cheap text context for Jev; no screenshot pixels leave vision.py."""
    from .. import tile_logs

    try:
        clients = tile_logs._clients()
    except Exception:
        return ""
    focused = next((client for client in clients if client.get("focusHistoryID") == 0), {})
    focused_class = str(focused.get("class") or "unknown")[:120]
    titles = []
    for client in clients[:20]:
        title = " ".join(str(client.get("title") or "").split())[:160]
        if title:
            titles.append(title)
    lines = [f"Focused window class: {focused_class}."]
    if titles:
        lines.append("Open window titles: " + "; ".join(titles))
    return "\n".join(lines)


def _gateway_key_configured(cfg) -> bool:
    gateway_key_path = getattr(cfg, "vercel_gateway_api_key_path", "")
    if not gateway_key_path:
        return False
    try:
        return bool(Path(gateway_key_path).expanduser().read_text().strip())
    except OSError:
        return False


# --- misc system toggles -----------------------------------------------------

def bluetooth_toggle(args: dict) -> ActionResult:
    return _run(["omarchy-bluetooth-power", "toggle"])


def nightlight_toggle(args: dict) -> ActionResult:
    return _run(["omarchy-toggle-nightlight"])


def battery_status(args: dict) -> ActionResult:
    if shutil.which("omarchy-battery-present"):
        present = subprocess.run(
            ["omarchy-battery-present"], capture_output=True, timeout=_TIMEOUT, check=False
        )
        if present.returncode != 0:
            return ActionResult(True, "no battery detected")
    return _run(["omarchy-battery-status"])


# --- Reminders (dedicated wrapper around `omarchy reminder`) -----------
# Already reachable through run_omarchy_command below (reminder is in its
# allowlist), but same reasoning as nightlight_toggle: a common, well-
# defined action gets its own typed tool rather than leaning on the model
# to hand-format generic CLI args correctly every time. Confirmed live
# against the real omarchy-reminder binary (`omarchy reminder --help`):
# relative minutes-from-now only, no absolute/natural-language time
# support on its side -- the model converts "in 20 minutes"/"at 3pm"/etc.
# into a minute count itself before calling this.


def set_reminder(args: dict) -> ActionResult:
    minutes = args.get("minutes")
    if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or minutes <= 0:
        return ActionResult(False, "minutes must be a positive number")
    minutes = int(minutes)
    message = args.get("message")
    if message is not None and not isinstance(message, str):
        return ActionResult(False, "message must be a string")
    argv = ["omarchy", "reminder", str(minutes)]
    if message:
        argv.append(message)
    result = _run(argv, timeout=15)
    if not result.ok:
        return result
    unit = "minute" if minutes == 1 else "minutes"
    return ActionResult(True, f"Reminder set for {minutes} {unit} from now" + (f": {message}" if message else "."))


def list_reminders(args: dict) -> ActionResult:
    return _run(["omarchy", "reminder", "show", "--json"], timeout=15)


def clear_reminders(args: dict) -> ActionResult:
    result = _run(["omarchy", "reminder", "clear"], timeout=15)
    if not result.ok:
        return result
    return ActionResult(True, "All reminders cleared.")


# --- Omarchy CLI passthrough (skill parity) -----------------------------
# The Claude Code "omarchy" skill (~/.claude/skills/omarchy/SKILL.md) gives
# an agent editing this machine's desktop config the full `omarchy` CLI --
# themes, reminders, bar layout, toggles, hooks, plugins, packages, system
# power, etc. The voice assistant had none of that, only the fixed,
# hand-picked actions in this file. This gives it a real slice of the same
# capability, deliberately narrower: an explicit allowlist of the command
# groups that are genuinely Level 2 (reversible, no real Level 3+ blast
# radius) per ADR-0001's policy table -- the same bar every other tool in
# this file (and tools.py's own header comment) already holds to.
#
# Left out on purpose, same reasoning as excluding logout/reboot/shutdown
# above: `pkg`/`update`/`reinstall`/`dev`/`system` (package/OS-level
# changes -- Level 3+, no confirm layer exists for voice yet); `refresh`
# (config reset -- the skill's own instructions require a human to
# confirm before running it, which a voice tool can't honor without a
# real policy layer); `hook`/`plugin` (install code that runs
# automatically on future system events -- a persistent blast radius,
# not a single reversible command like the rest of this allowlist).
_OMARCHY_SAFE_GROUPS = {"theme", "toggle", "reminder", "bar", "capture"}

# Even within an otherwise-safe group, some subcommands don't belong here.
# Confirmed live by reading their actual source (omarchy-theme-install):
# `theme install <git-repo-url>` runs a real `git clone` of a URL that, on
# a voice assistant, would come straight from a speech transcript --
# untrusted input driving a network fetch onto this machine, the same
# category of risk this file's own module docstring calls out for every
# other action. `theme remove`/`theme update` are lower-risk (no new
# remote content) but still destructive/network operations on
# already-installed themes -- excluded for the same "stay inside genuinely
# reversible, no-surprises territory" reasoning as the group-level list.
_OMARCHY_BLOCKED_SUBCOMMANDS = {
    "theme": {"install", "remove", "update"},
}


def run_omarchy_command(args: dict) -> ActionResult:
    argv = args.get("args")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
        return ActionResult(False, "no command given")
    group = argv[0].strip().lower()
    if group not in _OMARCHY_SAFE_GROUPS:
        return ActionResult(
            False,
            f"'{group}' isn't one of the command groups this tool is allowed "
            f"to run ({', '.join(sorted(_OMARCHY_SAFE_GROUPS))} only) -- "
            "packages, updates, reinstalling, hooks, plugins, and system "
            "power aren't voice-reachable.",
        )
    blocked = _OMARCHY_BLOCKED_SUBCOMMANDS.get(group, set())
    if len(argv) > 1 and argv[1].strip().lower() in blocked:
        return ActionResult(
            False,
            f"'{group} {argv[1]}' isn't voice-reachable (it fetches/removes "
            "content rather than just changing a setting) -- ask the user "
            "to run that one themselves.",
        )
    if _interactive_capture(argv):
        # 2026-10-29 21:30: "capture screen" ran the region picker, which
        # waited 30 s for a mouse drag nobody made; its leftover slurp then
        # made the next real capture exit without an image.
        return ActionResult(False, "Not run: that capture waits for the user to drag a region with the mouse. "
                            "For a picture of the screen use the screenshot tool (or describe_screen to look).")
    return _run(["omarchy", *argv], timeout=30)


def _interactive_capture(argv: list[str]) -> bool:
    """`omarchy capture screenshot` / `screenrecord` in a mode that asks the
    user to pick a region or window."""
    words = [a.strip().lower() for a in argv]
    if words[:1] == ["capture"]:
        words = words[1:]
    if not words or words[0] not in ("screenshot", "screenrecord"):
        return False
    mode = next((w for w in words[1:] if not w.startswith("-")), "smart" if words[0] == "screenshot" else "")
    return mode in ("smart", "region", "windows", "window")
