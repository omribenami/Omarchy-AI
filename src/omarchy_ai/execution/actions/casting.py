"""Android TV casting and receiver install.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

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
from pathlib import Path

from .base import ActionResult, _run

log = logging.getLogger("omarchy_ai.execution.actions")


# --- Android TV casting (Phase 2) --------------------------------------
# casting.py lives at <repo>/src/omarchy_ai/execution/actions/casting.py.
_REPO_ROOT = Path(__file__).resolve().parents[4]
_VENV_PYTHON = str(_REPO_ROOT / ".venv" / "bin" / "python")
# The last-known-TV fallback constant used to live here; moved to
# display/discovery.py (fallback_tv) so display/registry.py can use it
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
    from ...display import discovery

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
    from ...display import registry

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
    from ...display import discovery

    fallback = discovery.fallback_tv()
    if not fallback:
        return None, ActionResult(False, "no TVs are currently discoverable on the network")
    log.info("no TVs currently online; falling back to last-known TV %s", fallback)
    return fallback, None


def list_cast_targets(args: dict) -> ActionResult:
    """The shared device registry (display/registry.py) -- what the model
    reads out (or uses to disambiguate) when asked "what TVs are
    available" or when start_casting comes back ambiguous. Same data the
    omarchy-ai.tv-discovery overlay renders, refreshed here if stale."""
    from ...display import registry

    return ActionResult(True, json.dumps(registry.get_or_refresh()))


def start_casting(args: dict) -> ActionResult:
    from ...display import session
    try:
        with session.locked():
            return _start_casting(args)
    except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
        log.exception("casting operation failed")
        return ActionResult(False, f"casting failed: {exc}")


def _start_casting(args: dict) -> ActionResult:
    from ...display import registry, tv_overlay, session

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
    from ...display import registry, tv_overlay, session
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

    from ...display import discovery

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
