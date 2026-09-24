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

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import time
import base64
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from .. import myapi
from ..config import load_config
from ..myapi import usage as myapi_usage
from . import files as local_files
from . import sudo_approval
from .desktop_env import desktop_env

log = logging.getLogger("omarchy_ai.execution.actions")

_TIMEOUT = 10

# --- Android TV casting (Phase 2) --------------------------------------
# actions.py lives at <repo>/src/omarchy_ai/execution/actions.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VENV_PYTHON = str(_REPO_ROOT / ".venv" / "bin" / "python")
# The last-known-TV fallback constant used to live here; moved to
# display/discovery.py (TV_ADB_ADDR) so display/registry.py can use it
# too without a circular import between registry.py and this file.
_SIGNALING_PORT = 8765
_APK_PATH = (
    _REPO_ROOT / "android-receiver" / "app" / "build" / "outputs" / "apk" / "debug" / "app-debug.apk"
)
# Records the source fingerprint (see _source_fingerprint) the APK at
# _APK_PATH was actually built from, so _ensure_receiver_apk_built can tell
# "already built" apart from "built once, long since gone stale" -- before
# this it only ever checked whether the file existed, so an APK built
# months ago (versionCode/versionName hardcoded then too, see
# android-receiver/app/build.gradle.kts) would never get rebuilt no matter
# how much source changed.
_APK_BUILD_MARKER = _APK_PATH.with_suffix(".built-from-hash")
# Set by install_receiver_on_tv's discovery step, consumed by its pairing
# step on the next call -- same "module-level state across separate tool
# calls" pattern used by the pairing conversation.
_pending_pair_target: dict | None = None


@dataclass
class ActionResult:
    ok: bool
    message: str = ""


def _desktop_env() -> dict[str, str]:
    """Backward-compatible alias for callers that imported this helper."""
    return desktop_env()


def _run(argv: list[str], timeout: float = _TIMEOUT, cwd: str | None = None) -> ActionResult:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd, env=_desktop_env()
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
            env=_desktop_env(),
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
    from ..config import load_config
    from .vision import _capture, inspect_gateway_image

    path = _capture()
    if path is None:
        return ActionResult(False, "Screenshot capture failed; no saved image verified.")
    cfg = load_config()
    if _gateway_key_configured(cfg):
        evidence = inspect_gateway_image(path, "Describe the visible desktop in this saved screenshot.", cfg)
        if evidence.startswith("error:"):
            return ActionResult(False, f"Screenshot saved at {path}, but visual inspection failed: {evidence}")
        return ActionResult(True, f"Screenshot saved at {path}. Visual evidence: {evidence}")
    return ActionResult(True, f"Screenshot saved at {path}")


def lock_screen(args: dict) -> ActionResult:
    return _run(["omarchy-system-lock"])


def open_terminal(args: dict) -> ActionResult:
    # Wraps the launch so this terminal's output gets tracked in a
    # per-tile log (see tile_logs.py) — read_tile_log can then answer
    # "what happened in that terminal" from real text instead of a
    # describe_screen vision call. Falls back to a plain, untracked
    # launch if tile-log setup itself fails for any reason; a terminal
    # that opens without logging is far better than one that doesn't
    # open at all.
    try:
        from . import tile_logs

        _initial_log_path, argv_prefix, terminal_label = tile_logs.start_terminal_log()
        result = _run_detached(["omarchy-launch-terminal", *argv_prefix])
        if result.ok:
            result.message = (
                f"opened {terminal_label}; use that exact title when focusing it "
                "or reading its terminal log"
            )
        return result
    except Exception:  # noqa: BLE001
        log.exception("tile_logs setup failed, opening terminal untracked")
        return _run_detached(["omarchy-launch-terminal"])


def read_tile_log(args: dict) -> ActionResult:
    from . import tile_logs, workbench

    window = args.get("window")
    if window and not str(window).startswith("0x") and workbench.exists(str(window)):
        # An assistant terminal from terminal_task (2026-09-24: 'find-docker-compose').
        return workbench.read(str(window))
    text = tile_logs.read_log(window)
    available = not text.startswith(("no terminal", "more than one terminal", "failed to read log:"))
    return ActionResult(available, text)


def open_browser(args: dict) -> ActionResult:
    # Omarchy-ai owns one browser: the dedicated Jev-ultrafast Chromium
    # profile. Do not open the user's normal browser and then drive it with
    # keyboard focus, which is both ambiguous and unsafe.
    try:
        from .browser_jev import _ensure_dedicated_browser
        _ensure_dedicated_browser()
        from .browser_jev import _focus_dedicated_window
        _focus_dedicated_window()
        return ActionResult(True, "dedicated Jev browser ready")
    except Exception as exc:
        return ActionResult(False, f"dedicated Jev browser unavailable: {exc}")


def browser_task(args: dict) -> ActionResult:
    """Run jev-ultrafast with Gateway-backed Jev decisions and one Gateway key."""
    try:
        from .browser_jev import run_browser_task
        return run_browser_task(args, load_config())
    except Exception as exc:
        log.exception("browser task failed before execution")
        return ActionResult(False, f"browser task unavailable; no browser action executed: {exc}")


def open_files(args: dict) -> ActionResult:
    return _run_detached(["omarchy-launch-nautilus"])


def open_editor(args: dict) -> ActionResult:
    return _run_detached(["omarchy-launch-editor"])


# --- local files -----------------------------------------------------------

def list_files(args: dict) -> ActionResult:
    try:
        result = local_files.list_files(args.get("path"), load_config(), bool(args.get("recursive")))
        return ActionResult(True, json.dumps(result))
    except (local_files.FileAccessError, ValueError) as exc:
        return ActionResult(False, str(exc))


def read_file(args: dict) -> ActionResult:
    try:
        result = local_files.read_file(
            args.get("path"), load_config(), args.get("start_line", 1), args.get("max_chars", 4_000),
            system_config=bool(args.get("system_config")),
        )
        if result and _looks_like_script(result):
            result += _SCRIPT_HINT
        return ActionResult(True, result or "(empty file)")
    except (local_files.FileAccessError, ValueError) as exc:
        return ActionResult(False, str(exc))


# Real Gemini Live, commercial_prompt.md (2026-09-23): after read_file it
# ignored run_mission and started doing the steps one tool at a time again,
# which is how narration and step order were lost. The nudge belongs in the
# result the model is reading at that moment, not only in a long prompt.
_SCRIPT_HINT = ("\n\n[assistant note] This file is a multi-step script. If the user asked you to perform it, "
                "call run_mission ONCE now with all of its steps (say + action each, its exact targets and "
                "workspace rule); do not perform the steps yourself one by one.")


def _looks_like_script(text: str) -> bool:
    lines = [l.strip().lower() for l in text.splitlines() if l.strip()]
    numbered = sum(1 for l in lines if re.match(r"^(\d+[.)]|step \d+|- )", l))
    cues = sum(w in text.lower() for w in ("narrat", "demonstrat", "step", "then ", "finally", "workspace"))
    return numbered >= 3 or (numbered >= 2 and cues >= 2)


def write_file(args: dict) -> ActionResult:
    try:
        path = local_files.write_file(
            args.get("path"), args.get("content"), load_config(), bool(args.get("overwrite")),
        )
        return ActionResult(True, f"saved {path}")
    except (local_files.FileAccessError, ValueError) as exc:
        return ActionResult(False, str(exc))


def edit_file(args: dict) -> ActionResult:
    try:
        config = load_config()
        privileged = bool(args.get("privileged"))
        password = None
        if privileged:
            if not config.sudo_access_enabled:
                return ActionResult(False, "persistent Sudo Access is disabled in Assistant Settings")
            password = sudo_approval.retrieve()
        path, count = local_files.edit_file(
            args.get("path"), args.get("old_text"), args.get("new_text"), config,
            expected_replacements=args.get("expected_replacements", 1),
            privileged=privileged, sudo_password=password,
        )
        return ActionResult(True, f"updated and verified {path} ({count} exact replacement(s))")
    except (local_files.FileAccessError, ValueError) as exc:
        return ActionResult(False, str(exc))


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


# Wrappers between a terminal and what the user is actually running.
_WRAPPER_COMMS = {"bash", "zsh", "fish", "sh", "dash", "nu", "script", "tmux", "screen", "login",
                  "sudo", "su", "env", "xdg-terminal-exec", "uwsm-app", "setsid"}


def _process_children() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    try:
        pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return children
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            children.setdefault(int(stat[1]), []).append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return children


def _running_program(root_pid, children) -> str | None:
    """The shallowest non-wrapper process under a terminal: "claude",
    "codex", "nvim", "htop"... Claude Code titles its window "✳ <task>", so
    without this "the Claude terminal" matched nothing at all."""
    if not root_pid:
        return None
    level = list(children.get(root_pid, []))
    for _ in range(8):
        next_level = []
        for pid in sorted(level):
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                continue
            if comm and comm not in _WRAPPER_COMMS:
                return comm
            next_level.extend(children.get(pid, []))
        if not next_level:
            return None
        level = next_level
    return None


def _with_programs(clients: list[dict]) -> list[dict]:
    children = None
    for c in clients:
        if any(k in str(c.get("class") or "").lower() for k in _WINDOW_KINDS["terminal"]):
            children = children if children is not None else _process_children()
            c["running"] = _running_program(c.get("pid"), children)
    return clients


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
    clients = _with_programs(clients)
    windows = [
        {
            "address": c.get("address"),
            "app": c.get("class"),
            "title": c.get("title"),
            **({"running": c["running"]} if c.get("running") else {}),
            "workspace": (c.get("workspace") or {}).get("id"),
            "fullscreen": bool(c.get("fullscreen")),
            "focused": c.get("address") == active_address,
        }
        for c in clients
        if c.get("mapped")
    ]
    return ActionResult(True, json.dumps(windows))


def list_bar_icons(args: dict) -> ActionResult:
    from .bar import entries
    try:
        return ActionResult(True, json.dumps(entries()))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return ActionResult(False, f"Could not discover top-bar icons: {exc}")


def check_assistant_updates(args: dict) -> ActionResult:
    from ..core import updates
    result = updates.check_updates(force=True)
    return ActionResult(result["ok"], json.dumps(result))


def update_assistant(args: dict) -> ActionResult:
    from ..core import updates
    return ActionResult(*updates.request_update())


def get_update_status(args: dict) -> ActionResult:
    from ..core import updates
    return ActionResult(True, json.dumps(updates.update_status()))


def schedule_task(args: dict) -> ActionResult:
    from ..core import agenda
    try:
        job = agenda.create(args)
    except ValueError as exc:
        return ActionResult(False, f"Task NOT scheduled: {exc}")
    agenda.tick()  # a watch's first look happens now, not a heartbeat later
    return ActionResult(True, json.dumps({"scheduled": agenda.summary(job),
        "note": "Runs in the background even when no conversation is active; results arrive as desktop "
                "notifications and in the next conversation."}, ensure_ascii=False))


def list_scheduled_tasks(args: dict) -> ActionResult:
    from ..core import agenda
    return ActionResult(True, json.dumps(agenda.list_jobs(bool(args.get("include_finished"))), ensure_ascii=False))


def cancel_scheduled_task(args: dict) -> ActionResult:
    from ..core import agenda
    job = agenda.cancel(str(args.get("id") or ""))
    return ActionResult(bool(job), f"cancelled {job['id']}: {job['title']}" if job else "no task with that id")


def run_scheduled_task_now(args: dict) -> ActionResult:
    from ..core import agenda
    job = agenda._update(str(args.get("id") or ""), next_run=time.time())
    if not job or job.get("status") != "active":
        return ActionResult(False, "no active task with that id")
    agenda.tick()
    return ActionResult(True, f"started {job['id']} in the background; its result arrives as a notification "
                              "and in list_scheduled_tasks (last_result) — not verified yet")


def find_skill(args: dict) -> ActionResult:
    from ..core import skills
    request = str(args.get("request") or "").strip()
    if not request:
        return ActionResult(False, "request is required")
    picked = skills.suggest(request[:4000])
    if picked.get("skill"):
        skill = skills.load(picked["skill"])
        picked["instructions"] = skill["body"] if skill else ""
        picked["description"] = skill.get("description", "") if skill else ""
    return ActionResult(True, json.dumps(picked, ensure_ascii=False))


def load_skill(args: dict) -> ActionResult:
    from ..core import skills
    skill = skills.load(str(args.get("name") or ""))
    if not skill:
        return ActionResult(False, "no skill with that name")
    return ActionResult(True, json.dumps(skill, ensure_ascii=False))


def save_skill(args: dict) -> ActionResult:
    from ..core import skills
    try:
        skill, created = skills.save(args.get("name", ""), args.get("description", ""), args.get("instructions", ""))
    except ValueError as exc:
        return ActionResult(False, f"Skill NOT saved: {exc}")
    return ActionResult(True, f"{'created' if created else 'updated'} skill {skill['name']} "
                              f"(revision {skill.get('revision')})")


def list_skills(args: dict) -> ActionResult:
    from ..core import skills
    return ActionResult(True, json.dumps([{k: s.get(k) for k in ("name", "description", "updated", "revision")}
                                          for s in skills.roster()], ensure_ascii=False))


def delete_skill(args: dict) -> ActionResult:
    from ..core import skills
    ok = skills.delete(str(args.get("name") or ""))
    return ActionResult(ok, "deleted" if ok else "no skill with that name")


def get_release_notes(args: dict) -> ActionResult:
    from ..core import updates
    try:
        result = updates.release_notes(str(args.get("version") or "").strip() or None)
    except ValueError as exc:
        return ActionResult(False, str(exc))
    return ActionResult(result["ok"], json.dumps(result, ensure_ascii=False))


def report_issue(args: dict) -> ActionResult:
    from ..core import issues
    title = str(args.get("title") or "").strip()
    description = str(args.get("description") or "").strip()
    if not title or not description:
        return ActionResult(False, "title and description are both required")
    ok, result = issues.file_issue(title, description)
    if not ok:
        return ActionResult(False, f"Issue NOT filed: {result}")
    return ActionResult(True, f"Filed: {result}")


def close_bar_panel(args: dict) -> ActionResult:
    from .bar import close_panel
    try:
        return ActionResult(*close_panel(args.get("id", "")))
    except Exception as exc:
        return ActionResult(False, f"Could not close top-bar panel: {exc}")


def open_bar_panel(args: dict) -> ActionResult:
    from .bar import open_panel
    try:
        return ActionResult(*open_panel(args.get("id", "")))
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        return ActionResult(False, f"Could not open top-bar panel: {exc}")


def list_commands(args: dict) -> ActionResult:
    from .keybindings import list_commands as _list_commands

    results = _list_commands(args.get("query"))
    return ActionResult(True, json.dumps(results))


def execute_command(args: dict) -> ActionResult:
    from .keybindings import execute_command as _execute_command

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
    # A trailing newline would submit implicitly; submission is press_key's job.
    text = text.rstrip("\r\n")
    if not text:
        return ActionResult(False, "no text given")
    if "\n" in text:
        return _paste_text(text)
    return _run(["wtype", "--", text])


_WTYPE_MODIFIERS = {"shift", "ctrl", "alt", "logo", "super", "win", "altgr", "capslock"}


def press_key(args: dict) -> ActionResult:
    key = args.get("key")
    if not key:
        return ActionResult(False, "no key given")
    if shutil.which("wtype") is None:
        return ActionResult(False, "wtype is not installed")

    modifiers = args.get("modifiers") or []
    if isinstance(modifiers, str):
        modifiers = [modifiers]
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
    return _run(argv)


def submit_sudo_password(_args: dict) -> ActionResult:
    """Type the user-enabled keyring password into the focused sudo prompt."""
    if shutil.which("wtype") is None:
        return ActionResult(False, "wtype is not installed")
    if not load_config().sudo_access_enabled:
        return ActionResult(False, "persistent Sudo Access is disabled in Assistant Settings")
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


def remember_preference(args: dict) -> ActionResult:
    text = (args.get("preference") or "").strip()
    if not text:
        return ActionResult(False, "no preference given")
    from ..core.memory import add_preference

    add_preference(text)
    return ActionResult(True, "remembered")


def describe_screen(args: dict) -> ActionResult:
    result = _describe_screen_raw(args)
    hint = _terminal_text_hint()
    return ActionResult(result.ok, result.message + hint) if hint else result


def _terminal_text_hint() -> str:
    """2026-09-24 01:16-01:20: ~40 screenshots of an ssh terminal instead of
    its log; a vision summary drops lines, and a docker-compose was then
    written from those summaries. When the focused window is a terminal
    with a readable log, say so in the result she is reading."""
    from . import tile_logs
    focused = next((c for c in tile_logs._clients() if c.get("focusHistoryID") == 0), {})
    address = focused.get("address")
    if not address or focused.get("class") not in tile_logs._TERMINAL_CLASSES or not tile_logs.find_log(address):
        return ""
    return (f"\n[The focused window is a terminal with a readable log: for its exact text use "
            f"read_tile_log window='{address}', not screenshots.]")


def _describe_screen_raw(args: dict) -> ActionResult:
    from ..config import load_config
    from .vision import describe_screen as _describe_screen

    question = args.get("question") or "Briefly describe what's on the screen."
    cfg = load_config()
    if _gateway_key_configured(cfg):
        from .vision import _capture, inspect_gateway_image
        path = _capture()
        if path is None:
            return ActionResult(False, "Screenshot capture failed; screen contents unverified.")
        text = inspect_gateway_image(path, question, cfg)
        return ActionResult(not text.startswith("error:"), text)
    text = _describe_screen(
        question, cfg.api_key_path, cfg.responses_model, cfg.responses_reasoning_effort
    )
    if text.startswith("error:"):
        return ActionResult(False, text)
    return ActionResult(True, text)


def _gateway_key_configured(cfg) -> bool:
    gateway_key_path = getattr(cfg, "vercel_gateway_api_key_path", "")
    if not gateway_key_path:
        return False
    try:
        return bool(Path(gateway_key_path).expanduser().read_text().strip())
    except OSError:
        return False


def _matching_windows(targets, clients: list[dict]) -> list[dict]:
    """Same fuzzy app/title substring match focus_window uses, applied per
    target in a list. First match wins per target; already-matched windows
    are skipped so two vague targets can't both resolve to the same window."""
    chosen: list[dict] = []
    seen_addresses: set = set()
    for target in targets:
        needle = str(target or "").strip().lower()
        if not needle:
            continue
        match = next(
            (
                c for c in clients
                if c.get("address") not in seen_addresses
                and (needle in (c.get("class") or "").lower() or needle in (c.get("title") or "").lower())
            ),
            None,
        )
        if match is not None:
            chosen.append(match)
            seen_addresses.add(match.get("address"))
    return chosen


def show_window_labels(args: dict) -> ActionResult:
    """Floating name-label badges pinned over each candidate window's real
    on-screen rectangle — the visual "which window do you mean?" aid,
    rendered by the omarchy-ai.window-labels Quickshell plugin (same IPC
    pattern as omarchy-osd). Geometry comes straight from hyprctl clients -j
    (same shape list_windows already parses), so the badges track this
    machine's actual current window layout rather than a guess."""
    targets = args.get("targets")
    if targets is not None and not isinstance(targets, list):
        return ActionResult(False, "targets must be a list of strings or omitted")

    r = _run(["hyprctl", "clients", "-j"])
    if not r.ok:
        return r
    try:
        clients = json.loads(r.message)
    except json.JSONDecodeError:
        return ActionResult(False, "could not parse window list")
    mapped = [c for c in clients if c.get("mapped")]

    chosen = _matching_windows(targets, mapped) if targets else mapped
    if not chosen:
        return ActionResult(False, "no matching windows found")

    windows = []
    for c in chosen:
        at = c.get("at") or [0, 0]
        size = c.get("size") or [0, 0]
        label = c.get("title") or c.get("class") or "window"
        # Long titles would draw a chip wider than the window it's labeling
        # — the plugin elides overflow but has no cap on the chip's own
        # width, so keep it from growing unbounded in the first place.
        if len(label) > 40:
            label = label[:39].rstrip() + "…"
        windows.append({"x": at[0], "y": at[1], "width": size[0], "height": size[1], "label": label})

    payload = json.dumps({"windows": windows})
    r2 = _run(["omarchy-shell", "-q", "windowLabels", "show", payload])
    if not r2.ok:
        return r2
    return ActionResult(True, f"labeled {len(windows)} window(s)")


def hide_window_labels(args: dict) -> ActionResult:
    return _run(["omarchy-shell", "-q", "windowLabels", "hide"])


# Generic words people use for a kind of window, matched against app class.
_WINDOW_KINDS = {
    "terminal": ("foot", "kitty", "alacritty", "ghostty", "wezterm", "konsole", "terminal"),
    "browser": ("chromium", "chrome", "firefox", "brave", "zen", "vivaldi"),
    "editor": ("code", "nvim", "neovide", "zed", "gedit", "kate", "sublime"),
    "files": ("nautilus", "thunar", "dolphin", "files"),
}
_THIS_WINDOW = {"focused", "current", "this", "active", "focused window", "current window",
                "focused terminal", "current terminal", "this terminal", "the focused terminal"}


def _rank_windows(clients: list[dict], needle: str, active_address, active_workspace):
    """Matching windows, best first.

    Real bug (2026-09-22): the first hyprctl client whose class/title
    contained "terminal" won. On workspace 5 that was a terminal on workspace
    1 (its title happened to contain "Terminal"), so every "type in the
    terminal" jumped the user to workspace 1, twice in a row. Now the
    focused window wins, then windows on the current workspace, then the
    most recently used; generic words ("terminal") match by app kind."""
    kinds = next((names for word, names in _WINDOW_KINDS.items()
                  if needle in (word, "the " + word, word + "s")), None)
    matches = []
    for c in clients:
        if not c.get("mapped"):
            continue
        app, title = (c.get("class") or "").lower(), (c.get("title") or "").lower()
        running = (c.get("running") or "").lower()
        if kinds is not None:
            hit = any(k in app for k in kinds)
        else:
            words = [w for w in needle.replace("the ", " ").replace(" terminal", " ").replace(" window", " ").split() if w]
            hit = needle in app or needle in title or bool(running and any(w == running for w in words))
        if hit:
            matches.append(c)
    def key(c):
        return (c.get("address") != active_address,
                (c.get("workspace") or {}).get("id") != active_workspace,
                c.get("focusHistoryID", 1_000))
    return sorted(matches, key=key)


def terminal_task(args: dict) -> ActionResult:
    """Run a command in the assistant's own terminal (tmux): visible on the
    user's screen when she is the only operator, in the background while the
    user is working. Nothing is typed through the user's keyboard."""
    from . import operator, workbench
    command = str(args.get("command") or "").strip()
    if not command:
        return ActionResult(False, "command is required")
    try:
        name = workbench.slug(args.get("name") or command.split()[0])
    except ValueError as exc:
        return ActionResult(False, str(exc))
    started = workbench.start(name)
    if not started.ok:
        return started
    show = args.get("show", "auto")
    on_screen = operator.visible(show)
    if on_screen:
        shown = workbench.show(name)
        if not shown.ok:
            return shown
    elif show in (None, "", "auto"):
        workbench.pending_handover.add(name)
    typed = workbench.send(name, command)
    if not typed.ok:
        return typed
    where = ("on the user's screen" if on_screen else
             "in the background because the user is working (it moves to their screen when they stop "
             "touching it for ~30s, or say terminal_show)")
    return ActionResult(True, f"started {command!r} in assistant terminal {name!r}, {where}. Output is "
                              "NOT verified yet: use terminal_read, or schedule_task watch with "
                              f"terminal={name!r} to be told when it finishes.")


def terminal_read(args: dict) -> ActionResult:
    from . import workbench
    return workbench.read(str(args.get("name") or ""), int(args.get("lines") or 120))


def terminal_type(args: dict) -> ActionResult:
    from . import workbench
    return workbench.send(str(args.get("name") or ""), str(args.get("text") or ""), bool(args.get("enter", True)))


def terminal_show(args: dict) -> ActionResult:
    from . import workbench
    name = str(args.get("name") or "")
    workbench.pending_handover.discard(name)
    return workbench.show(name)


def terminal_hide(args: dict) -> ActionResult:
    from . import workbench
    return workbench.hide(str(args.get("name") or ""))


def terminal_close(args: dict) -> ActionResult:
    from . import workbench
    workbench.pending_handover.discard(str(args.get("name") or ""))
    return workbench.stop(str(args.get("name") or ""))


def terminal_sudo(args: dict) -> ActionResult:
    from . import workbench
    if not load_config().sudo_access_enabled:
        return ActionResult(False, "persistent Sudo Access is disabled in Assistant Settings")
    name = str(args.get("name") or "")
    if not workbench.exists(name):
        # 2026-09-24 00:28: called twice on the user's own terminal, failed,
        # and she told the user to type the password themselves.
        return ActionResult(False, f"{name!r} is not an assistant terminal from terminal_task. For a regular "
                            "terminal window: focus_window it, read_tile_log to confirm the password prompt, "
                            "then call submit_sudo_password.")
    password = sudo_approval.retrieve()
    if password is None:
        return ActionResult(False, "no sudo password is saved in GNOME Keyring; add it in Assistant Settings")
    return workbench.submit_password(name, password)


def run_mission(args: dict) -> ActionResult:
    # Executed inside the live session (it narrates through it); see
    # GeminiLiveSession._run_mission. Other providers cannot run it.
    return ActionResult(False, "run_mission needs a live Gemini session")


def move_window_to_workspace(args: dict) -> ActionResult:
    """One Hyprland dispatch, then verified -- no command search, no model
    round trip. Real session (2026-09-23 00:11): "move the window to
    workspace 4" went list_windows -> list_commands("move window") -> the
    SUPER+LEFT MOUSE drag binding at p=0.99, and nothing happened."""
    try:
        number = int(args.get("number"))
    except (TypeError, ValueError):
        return ActionResult(False, "number must be a workspace number")
    if not 1 <= number <= 99:
        return ActionResult(False, "workspace number must be 1-99")
    follow = bool(args.get("follow", False))
    target = str(args.get("target") or "focused").strip()
    r = _run(["hyprctl", "clients", "-j"])
    active = _run(["hyprctl", "activewindow", "-j"])
    workspace = _run(["hyprctl", "activeworkspace", "-j"])
    try:
        clients = _with_programs(json.loads(r.message)) if r.ok else []
        active_address = json.loads(active.message).get("address") if active.ok else None
        active_workspace = json.loads(workspace.message).get("id") if workspace.ok else None
    except (ValueError, AttributeError):
        return ActionResult(False, "could not read windows")
    if target.startswith("0x"):
        address = target
    elif target.lower() in _THIS_WINDOW:
        address = active_address
    else:
        ranked = _rank_windows(clients, target.lower(), active_address, active_workspace) or \
            _jev_window(clients, target, active_workspace)
        address = ranked[0]["address"] if ranked else None
    if not address:
        return ActionResult(False, f"no window matching '{target}' found")
    result = _hyprctl_dispatch(
        f'hl.dsp.window.move({{ workspace = {number}, window = "address:{address}", follow = {"true" if follow else "false"} }})',
        [("movetoworkspace" if follow else "movetoworkspacesilent"), f"{number},address:{address}"])
    if not result.ok:
        return result
    for _ in range(10):
        after = _run(["hyprctl", "clients", "-j"])
        try:
            moved = next((c for c in json.loads(after.message) if c.get("address") == address), None)
        except ValueError:
            moved = None
        if moved and (moved.get("workspace") or {}).get("id") == number:
            title = str(moved.get("title") or moved.get("class") or address)[:60]
            return ActionResult(True, f"verified: {title!r} is now on workspace {number}"
                                      + ("; view followed it" if follow else "; view stayed where it was"))
        time.sleep(0.03)
    return ActionResult(False, f"move dispatched but window {address} is not verified on workspace {number}")


def _jev_window(clients: list[dict], target: str, active_workspace) -> list[dict]:
    """Semantic fallback when no window matches by name ("the one with the
    drone project", "the Codex window"): one Jev Choice over the mapped
    windows. Only a clear pick counts; otherwise report no match rather than
    focus a guess."""
    mapped = [c for c in clients if c.get("mapped")][:100]
    if not mapped:
        return []
    try:
        from ..core.jev import Jev, JevError, choice
        criteria = {"none": "No listed window is the one meant."}
        for i, c in enumerate(mapped):
            criteria[str(i)] = json.dumps({"app": c.get("class"), "title": c.get("title"),
                                           "running": c.get("running"),
                                           "workspace": (c.get("workspace") or {}).get("id"),
                                           "on_current_workspace": (c.get("workspace") or {}).get("id") == active_workspace},
                                          ensure_ascii=False)
        answer = Jev().ask({"requested_window": target}, {"which": choice(
            "Which listed window is `requested_window`? Choose none if no window clearly fits.", criteria)},
            timeout=4, retries=1)["which"]
    except (JevError, ValueError) as exc:
        log.info("Jev window resolution unavailable: %s", str(exc)[:120])
        return []
    # Real probe: "my shell in the tello folder" -> the right window at 0.68
    # (none 0.28); "the terminal with the drone project" split 0.39/0.26
    # between two Tello windows. A clear lead is required, not a high bar.
    runner_up = sorted(answer["probabilities"].values(), reverse=True)[1]
    if answer["choice"] == "none" or answer["p"] < 0.6 or answer["p"] < 2 * runner_up:
        return []
    return [mapped[int(answer["choice"])]]


def focus_window(args: dict) -> ActionResult:
    target = (args.get("target") or "").strip()
    if not target:
        return ActionResult(False, "no window specified")
    address = target
    note = ""
    if not target.startswith("0x"):
        r = _run(["hyprctl", "clients", "-j"])
        if not r.ok:
            return r
        try:
            clients = json.loads(r.message)
        except json.JSONDecodeError:
            return ActionResult(False, "could not parse window list")
        active_address = active_workspace = None
        active = _run(["hyprctl", "activewindow", "-j"])
        workspace = _run(["hyprctl", "activeworkspace", "-j"])
        try:
            active_address = json.loads(active.message).get("address") if active.ok else None
            active_workspace = json.loads(workspace.message).get("id") if workspace.ok else None
        except (ValueError, AttributeError):
            pass
        needle = target.lower()
        if needle in _THIS_WINDOW and active_address:
            address = active_address
            match = next((c for c in clients if c.get("address") == address), {})
        else:
            clients = _with_programs(clients)
            ranked = _rank_windows(clients, needle, active_address, active_workspace)
            if not ranked:
                ranked = _jev_window(clients, target, active_workspace)
            if not ranked:
                return ActionResult(False, f"no window matching '{target}' found")
            match = ranked[0]
            address = match["address"]
        workspace_id = (match.get("workspace") or {}).get("id")
        if active_workspace is not None and workspace_id not in (None, active_workspace):
            note = (f" -- NOTE: it is on workspace {workspace_id}, so the view moved there from workspace "
                    f"{active_workspace}")
    result = _hyprctl_dispatch(
        f'hl.dsp.focus({{ window = "address:{address}" }})',
        ["focuswindow", f"address:{address}"],
    )
    if not result.ok:
        return result
    for _ in range(5):
        active = _run(["hyprctl", "activewindow", "-j"])
        try:
            if active.ok and json.loads(active.message).get("address") == address:
                return ActionResult(True, f"verified focus on {address}{note}")
        except (ValueError, AttributeError):
            pass
        time.sleep(0.04)
    return ActionResult(False, f"focus dispatch returned but {address} is not verified active")



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


# --- Android TV casting -------------------------------------------------

def _port_listening(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _looks_like_address(s: str) -> bool:
    """True if s (optionally with a :port suffix) is a literal IP -- lets
    the model/user pass a raw address directly rather than only a
    discovered device name."""
    host = s.split(":")[0]
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _discover_androidtv_devices() -> list[dict]:
    """Only used by the new-TV pairing flow below (_reachable_target_for_
    install/install_receiver_on_tv), which is a distinct feature from
    casting-target resolution -- that path now reads display/registry.py
    instead, see _resolve_cast_target/list_cast_targets."""
    from ..display import discovery

    try:
        return discovery.discover_androidtv_devices()
    except Exception:  # noqa: BLE001 -- discovery must never take casting down
        log.exception("TV discovery failed")
        return []


def _resolve_cast_target(target: str | None) -> tuple[str | None, ActionResult | None]:
    """Resolves a spoken/typed target -- a name from list_cast_targets, a
    raw IP[:port], or nothing -- to a single adb address ("host:port").

    Reads display/registry.py -- the same shared device state the
    omarchy-ai.tv-discovery overlay renders -- rather than calling mDNS
    discovery directly, so a voice-resolved target and whatever the
    overlay is showing can never drift apart. A named target that doesn't
    match anything real is reported back as a failure rather than
    silently guessed at. Returns (address, None) on success or
    (None, ActionResult) with a message for the model to relay/act on
    (ambiguous match, no match, or multiple candidates needing a pick)."""
    from ..display import registry

    devices = registry.get_or_refresh()

    if target:
        needle = target.strip().lower()
        matches = [d for d in devices if needle in d["name"].lower()]
        if len(matches) == 1:
            return f"{matches[0]['address']}:5555", None
        if len(matches) > 1:
            names = ", ".join(d["name"] for d in matches)
            return None, ActionResult(
                False,
                f"'{target}' matches more than one TV on the network ({names}) "
                "-- ask which exact one, then call start_casting again with that name.",
            )
        if _looks_like_address(target):
            host = target.split(":")[0]
            port = target.split(":")[1] if ":" in target else "5555"
            return f"{host}:{port}", None
        if devices:
            names = ", ".join(d["name"] for d in devices)
            return None, ActionResult(
                False, f"no TV named '{target}' found on the network right now. Available: {names}"
            )
        return None, ActionResult(
            False, f"no TV named '{target}' found, and no TVs are currently discoverable on the network"
        )

    # Only ever auto-pick among devices actually seen live -- "unknown"
    # (the never-confirmed fallback entry) and "offline" ones don't count
    # toward an unambiguous single choice, they're just kept visible.
    live = [d for d in devices if d["status"] in ("online", "connecting", "connected")]
    if len(live) == 1:
        return f"{live[0]['address']}:5555", None
    if len(live) > 1:
        names = ", ".join(d["name"] for d in live)
        return None, ActionResult(
            False,
            f"found {len(live)} TVs on the network ({names}) -- call list_cast_targets "
            "and ask the user which one, then call start_casting again with that name as the target.",
        )

    # Nothing currently live -- fall back to the one TV already confirmed
    # working before (the "unknown"-status registry entry, see
    # registry.py) rather than failing the moment mDNS has a bad day or a
    # TV's remote-control service happens to be off.
    from ..display import discovery

    log.info("no TVs currently online; falling back to last-known TV %s", discovery.TV_ADB_ADDR)
    return discovery.TV_ADB_ADDR, None


def list_cast_targets(args: dict) -> ActionResult:
    """The shared device registry (display/registry.py) -- what the model
    reads out (or uses to disambiguate) when asked "what TVs are
    available" or when start_casting comes back ambiguous. Same data the
    omarchy-ai.tv-discovery overlay renders, refreshed here if stale."""
    from ..display import registry

    return ActionResult(True, json.dumps(registry.get_or_refresh()))


def start_casting(args: dict) -> ActionResult:
    from ..display import session
    try:
        with session.locked():
            return _start_casting(args)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        log.exception("casting operation failed")
        return ActionResult(False, f"casting failed: {exc}")


def _start_casting(args: dict) -> ActionResult:
    from ..display import registry, tv_overlay, session

    tv_overlay.show_and_track()
    tv_addr, err = _resolve_cast_target((args.get("target") or "").strip() or None)
    if err is not None:
        return err
    host = tv_addr.split(":")[0]
    previous = session.target()
    if session.active() and previous == host and session.read_state().get("state") == "connected":
        registry.mark_connected(host)
        tv_overlay.hide_tracking()
        return ActionResult(True, f"already casting to {tv_addr}")

    def failed(message):
        registry.mark_failed(host)
        tv_overlay.push_update()
        tv_overlay.hide_tracking()
        log.error("cast to %s failed: %s", host, message)
        return ActionResult(False, message)

    registry.mark_connecting(host)
    tv_overlay.push_update()
    r = _run(["adb", "connect", tv_addr], timeout=8)
    if not r.ok or "connected" not in r.message.lower():
        return failed(f"could not reach {tv_addr}: {r.message}")
    r = _run(["adb", "-s", tv_addr, "get-state"], timeout=5)
    if not r.ok or r.message.strip() != "device":
        return failed(f"receiver ADB is not ready: {r.message}")

    # Validate the new target before interrupting an existing mirror.
    if previous:
        session.stop()
        registry.mark_disconnected(previous)
    r = _install_or_update_receiver(tv_addr)
    if not r.ok:
        return failed(r.message)

    if not _port_listening(_SIGNALING_PORT):
        subprocess.run([
            "systemd-run", "--user", "--collect", "--unit=omarchy-ai-signaling.service",
            f"--working-directory={_REPO_ROOT}", "--property=Restart=on-failure",
            "--property=RestartSec=2", "--", _VENV_PYTHON, "-m", "omarchy_ai.display.signaling",
        ], check=True, capture_output=True, text=True, timeout=10)
        for _ in range(20):
            if _port_listening(_SIGNALING_PORT):
                break
            time.sleep(.25)
        else:
            return failed("signaling server failed to start")

    # Explicitly reconnect a warm Activity as well as a cold launch, using
    # the desktop address routed to this TV rather than a baked-in IP.
    r = _run(["adb", "-s", tv_addr, "shell", "am", "start", "-W", "-n",
              "ai.omarchy.receiver/.MainActivity", "--es", "signaling_host",
              session.local_address(host)], timeout=12)
    if not r.ok or "Error:" in r.message or "Exception" in r.message:
        return failed(f"could not launch receiver: {r.message}")
    try:
        session.start([_VENV_PYTHON, "-u", str(_REPO_ROOT / "scripts/spike_cast_sender.py")],
                      str(_REPO_ROOT), host)
        connected, detail = session.wait_connected()
        if not connected:
            session.stop()
            return failed(f"casting failed: {detail}")
    except (OSError, RuntimeError, subprocess.SubprocessError):
        session.stop()
        registry.mark_failed(host)
        tv_overlay.hide_tracking()
        raise
    registry.mark_connected(host)
    tv_overlay.select(host)
    time.sleep(.8)
    tv_overlay.hide_tracking()
    return ActionResult(True, f"casting connected to the TV at {tv_addr}")


def stop_casting(args: dict) -> ActionResult:
    from ..display import registry, tv_overlay, session
    try:
        with session.locked():
            host = session.target()
            session.stop()
            if host:
                registry.mark_disconnected(host)
            tv_overlay.hide_tracking()
            return ActionResult(True, "casting stopped")
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        return ActionResult(False, f"could not stop casting: {exc}")


def _source_fingerprint() -> str:
    """Hashes every file under android-receiver/app/src plus build.gradle.kts
    -- what _ensure_receiver_apk_built compares against the last successful
    build's marker to decide "stale, needs a rebuild" vs. "already current".
    Deliberately content-based, not git-HEAD-based: this machine builds
    locally (see build.gradle.kts's own versionName comment), so a real,
    live case is uncommitted source changes that HEAD wouldn't move for
    at all -- and that's exactly the state this repo is in right now."""
    h = hashlib.sha256()
    src_dir = _REPO_ROOT / "android-receiver" / "app" / "src"
    build_file = _REPO_ROOT / "android-receiver" / "app" / "build.gradle.kts"
    if src_dir.is_dir():
        for p in sorted(src_dir.rglob("*")):
            if p.is_file():
                h.update(str(p.relative_to(_REPO_ROOT)).encode())
                h.update(p.read_bytes())
    if build_file.exists():
        h.update(build_file.read_bytes())
    return h.hexdigest()


def _aapt_path() -> str | None:
    sdk_root = Path(os.environ.get("ANDROID_SDK_ROOT") or (Path.home() / "Android" / "Sdk"))
    build_tools = sdk_root / "build-tools"
    if not build_tools.is_dir():
        return None
    for d in sorted(build_tools.iterdir(), reverse=True):
        aapt = d / "aapt"
        if aapt.exists():
            return str(aapt)
    return None


def _local_apk_version() -> str | None:
    """versionName baked into the locally built APK -- what a TV's install
    should match once it's current."""
    aapt = _aapt_path()
    if not aapt or not _APK_PATH.exists():
        return None
    r = _run([aapt, "dump", "badging", str(_APK_PATH)], timeout=15)
    if not r.ok:
        return None
    m = re.search(r"versionName='([^']+)'", r.message)
    return m.group(1) if m else None


def _installed_receiver_version(adb_addr: str) -> str | None:
    """versionName currently installed on a given TV, read live via
    dumpsys -- never cached, since the whole point is catching drift."""
    r = _run(["adb", "-s", adb_addr, "shell", "dumpsys", "package", "ai.omarchy.receiver"], timeout=10)
    if not r.ok:
        return None
    m = re.search(r"versionName=(\S+)", r.message)
    return m.group(1) if m else None


def _ensure_receiver_apk_built() -> ActionResult:
    fingerprint = _source_fingerprint()
    marker = _APK_BUILD_MARKER.read_text().strip() if _APK_BUILD_MARKER.exists() else None
    if _APK_PATH.exists() and fingerprint and marker == fingerprint:
        return ActionResult(True, f"receiver APK already built and current ({_APK_PATH.stat().st_size} bytes)")
    gradlew = _REPO_ROOT / "android-receiver" / "gradlew"
    if not gradlew.exists():
        if _APK_PATH.exists():
            return ActionResult(
                True,
                f"receiver APK exists but android-receiver/gradlew is missing -- "
                f"can't confirm it's current ({_APK_PATH.stat().st_size} bytes)",
            )
        return ActionResult(False, "android-receiver/gradlew not found -- can't build the receiver app")
    log.info(
        "receiver APK %s; building via ./gradlew assembleDebug",
        "missing" if not _APK_PATH.exists() else "stale (source changed since last build)",
    )
    # cwd matters here, confirmed live: gradlew invoked by absolute path
    # with no cwd fails with "Directory '...' does not contain a Gradle
    # build" (it doesn't self-locate its project root from $0) -- a
    # latent bug in this function since before today's changes, just
    # never exercised because the APK already existed every time this
    # ran before now.
    build = [str(gradlew), "assembleDebug"]
    if shutil.which("mise"):
        build = ["mise", "exec", "--", *build]
    r = _run(build, timeout=300, cwd=str(gradlew.parent))
    if not r.ok or not _APK_PATH.exists():
        return ActionResult(False, f"building the receiver APK failed: {r.message}")
    if fingerprint:
        _APK_BUILD_MARKER.write_text(fingerprint)
    return ActionResult(True, "built the receiver APK")


def _install_or_update_receiver(adb_addr: str) -> ActionResult:
    """Reinstalls the receiver on an already-known, already-reachable TV
    if (and only if) its installed version doesn't match the local build
    -- the real "just update it" path, with none of install_receiver_on_tv's
    brand-new-device pairing narration (there's nothing to pair, it's
    already paired)."""
    apk = _ensure_receiver_apk_built()
    if not apk.ok:
        return apk
    local_version = _local_apk_version()
    installed_version = _installed_receiver_version(adb_addr)
    if local_version and installed_version and local_version == installed_version:
        return ActionResult(True, f"receiver on {adb_addr} is already up to date (v{installed_version})")
    r = _run(["adb", "-s", adb_addr, "install", "-r", str(_APK_PATH)], timeout=120)
    if not r.ok:
        return ActionResult(
            False, f"found the receiver already set up on {adb_addr}, but reinstalling it failed: {r.message}"
        )
    new_version = local_version or "unknown"
    was = f"v{installed_version}" if installed_version else "an earlier build"
    return ActionResult(
        True,
        f"updated the receiver on {adb_addr} from {was} to v{new_version} "
        "(it was already paired, so no Developer-options setup was needed)",
    )


def _ensure_receiver_current(adb_addr: str) -> None:
    """Best-effort version check/update run before every cast starts, so a
    cast never silently runs against a stale receiver build. Never blocks
    or fails casting on its own account -- same fire-and-forget policy as
    _discover_androidtv_devices. The common case (already up to date) stays
    fast: a source-fingerprint hash plus one dumpsys read; the rebuild/
    reinstall cost is only paid when something's actually stale."""
    try:
        r = _install_or_update_receiver(adb_addr)
        if not r.ok:
            log.warning("receiver version check/update on %s: %s", adb_addr, r.message)
        else:
            log.info("receiver version check on %s: %s", adb_addr, r.message)
    except Exception:  # noqa: BLE001 -- cosmetic w.r.t. casting itself
        log.exception("receiver version check/update failed")


def _reachable_target_for_install(target: str | None) -> str | None:
    """Returns an adb "host:port" address if `target` (or, when omitted,
    the single currently-discovered TV) names a device this project
    already knows about AND is reachable over adb right now -- the real
    signal that this is a reinstall/update on an already-paired TV, not a
    brand-new one that still needs the Developer-options dance. Returns
    None for anything else, including "ambiguous, multiple devices" or
    "named but not currently discoverable" -- those fall through to the
    existing pairing flow below rather than being resolved here.

    This is the direct fix for a real, live bug: asking to "install the
    newer receiver" on Living Room TV -- a TV that's been paired and
    working for a while -- produced "here's how to open Developer
    options" instructions, which made no sense for a device already set
    up. See STATUS.md's receiver-version-tracking entry."""
    devices = _discover_androidtv_devices()
    addr = None
    if target:
        needle = target.strip().lower()
        matches = [d for d in devices if needle in d["name"].lower()]
        if len(matches) == 1:
            addr = f"{matches[0]['address']}:5555"
        elif _looks_like_address(target):
            host = target.split(":")[0]
            port = target.split(":")[1] if ":" in target else "5555"
            addr = f"{host}:{port}"
        else:
            return None
    elif len(devices) == 1:
        addr = f"{devices[0]['address']}:5555"
    else:
        return None

    r = _run(["adb", "connect", addr], timeout=8)
    if r.ok and "connected" in r.message.lower():
        return addr
    return None


def install_receiver_on_tv(args: dict) -> ActionResult:
    """Guided new-TV setup, real step by step -- OR, when `target` names
    (or there's no ambiguity about) a TV that's already paired and
    adb-reachable, a plain reinstall/update with none of the pairing
    narration (see _reachable_target_for_install).

    Hard, real Android constraint (checked live on this network -- not
    worked around, because it can't be): a brand-new, never-paired TV
    cannot be discovered or reached over adb at all until the user
    personally turns on Developer options + Wireless debugging *on that
    TV's own screen*. There is no scriptable path around that first step.

    This tool does everything that CAN be automated -- building the APK if
    needed, discovering the TV's pairing/connect mDNS services once they
    appear, running `adb pair`/`adb connect`/`adb install` -- and is
    explicit, in its return messages, about the one piece that can't be:
    reading a pairing code off the TV's screen. It's called once with no
    `pairing_code` to get the narrated setup steps (and, once the TV is
    mid-pairing, its address); called again with `pairing_code` once the
    user has read it out to finish pairing, connect, and install.
    """
    global _pending_pair_target

    pairing_code = (args.get("pairing_code") or "").strip() or None
    target = (args.get("target") or "").strip() or None

    if pairing_code is None:
        already_paired_addr = _reachable_target_for_install(target)
        if already_paired_addr:
            return _install_or_update_receiver(already_paired_addr)

    apk = _ensure_receiver_apk_built()
    if not apk.ok:
        return apk

    from ..display import discovery

    if pairing_code is None:
        try:
            pairing = discovery.discover_adb_tls_pairing()
        except Exception:  # noqa: BLE001
            log.exception("adb-tls-pairing discovery failed")
            pairing = []

        if not pairing:
            _pending_pair_target = None
            return ActionResult(
                True,
                "No TV is currently broadcasting a pairing signal. Walk the user "
                "through these steps on the TV itself, one at a time, waiting for "
                "them to confirm each: open Settings, go to About (or Device "
                "Preferences) and select the build/version entry, then click/select "
                "it repeatedly (about 7 times) until it says Developer options is "
                "unlocked; go back to the main Settings screen, open Developer "
                "options, and turn on 'Wireless debugging'; then open 'Wireless "
                "debugging' and select 'Pair device with pairing code'. Once that "
                "screen is showing a 6-digit code, call this tool again (still no "
                "pairing_code) to find it.",
            )
        if len(pairing) > 1:
            names = ", ".join(p["name"] for p in pairing)
            _pending_pair_target = None
            return ActionResult(
                False,
                f"more than one TV is showing a pairing screen right now ({names}) "
                "-- ask which one to set up and pair them one at a time.",
            )

        target = pairing[0]
        _pending_pair_target = target
        return ActionResult(
            True,
            f"Found '{target['name']}' at {target['address']} ready to pair. Ask "
            "the user to read the pairing code shown on the TV screen out loud "
            "(or type it), then call this tool again with that code as pairing_code.",
        )

    # pairing_code given: use the target found by the discovery step above,
    # re-discovering if it's missing (e.g. a fresh conversation/process, or
    # the model skipped straight to a code it already had).
    if _pending_pair_target is None:
        try:
            pairing = discovery.discover_adb_tls_pairing()
        except Exception:  # noqa: BLE001
            log.exception("adb-tls-pairing discovery failed")
            pairing = []
        if len(pairing) != 1:
            return ActionResult(
                False,
                "no single TV is currently showing a pairing screen -- ask the user "
                "to reopen Developer options > Wireless debugging > Pair device "
                "with pairing code on the TV, then retry.",
            )
        _pending_pair_target = pairing[0]

    pair_target = _pending_pair_target
    _pending_pair_target = None
    pair_addr = f"{pair_target['address']}:{pair_target['port']}"

    r = _run(["adb", "pair", pair_addr, pairing_code], timeout=15)
    if not r.ok:
        return ActionResult(
            False,
            f"pairing failed ({r.message}). Ask the user to double check the code, "
            "or reopen the pairing screen on the TV for a fresh one and try again.",
        )

    # Wireless Debugging's general connect port is a separate, randomly
    # assigned port from the one just used for pairing -- discovered via a
    # different mDNS service (_adb-tls-connect._tcp), not the fixed 5555
    # this project's already-paired TV happens to use (that one was set up
    # via the older adb tcpip 5555 method, not Wireless Debugging).
    try:
        connect_candidates = discovery.discover_adb_tls_connect()
    except Exception:  # noqa: BLE001
        log.exception("adb-tls-connect discovery failed")
        connect_candidates = []
    if not connect_candidates:
        return ActionResult(
            False,
            "paired successfully, but no TV is currently advertising a connect "
            "address -- ask the user to confirm Wireless debugging is still on, "
            "then call this tool again with the same pairing_code.",
        )
    connect = connect_candidates[0]
    connect_addr = f"{connect['address']}:{connect['port']}"

    r = _run(["adb", "connect", connect_addr], timeout=8)
    if not r.ok or "connected" not in r.message.lower():
        return ActionResult(False, f"paired, but could not adb connect to {connect_addr}")

    r = _run(["adb", "-s", connect_addr, "install", "-r", str(_APK_PATH)], timeout=120)
    if not r.ok:
        return ActionResult(False, f"connected to {connect_addr}, but installing the receiver app failed: {r.message}")

    return ActionResult(
        True,
        f"receiver app installed on '{connect['name']}' at {connect_addr}. "
        "You can now cast to it -- it should also show up under that name "
        "via list_cast_targets once its Android TV remote-control service is up.",
    )


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
    return _run(["omarchy", *argv], timeout=30)


# --- MyApi (myapiai.com) passthrough ------------------------------------
# See src/omarchy_ai/myapi/. Only added to the model's tool list at all
# when a connection actually exists (voice/live.py's build_session_config
# checks myapi.is_connected() before including these) -- they still guard
# for "not connected" themselves too, since this same ACTIONS dict is also
# reachable from the phone bridge (phone/server.py), which doesn't share
# that per-session check.
#
# myapi_call only allows GET: same "no real confirm/policy layer exists
# yet, so only Level 1 (read-only) is voice-reachable" bar every other
# action in this file holds to -- see the module docstring and
# run_omarchy_command's own allowlist just above.


def myapi_list_services(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    try:
        result = myapi.MyApiClient().list_services()
    except myapi.MyApiError as e:
        return ActionResult(False, f"couldn't reach MyApi: {e}")
    return ActionResult(True, json.dumps(result))


def myapi_service_methods(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    service = args.get("service")
    if not isinstance(service, str) or not service.strip():
        return ActionResult(False, "no service given")
    try:
        result = myapi.MyApiClient().service_methods(service.strip())
    except myapi.MyApiError as e:
        return ActionResult(False, f"couldn't reach MyApi: {e}")
    return ActionResult(True, json.dumps(result))


def myapi_call(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect it from the Omarchy AI settings panel first.")
    service = args.get("service")
    path = args.get("path")
    method = str(args.get("method") or "GET").strip().upper()
    if not isinstance(service, str) or not service.strip():
        return ActionResult(False, "no service given")
    if not isinstance(path, str) or not path.strip():
        return ActionResult(False, "no path given")
    if method != "GET":
        return ActionResult(
            False,
            f"'{method}' isn't voice-reachable through MyApi yet -- only "
            "reading (GET) is, since there's no confirm/policy layer for "
            "anything that would send, create, or delete something on the "
            "user's behalf. Tell the user you can't do that yet, don't "
            "attempt it a different way.",
        )
    query = args.get("query")
    if query is not None and not isinstance(query, dict):
        return ActionResult(False, "query must be an object")

    started = time.monotonic()
    ok = False
    try:
        result = myapi.MyApiClient().call_service(service.strip(), path.strip(), method, query=query)
        ok = True
        return ActionResult(True, json.dumps(result))
    except myapi.MyApiError as e:
        return ActionResult(False, f"MyApi call failed: {e}")
    finally:
        myapi_usage.record(
            service=service.strip(), path=path.strip(), method=method,
            ok=ok, duration_ms=(time.monotonic() - started) * 1000,
        )


def _gmail_execute(method: str, arguments: dict) -> dict:
    """Call the small, explicitly read-only Gmail execute allowlist.

    MyApi exposes these provider reads as POST /execute even though they
    don't alter a mailbox.  Keeping the method names here (rather than a
    generic POST tool) preserves the voice action boundary.
    """
    # This is MyApi's documented provider-tool endpoint, distinct from the
    # REST proxy used by myapi_call. Sending it through /proxy makes the
    # upstream receive an HTML page rather than a Gmail operation.
    return myapi.MyApiClient().request(
        "POST", "/services/gmail/execute",
        body={"method": method, "params": {"arguments": arguments}},
    )


def _walk_gmail_attachments(value: object, message_id: str | None = None) -> list[dict]:
    found: list[dict] = []
    if isinstance(value, dict):
        current_id = value.get("message_id") or value.get("messageId") or value.get("id") or message_id
        attachment_id = value.get("attachment_id") or value.get("attachmentId")
        filename = value.get("filename") or value.get("file_name") or value.get("fileName")
        if attachment_id:
            found.append({
                "message_id": current_id,
                "attachment_id": attachment_id,
                "filename": filename or "attachment",
                "mime_type": value.get("mimeType") or value.get("mime_type"),
                "size": value.get("size") or value.get("sizeEstimate"),
            })
        for child in value.values():
            found.extend(_walk_gmail_attachments(child, current_id))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_gmail_attachments(child, message_id))
    return found


def myapi_gmail_search_attachments(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect Gmail from the Omarchy AI settings panel first.")
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return ActionResult(False, "a Gmail search query is required")
    maximum = args.get("max_results", 10)
    if not isinstance(maximum, int) or not 1 <= maximum <= 50:
        return ActionResult(False, "max_results must be between 1 and 50")
    started = time.monotonic()
    ok = False
    try:
        result = _gmail_execute("GMAIL_FETCH_EMAILS", {
            "query": query.strip(), "max_results": maximum, "include_payload": True,
        })
        attachments = _walk_gmail_attachments(result)
        # Payloads can repeat the same MIME part at several nesting levels.
        unique = {(x["message_id"], x["attachment_id"]): x for x in attachments}
        ok = True
        return ActionResult(True, json.dumps({"attachments": list(unique.values())[:100]}))
    except myapi.MyApiError as exc:
        return ActionResult(False, f"Gmail search failed: {exc}")
    finally:
        myapi_usage.record(service="gmail", path="/execute:GMAIL_FETCH_EMAILS", method="POST(read)", ok=ok,
                           duration_ms=(time.monotonic() - started) * 1000)


def _find_base64_payload(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("data", "content", "content_base64", "base64", "file_data"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate:
                return candidate
        for child in value.values():
            found = _find_base64_payload(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_base64_payload(child)
            if found:
                return found
    return None


def _find_download_url(value: object) -> str | None:
    if isinstance(value, dict):
        for key in ("s3url", "download_url", "downloadUrl", "url"):
            candidate = value.get(key)
            if isinstance(candidate, str) and urllib.parse.urlparse(candidate).scheme == "https":
                return candidate
        for child in value.values():
            found = _find_download_url(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = _find_download_url(child)
            if found:
                return found
    return None


def _download_url(url: str) -> bytes:
    """Download MyApi's short-lived HTTPS attachment URL with a hard cap."""
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > 30 * 1024 * 1024:
                raise local_files.FileAccessError("that attachment is over the 30 MB download limit")
            data = response.read(30 * 1024 * 1024 + 1)
    except (OSError, ValueError) as exc:
        raise local_files.FileAccessError(f"couldn't download the Gmail attachment: {exc}") from exc
    if len(data) > 30 * 1024 * 1024:
        raise local_files.FileAccessError("that attachment is over the 30 MB download limit")
    return data


def _safe_download_name(value: object) -> str:
    name = Path(str(value or "attachment")).name
    name = re.sub(r"[^A-Za-z0-9._ -]+", "_", name).strip(". ")
    return name[:180] or "attachment"


def myapi_gmail_download_attachment(args: dict) -> ActionResult:
    if not myapi.is_connected():
        return ActionResult(False, "MyApi isn't connected -- connect Gmail from the Omarchy AI settings panel first.")
    message_id, attachment_id, filename = args.get("message_id"), args.get("attachment_id"), args.get("filename")
    if not all(isinstance(value, str) and value.strip() for value in (message_id, attachment_id, filename)):
        return ActionResult(False, "message_id, attachment_id, and filename are required")
    started = time.monotonic()
    ok = False
    try:
        result = _gmail_execute("GMAIL_GET_ATTACHMENT", {
            "message_id": message_id.strip(), "attachment_id": attachment_id.strip(), "file_name": filename.strip(),
        })
        encoded = _find_base64_payload(result)
        if encoded:
            try:
                raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            except (ValueError, TypeError) as exc:
                return ActionResult(False, f"Gmail returned invalid attachment data: {exc}")
        else:
            url = _find_download_url(result)
            if not url:
                return ActionResult(False, "Gmail returned no downloadable attachment data")
            raw = _download_url(url)
        destination = Path.home() / "Downloads" / "Omarchy_AI" / _safe_download_name(filename)
        # Preserve the earlier download rather than silently replacing it.
        if destination.exists():
            stem, suffix = destination.stem, destination.suffix
            for index in range(2, 1000):
                candidate = destination.with_name(f"{stem} ({index}){suffix}")
                if not candidate.exists():
                    destination = candidate
                    break
        saved = local_files.write_bytes(str(destination), raw, load_config())
        ok = True
        return ActionResult(True, f"downloaded {len(raw)} bytes to {saved}")
    except myapi.MyApiError as exc:
        return ActionResult(False, f"Gmail download failed: {exc}")
    except local_files.FileAccessError as exc:
        return ActionResult(False, str(exc))
    finally:
        myapi_usage.record(service="gmail", path="/execute:GMAIL_GET_ATTACHMENT", method="POST(read)", ok=ok,
                           duration_ms=(time.monotonic() - started) * 1000)


ACTIONS = {
    "check_assistant_updates": check_assistant_updates,
    "update_assistant": update_assistant,
    "get_update_status": get_update_status,
    "get_release_notes": get_release_notes,
    "schedule_task": schedule_task,
    "list_scheduled_tasks": list_scheduled_tasks,
    "cancel_scheduled_task": cancel_scheduled_task,
    "run_scheduled_task_now": run_scheduled_task_now,
    "find_skill": find_skill,
    "load_skill": load_skill,
    "save_skill": save_skill,
    "list_skills": list_skills,
    "delete_skill": delete_skill,
    "report_issue": report_issue,
    "list_bar_icons": list_bar_icons,
    "open_bar_panel": open_bar_panel,
    "close_bar_panel": close_bar_panel,
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
    "read_tile_log": read_tile_log,
    "open_browser": open_browser,
    "browser_task": browser_task,
    "open_files": open_files,
    "open_editor": open_editor,
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "workspace_switch": workspace_switch,
    "workspace_next": workspace_next,
    "workspace_prev": workspace_prev,
    "close_window": close_window,
    "window_fullscreen_toggle": window_fullscreen_toggle,
    "list_windows": list_windows,
    "focus_window": focus_window,
    "move_window_to_workspace": move_window_to_workspace,
    "run_mission": run_mission,
    "terminal_task": terminal_task,
    "terminal_read": terminal_read,
    "terminal_type": terminal_type,
    "terminal_show": terminal_show,
    "terminal_hide": terminal_hide,
    "terminal_close": terminal_close,
    "terminal_sudo": terminal_sudo,
    "show_window_labels": show_window_labels,
    "hide_window_labels": hide_window_labels,
    "describe_screen": describe_screen,
    "remember_preference": remember_preference,
    "list_commands": list_commands,
    "execute_command": execute_command,
    "type_text": type_text,
    "press_key": press_key,
    "submit_sudo_password": submit_sudo_password,
    "bluetooth_toggle": bluetooth_toggle,
    "nightlight_toggle": nightlight_toggle,
    "battery_status": battery_status,
    "media_play_pause": media_play_pause,
    "media_next": media_next,
    "media_prev": media_prev,
    "start_casting": start_casting,
    "stop_casting": stop_casting,
    "list_cast_targets": list_cast_targets,
    "install_receiver_on_tv": install_receiver_on_tv,
    "run_omarchy_command": run_omarchy_command,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders,
    "clear_reminders": clear_reminders,
    "myapi_list_services": myapi_list_services,
    "myapi_service_methods": myapi_service_methods,
    "myapi_call": myapi_call,
    "myapi_gmail_search_attachments": myapi_gmail_search_attachments,
    "myapi_gmail_download_attachment": myapi_gmail_download_attachment,
}


def desktop_task(args: dict) -> ActionResult:
    from .desktop_jev import desktop_task as execute
    return execute(args)


def search_os_knowledge(args: dict) -> ActionResult:
    from .os_knowledge import search_os_knowledge as search
    return search(args)


def _task_tool(name: str):
    def call(args: dict) -> ActionResult:
        from ..runtime import service
        return getattr(service, name)(args)
    return call


ACTIONS.update(desktop_task=desktop_task, search_os_knowledge=search_os_knowledge,
               start_task=_task_tool("start_task"), task_status=_task_tool("task_status"),
               task_respond=_task_tool("task_respond"))


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
