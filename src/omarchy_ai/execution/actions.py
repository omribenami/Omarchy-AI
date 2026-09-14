"""What each tool call actually runs on the OS.

Ported from jarvisd (~/Git/jarvisd/src/jarvisd/actions.py), which proved
these out against this exact machine. Deliberately excludes logout/reboot/
shutdown — those are Level 3/4 per ADR-0001's policy table and need a real
confirm/policy layer before they're reachable by voice at all, not just a
description telling the model to ask first.

Every action shells out through an explicit argv list — never shell=True —
because arguments here can originate from a speech transcript by way of the
model's tool-call arguments, and that is untrusted input same as any other.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from dataclasses import dataclass

log = logging.getLogger("omarchy_ai.execution.actions")

_TIMEOUT = 10


@dataclass
class ActionResult:
    ok: bool
    message: str = ""


def _run(argv: list[str], timeout: float = _TIMEOUT) -> ActionResult:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False
        )
    except FileNotFoundError:
        return ActionResult(False, f"{argv[0]} is not installed")
    except subprocess.TimeoutExpired:
        return ActionResult(False, f"{argv[0]} timed out")
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        return ActionResult(False, err[0] if err else f"{argv[0]} failed")
    return ActionResult(True, (proc.stdout or "").strip())


def _run_detached(argv: list[str]) -> ActionResult:
    """Launch and don't wait — omarchy-launch-* scripts exec into the app
    itself rather than forking, so waiting would hang until the user closed
    the launched window."""
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        return ActionResult(False, f"{argv[0]} is not installed")
    return ActionResult(True, "")


def _clamp_percent(value) -> int:
    if value is None:
        value = 50
    return max(0, min(100, int(value)))


def _hyprctl_dispatch(lua_expr: str, classic_argv: list[str]) -> ActionResult:
    r = _run(["hyprctl", "dispatch", lua_expr])
    if r.ok:
        return r
    return _run(["hyprctl", "dispatch", *classic_argv])


# --- audio -------------------------------------------------------------

def volume_up(args: dict) -> ActionResult:
    return _run(["omarchy-audio-output-volume", "raise"])


def volume_down(args: dict) -> ActionResult:
    return _run(["omarchy-audio-output-volume", "lower"])


def volume_mute_toggle(args: dict) -> ActionResult:
    return _run(["omarchy-audio-output-volume", "mute-toggle"])


def volume_set(args: dict) -> ActionResult:
    pct = _clamp_percent(args.get("percent"))
    r = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"])
    if not r.ok:
        return r
    _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
    _run(["omarchy-osd", "-i", "volume-high", "-p", str(pct)])
    return ActionResult(True, f"volume set to {pct} percent")


def mic_mute_toggle(args: dict) -> ActionResult:
    return _run(["omarchy-audio-input-mute"])


# --- brightness ----------------------------------------------------------

def brightness_up(args: dict) -> ActionResult:
    return _run(["omarchy-brightness-display", "+10%"])


def brightness_down(args: dict) -> ActionResult:
    return _run(["omarchy-brightness-display", "10%-"])


def brightness_set(args: dict) -> ActionResult:
    pct = _clamp_percent(args.get("percent"))
    return _run(["omarchy-brightness-display", f"{pct}%"])


# --- capture / launchers -----------------------------------------------------

def screenshot(args: dict) -> ActionResult:
    return _run(["omarchy-capture-screenshot", "fullscreen"])


def lock_screen(args: dict) -> ActionResult:
    return _run(["omarchy-system-lock"])


def open_terminal(args: dict) -> ActionResult:
    return _run_detached(["omarchy-launch-terminal"])


def open_browser(args: dict) -> ActionResult:
    return _run_detached(["omarchy-launch-browser"])


def open_files(args: dict) -> ActionResult:
    return _run_detached(["omarchy-launch-nautilus"])


def open_editor(args: dict) -> ActionResult:
    return _run_detached(["omarchy-launch-editor"])


# --- window / workspace -----------------------------------------------------

def workspace_switch(args: dict) -> ActionResult:
    number = args.get("number")
    if not isinstance(number, int) or not (1 <= number <= 99):
        return ActionResult(False, "no valid workspace number given")
    return _hyprctl_dispatch(
        f"hl.dsp.focus({{ workspace = {number} }})", ["workspace", str(number)]
    )


def workspace_next(args: dict) -> ActionResult:
    return _hyprctl_dispatch('hl.dsp.focus({ workspace = "e+1" })', ["workspace", "e+1"])


def workspace_prev(args: dict) -> ActionResult:
    return _hyprctl_dispatch('hl.dsp.focus({ workspace = "e-1" })', ["workspace", "e-1"])


def close_window(args: dict) -> ActionResult:
    return _hyprctl_dispatch("hl.dsp.window.close()", ["killactive"])


def window_fullscreen_toggle(args: dict) -> ActionResult:
    return _hyprctl_dispatch("hl.dsp.window.fullscreen()", ["fullscreen"])


def list_windows(args: dict) -> ActionResult:
    """The model has no idea what's actually on screen otherwise — this is
    what lets it target the *right* window instead of guessing that
    "focused" already means whichever one the user is talking about."""
    r = _run(["hyprctl", "clients", "-j"])
    if not r.ok:
        return r
    active = _run(["hyprctl", "activewindow", "-j"])
    active_address = None
    if active.ok:
        try:
            active_address = json.loads(active.message).get("address")
        except (json.JSONDecodeError, AttributeError):
            pass
    try:
        clients = json.loads(r.message)
    except json.JSONDecodeError:
        return ActionResult(False, "could not parse window list")
    windows = [
        {
            "address": c.get("address"),
            "app": c.get("class"),
            "title": c.get("title"),
            "workspace": (c.get("workspace") or {}).get("id"),
            "fullscreen": bool(c.get("fullscreen")),
            "focused": c.get("address") == active_address,
        }
        for c in clients
        if c.get("mapped")
    ]
    return ActionResult(True, json.dumps(windows))


def list_commands(args: dict) -> ActionResult:
    from .keybindings import list_commands as _list_commands

    results = _list_commands(args.get("query"))
    return ActionResult(True, json.dumps(results))


def execute_command(args: dict) -> ActionResult:
    from .keybindings import execute_command as _execute_command

    title = args.get("title") or ""
    ok, message = _execute_command(title)
    return ActionResult(ok, message)


def type_text(args: dict) -> ActionResult:
    text = args.get("text")
    if not text:
        return ActionResult(False, "no text given")
    if shutil.which("wtype") is None:
        return ActionResult(False, "wtype is not installed")
    return _run(["wtype", "--", text])


def press_key(args: dict) -> ActionResult:
    key = args.get("key")
    if not key:
        return ActionResult(False, "no key given")
    if shutil.which("wtype") is None:
        return ActionResult(False, "wtype is not installed")
    return _run(["wtype", "-k", key])


def describe_screen(args: dict) -> ActionResult:
    from ..config import load_config
    from .vision import describe_screen as _describe_screen

    question = args.get("question") or "Briefly describe what's on the screen."
    cfg = load_config()
    text = _describe_screen(question, cfg.api_key_path, cfg.responses_model)
    if text.startswith("error:"):
        return ActionResult(False, text)
    return ActionResult(True, text)


def focus_window(args: dict) -> ActionResult:
    target = (args.get("target") or "").strip()
    if not target:
        return ActionResult(False, "no window specified")
    address = target
    if not target.startswith("0x"):
        r = _run(["hyprctl", "clients", "-j"])
        if not r.ok:
            return r
        try:
            clients = json.loads(r.message)
        except json.JSONDecodeError:
            return ActionResult(False, "could not parse window list")
        needle = target.lower()
        match = next(
            (
                c for c in clients
                if c.get("mapped")
                and (needle in (c.get("class") or "").lower() or needle in (c.get("title") or "").lower())
            ),
            None,
        )
        if match is None:
            return ActionResult(False, f"no window matching '{target}' found")
        address = match["address"]
    return _hyprctl_dispatch(
        f'hl.dsp.focus({{ window = "address:{address}" }})',
        ["focuswindow", f"address:{address}"],
    )


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


# --- media (soft dependency on playerctl) ------------------------------------

def _playerctl(*args: str) -> ActionResult:
    if shutil.which("playerctl") is None:
        return ActionResult(False, "playerctl is not installed")
    return _run(["playerctl", *args])


def media_play_pause(args: dict) -> ActionResult:
    return _playerctl("play-pause")


def media_next(args: dict) -> ActionResult:
    return _playerctl("next")


def media_prev(args: dict) -> ActionResult:
    return _playerctl("previous")


ACTIONS = {
    "volume_up": volume_up,
    "volume_down": volume_down,
    "volume_mute_toggle": volume_mute_toggle,
    "volume_set": volume_set,
    "mic_mute_toggle": mic_mute_toggle,
    "brightness_up": brightness_up,
    "brightness_down": brightness_down,
    "brightness_set": brightness_set,
    "screenshot": screenshot,
    "lock_screen": lock_screen,
    "open_terminal": open_terminal,
    "open_browser": open_browser,
    "open_files": open_files,
    "open_editor": open_editor,
    "workspace_switch": workspace_switch,
    "workspace_next": workspace_next,
    "workspace_prev": workspace_prev,
    "close_window": close_window,
    "window_fullscreen_toggle": window_fullscreen_toggle,
    "list_windows": list_windows,
    "focus_window": focus_window,
    "describe_screen": describe_screen,
    "list_commands": list_commands,
    "execute_command": execute_command,
    "type_text": type_text,
    "press_key": press_key,
    "bluetooth_toggle": bluetooth_toggle,
    "nightlight_toggle": nightlight_toggle,
    "battery_status": battery_status,
    "media_play_pause": media_play_pause,
    "media_next": media_next,
    "media_prev": media_prev,
}


def run_action(name: str, args: dict) -> ActionResult:
    fn = ACTIONS.get(name)
    if fn is None:
        log.error("no such action: %s", name)
        return ActionResult(False, f"unknown action '{name}'")
    try:
        return fn(args)
    except Exception:  # noqa: BLE001
        log.exception("action %s raised", name)
        return ActionResult(False, f"{name} crashed")
