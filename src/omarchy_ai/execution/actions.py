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

import ipaddress
import json
import logging
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
_cast_process: subprocess.Popen | None = None
_signaling_process: subprocess.Popen | None = None
# Set by install_receiver_on_tv's discovery step, consumed by its pairing
# step on the next call -- same "module-level state across separate tool
# calls" pattern _cast_process already uses.
_pending_pair_target: dict | None = None


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

    if not _port_listening(_SIGNALING_PORT):
        _signaling_process = subprocess.Popen(
            [_VENV_PYTHON, "-m", "omarchy_ai.display.signaling"],
            cwd=str(_REPO_ROOT),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
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

    _cast_process = subprocess.Popen(
        [_VENV_PYTHON, "-u", str(_REPO_ROOT / "scripts" / "spike_cast_sender.py")],
        cwd=str(_REPO_ROOT),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
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


def _ensure_receiver_apk_built() -> ActionResult:
    if _APK_PATH.exists():
        return ActionResult(True, f"receiver APK already built ({_APK_PATH.stat().st_size} bytes)")
    gradlew = _REPO_ROOT / "android-receiver" / "gradlew"
    if not gradlew.exists():
        return ActionResult(False, "android-receiver/gradlew not found -- can't build the receiver app")
    log.info("receiver APK missing, building via ./gradlew assembleDebug")
    r = _run([str(gradlew), "assembleDebug"], timeout=300)
    if not r.ok or not _APK_PATH.exists():
        return ActionResult(False, f"building the receiver APK failed: {r.message}")
    return ActionResult(True, "built the receiver APK")


def install_receiver_on_tv(args: dict) -> ActionResult:
    """Guided new-TV setup, real step by step.

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

    apk = _ensure_receiver_apk_built()
    if not apk.ok:
        return apk

    pairing_code = (args.get("pairing_code") or "").strip() or None
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
