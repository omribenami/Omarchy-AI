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
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("omarchy_ai.execution.actions")

_TIMEOUT = 10

# --- Android TV casting (Phase 2) --------------------------------------
# actions.py lives at <repo>/src/omarchy_ai/execution/actions.py.
_REPO_ROOT = Path(__file__).resolve().parents[3]
_VENV_PYTHON = str(_REPO_ROOT / ".venv" / "bin" / "python")
# The one TV that's been manually paired and proven working (STATUS.md's
# "Hardware-dependent findings" -- adb tcpip 5555, not Wireless Debugging).
# Used only as the fallback when live mDNS discovery (display/discovery.py)
# comes back empty -- never the only path, see _resolve_cast_target.
_TV_ADB_ADDR = "192.168.1.86:5555"
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
_cast_process: subprocess.Popen | None = None
_signaling_process: subprocess.Popen | None = None


def _spawn_own_cgroup(argv: list[str], cwd: str) -> subprocess.Popen:
    """Launches argv fully decoupled from omarchy-ai.service's own cgroup,
    via a transient `systemd-run --user --scope`.

    Needed because the service's `KillMode` is the systemd default
    (`control-group`): a plain `subprocess.Popen(..., start_new_session=
    True)` still ends up as a member of the service's cgroup (confirmed
    live via /proc/<pid>/cgroup on a real cast subprocess) despite getting
    its own process group/session -- so restarting the daemon to load a
    code fix would kill an actively-streaming cast too. That's directly
    against the real requirement here: casting stops only via
    `stop_casting`, never as a side effect of anything else (a
    conversation ending -- already true, see `voice/live.py`'s hangup path
    never touching `_cast_process` -- or a daemon restart to pick up a
    fix, which wasn't true before this).

    `systemd-run --scope` execs straight into argv rather than forking a
    wrapper around it -- confirmed live: `Popen.pid` was argv's own real
    pid (matched via /proc/<pid>/cmdline), and `poll()`/`terminate()` on
    the returned Popen behaved exactly as they would on a plain Popen --
    while placing that pid in its own transient scope unit under
    user.slice instead of the service's cgroup (confirmed live via
    /proc/<pid>/cgroup showing `.../app.slice/run-p<pid>-*.scope`, not
    `.../omarchy-ai.service`). `--collect` cleans up the transient unit
    once the process exits so these don't accumulate. Falls back to a
    plain detached Popen (today's cgroup-coupled behavior) if
    `systemd-run` isn't on PATH, rather than failing casting outright over
    a decoupling nicety."""
    try:
        return subprocess.Popen(
            ["systemd-run", "--user", "--scope", "--collect", "--quiet", "--", *argv],
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except FileNotFoundError:
        log.warning("systemd-run not on PATH -- casting subprocess will stay in omarchy-ai.service's cgroup")
        return subprocess.Popen(
            argv,
            cwd=cwd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
# Set by install_receiver_on_tv's discovery step, consumed by its pairing
# step on the next call -- same "module-level state across separate tool
# calls" pattern _cast_process already uses.
_pending_pair_target: dict | None = None


@dataclass
class ActionResult:
    ok: bool
    message: str = ""


def _run(argv: list[str], timeout: float = _TIMEOUT, cwd: str | None = None) -> ActionResult:
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, check=False, cwd=cwd
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
    # Wraps the launch so this terminal's output gets tracked in a
    # per-tile log (see tile_logs.py) — read_tile_log can then answer
    # "what happened in that terminal" from real text instead of a
    # describe_screen vision call. Falls back to a plain, untracked
    # launch if tile-log setup itself fails for any reason; a terminal
    # that opens without logging is far better than one that doesn't
    # open at all.
    try:
        from . import tile_logs

        _initial_log_path, argv_prefix = tile_logs.start_terminal_log()
        return _run_detached(["omarchy-launch-terminal", *argv_prefix])
    except Exception:  # noqa: BLE001
        log.exception("tile_logs setup failed, opening terminal untracked")
        return _run_detached(["omarchy-launch-terminal"])


def read_tile_log(args: dict) -> ActionResult:
    from . import tile_logs

    text = tile_logs.read_log(args.get("window"))
    return ActionResult(True, text)


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


def remember_preference(args: dict) -> ActionResult:
    text = (args.get("preference") or "").strip()
    if not text:
        return ActionResult(False, "no preference given")
    from ..core.memory import add_preference

    add_preference(text)
    return ActionResult(True, "remembered")


def describe_screen(args: dict) -> ActionResult:
    from ..config import load_config
    from .vision import describe_screen as _describe_screen

    question = args.get("question") or "Briefly describe what's on the screen."
    cfg = load_config()
    text = _describe_screen(
        question, cfg.api_key_path, cfg.responses_model, cfg.responses_reasoning_effort
    )
    if text.startswith("error:"):
        return ActionResult(False, text)
    return ActionResult(True, text)


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
    from ..display import discovery

    try:
        return discovery.discover_androidtv_devices()
    except Exception:  # noqa: BLE001 -- discovery must never take casting down
        log.exception("TV discovery failed")
        return []


def _resolve_cast_target(target: str | None) -> tuple[str | None, ActionResult | None]:
    """Resolves a spoken/typed target -- a name from list_cast_targets, a
    raw IP[:port], or nothing -- to a single adb address ("host:port").
    Real mDNS discovery (_androidtvremote2._tcp) is authoritative: a named
    target that doesn't match anything real is reported back as a failure
    rather than silently guessed at. Returns (address, None) on success or
    (None, ActionResult) with a message for the model to relay/act on
    (ambiguous match, no match, or multiple candidates needing a pick)."""
    devices = _discover_androidtv_devices()

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

    if len(devices) == 1:
        return f"{devices[0]['address']}:5555", None
    if len(devices) > 1:
        names = ", ".join(d["name"] for d in devices)
        return None, ActionResult(
            False,
            f"found {len(devices)} TVs on the network ({names}) -- call list_cast_targets "
            "and ask the user which one, then call start_casting again with that name as the target.",
        )

    # Discovery found nothing at all -- fall back to the one TV already
    # confirmed working today rather than failing the moment mDNS has a
    # bad day or a TV's remote-control service happens to be off.
    log.info("no TVs discovered via mDNS; falling back to last-known TV %s", _TV_ADB_ADDR)
    return _TV_ADB_ADDR, None


def list_cast_targets(args: dict) -> ActionResult:
    """Real mDNS discovery of every Android TV currently advertising
    _androidtvremote2._tcp on the network -- what the model reads out (or
    uses to disambiguate) when asked "what TVs are available" or when
    start_casting comes back ambiguous."""
    devices = _discover_androidtv_devices()
    if not devices:
        # Still give a real, useful answer: the one TV already known to
        # work, so this isn't just an empty list the moment mDNS has a
        # bad day.
        devices = [{"name": "previously paired TV", "address": _TV_ADB_ADDR.split(":")[0]}]
    return ActionResult(True, json.dumps(devices))


def start_casting(args: dict) -> ActionResult:
    global _cast_process, _signaling_process

    if _cast_process is not None and _cast_process.poll() is None:
        return ActionResult(True, "already casting")

    target = (args.get("target") or "").strip() or None
    tv_addr, err = _resolve_cast_target(target)
    if err is not None:
        return err

    # adb connect can hang for a long time against an unreachable host
    # (confirmed live — a plain 120s-timeout background hang, not a quick
    # failure) so this needs its own short timeout rather than trusting
    # adb to fail fast.
    r = _run(["adb", "connect", tv_addr], timeout=8)
    if not r.ok or "connected" not in r.message.lower():
        return ActionResult(False, f"could not reach {tv_addr} over the network")

    _ensure_receiver_current(tv_addr)

    if not _port_listening(_SIGNALING_PORT):
        _signaling_process = _spawn_own_cgroup(
            [_VENV_PYTHON, "-m", "omarchy_ai.display.signaling"],
            cwd=str(_REPO_ROOT),
        )
        for _ in range(20):
            if _port_listening(_SIGNALING_PORT):
                break
            time.sleep(0.25)
        else:
            return ActionResult(False, "signaling server failed to start")

    _run(
        ["adb", "-s", tv_addr, "shell", "am", "start", "-n", "ai.omarchy.receiver/.MainActivity"],
        timeout=8,
    )
    time.sleep(1)

    _cast_process = _spawn_own_cgroup(
        [_VENV_PYTHON, "-u", str(_REPO_ROOT / "scripts" / "spike_cast_sender.py")],
        cwd=str(_REPO_ROOT),
    )
    time.sleep(2)
    if _cast_process.poll() is not None:
        _cast_process = None
        return ActionResult(False, "casting failed to start")
    return ActionResult(True, f"casting started to the TV at {tv_addr}")


def stop_casting(args: dict) -> ActionResult:
    global _cast_process
    if _cast_process is None or _cast_process.poll() is not None:
        _cast_process = None
        return ActionResult(True, "not currently casting")
    _cast_process.terminate()
    try:
        _cast_process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _cast_process.kill()
    _cast_process = None
    return ActionResult(True, "casting stopped")


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
    r = _run([str(gradlew), "assembleDebug"], timeout=300, cwd=str(gradlew.parent))
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
    "read_tile_log": read_tile_log,
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
    "show_window_labels": show_window_labels,
    "hide_window_labels": hide_window_labels,
    "describe_screen": describe_screen,
    "remember_preference": remember_preference,
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
    "start_casting": start_casting,
    "stop_casting": stop_casting,
    "list_cast_targets": list_cast_targets,
    "install_receiver_on_tv": install_receiver_on_tv,
    "run_omarchy_command": run_omarchy_command,
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
