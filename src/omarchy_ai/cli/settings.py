"""CLI backend for the `omarchy-ai.settings` Quickshell panel
(~/.config/omarchy/plugins/omarchy-ai.settings/Panel.qml).

The panel is plain QML/JS with no direct YAML or systemd access — every
read/write of ~/.config/omarchy-ai/config.yaml and every restart decision
goes through this script via Quickshell's `Process` element (the same
"shell out to a real interpreter for real logic" pattern
$OMARCHY_PATH/shell/plugins/panels/dropbox/status.py already uses inside
the shell itself). Always emits one line of JSON on stdout and exits 0 —
callers check the `error` key rather than the exit code, since a QML
`StdioCollector` only has one clean path for "read what came back".

Commands:
  get                        -> current settings snapshot + wake model list
  set <key> <json-value>     -> validate, merge one field, write config.yaml,
                                 print the fresh snapshot (same shape as get)
  restart-status             -> {"busy": bool, "reason": str}
  restart                    -> restart omarchy-ai.service iff not busy
  pair-phone / revoke-phones -> phone bridge QR pairing (see phone/server.py)
  connect-myapi               -> exchange an ASC Quick Connect code (env
                                 var OMARCHY_AI_MYAPI_CODE) for a MyApi
                                 identity (see ../myapi/)
  disconnect-myapi           -> delete the local MyApi identity
  myapi-usage                -> per-service call counts (omarchy-ai.myapi panel)
  configure-sudo             -> save persistent Sudo Access in GNOME Keyring
  forget-sudo                -> delete saved Sudo Access from GNOME Keyring
  select-cast-target <addr>  -> manual click in the omarchy-ai.tv-discovery
                                 overlay; runs the real start_casting
  refresh-cast-targets       -> the overlay's "Refresh" button

Only a small, deliberately curated whitelist of Config fields is settable
here (SETTABLE) — not every dataclass field. Internal plumbing
(api_key_path, context_max_chars, instructions, ...) is left out on
purpose; see docs/ADR-0001-architecture.md's settings-menu entry for the
reasoning.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys

import yaml

from .. import myapi
from ..myapi import usage as myapi_usage
from ..voice import control
from ..config import (
    CONFIG_DIR,
    LEGACY_KEY_PATH,
    OMARCHY_KEY_PATH,
    USER_CONFIG_PATH,
    WAKE_MODELS_DIR,
    Config,
    load_config,
)

# Two log lines daemon.py always emits, in this exact wording, at the two
# ends of a conversation's lifetime (src/omarchy_ai/core/daemon.py) — used
# as a cheap, dependency-free "is a conversation active right now" check
# over `journalctl`, the same restart discipline this project's own agents
# have followed by hand all day (see STATUS.md/CLAUDE.md). No new
# state-tracking file needed: the daemon already logs exactly this.
_START_MARKER = "wake word detected, starting live session"
_END_MARKER = "session ended, back to listening"

SERVICE = "omarchy-ai.service"

WATCHDOG_DISPLAY_MODES = ("feed", "visualizer", "both")

# This project's own accent, used everywhere else already (Watch Dogs
# overlay, phone bridge page, status dots) — reused here so the pairing QR
# reads as this project's own rather than a generic black-and-white code
# pasted in from qrencode's default. qrencode wants bare RRGGBB, no '#'.
QR_FOREGROUND = "39ff88"
QR_BACKGROUND = "0d1a12"

# name -> (validate_and_normalize(raw_json_value, current_config) -> value, description)
SETTABLE = (
    "custom_wake_model_paths",
    "wake_threshold",
    "watchdog_enabled",
    "sudo_access_enabled",
    "watchdog_display_mode",
    "voice",
    "phone_bridge_enabled",
    "myapi_enabled",
)

# A curated set of realtime voices this project has seen documented/used —
# not exhaustive (OpenAI adds voices over time and the daemon doesn't
# validate this field itself, it just passes it through), just enough to
# give the settings panel a sensible picker. The user's own current value
# is always included even if it isn't in this list.
KNOWN_VOICES = ["marin", "cedar", "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse"]


class ValidationError(Exception):
    pass


def _wake_models() -> list[dict]:
    """Every *.onnx under WAKE_MODELS_DIR — same glob config.py's own
    default_factory uses, so this list always matches what the daemon
    would auto-discover. Never hardcode model names (omachy/omri/roni are
    just whatever happens to be installed today)."""
    if not WAKE_MODELS_DIR.is_dir():
        return []
    return [
        {"name": p.name, "path": str(p)}
        for p in sorted(WAKE_MODELS_DIR.glob("*.onnx"))
    ]


def _snapshot() -> dict:
    cfg = load_config()
    defaults = Config()
    fields = {name: getattr(cfg, name) for name in SETTABLE}
    default_fields = {name: getattr(defaults, name) for name in SETTABLE}
    voice_options = list(KNOWN_VOICES)
    if cfg.voice not in voice_options:
        voice_options.append(cfg.voice)
    from ..phone.server import paired_count
    from ..execution import sudo_approval

    return {
        "fields": fields,
        "defaults": default_fields,
        "wake_models": _wake_models(),
        "watchdog_display_modes": list(WATCHDOG_DISPLAY_MODES),
        "voice_options": voice_options,
        "config_path": str(USER_CONFIG_PATH),
        "api_key": _api_key_state(),
        "phone_bridge_paired_count": paired_count(),
        "myapi": _myapi_state(),
        "assistant": control.request('status'),
        "sudo_approval": sudo_approval.status(),
    }


def _validate(key: str, raw_value: str, cfg: Config) -> object:
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        # Plain unquoted strings (e.g. `voice marin` rather than `voice
        # '"marin"'`) are common from a shell/Process caller — accept them
        # as a literal string rather than forcing every caller to
        # double-quote scalars.
        value = raw_value

    if key == "custom_wake_model_paths":
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValidationError("custom_wake_model_paths must be a list of strings")
        known = {m["path"] for m in _wake_models()}
        unknown = [v for v in value if v not in known]
        if unknown:
            raise ValidationError(f"not a discovered wake model: {unknown!r}")
        if len(value) == 0:
            # wake.py's _resolve_model_paths falls back to the bundled
            # pretrained-model lookup when this list is empty, and
            # "omachy" isn't a bundled openWakeWord model — an empty list
            # here would crash the daemon on its next restart
            # (ValueError: wake word 'omachy' not found among bundled
            # models). At least one active model is a hard requirement,
            # not a preference.
            raise ValidationError("at least one wake model must stay active")
        return sorted(set(value))

    if key == "wake_threshold":
        try:
            f = float(value)
        except (TypeError, ValueError):
            raise ValidationError("wake_threshold must be a number") from None
        if not (0.0 < f < 1.0):
            raise ValidationError("wake_threshold must be between 0 and 1")
        return f

    if key == "watchdog_enabled":
        if not isinstance(value, bool):
            raise ValidationError("watchdog_enabled must be a boolean")
        return value

    if key == "sudo_access_enabled":
        if not isinstance(value, bool):
            raise ValidationError("sudo_access_enabled must be a boolean")
        return value

    if key == "watchdog_display_mode":
        if value not in WATCHDOG_DISPLAY_MODES:
            raise ValidationError(f"watchdog_display_mode must be one of {WATCHDOG_DISPLAY_MODES}")
        return value

    if key == "voice":
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("voice must be a non-empty string")
        return value.strip()

    if key == "phone_bridge_enabled":
        if not isinstance(value, bool):
            raise ValidationError("phone_bridge_enabled must be a boolean")
        return value

    if key == "myapi_enabled":
        if not isinstance(value, bool):
            raise ValidationError("myapi_enabled must be a boolean")
        return value

    raise ValidationError(f"unknown or non-settable key: {key!r}")


def _api_key_state() -> dict:
    """Whether a key exists and where — deliberately never the value.

    The settings UI only ever needs "is one set?" to render its field; the
    secret itself must not round-trip back out to the QML/JS layer, get
    into a Process's stdout, or land in a log. Same principle as a password
    field that shows dots for a stored password it can't actually read.
    """
    if OMARCHY_KEY_PATH.exists():
        return {"set": True, "source": "omarchy-ai", "path": str(OMARCHY_KEY_PATH)}
    if LEGACY_KEY_PATH.exists():
        return {"set": True, "source": "omavoice", "path": str(LEGACY_KEY_PATH)}
    return {"set": False, "source": None, "path": str(OMARCHY_KEY_PATH)}


def _myapi_state() -> dict:
    """Connection status for the panel to bind to — cheap/local only, same
    "presence check, never a network call" discipline as _api_key_state()
    (this runs from the settings CLI's hot path, not somewhere a
    myapiai.com round-trip is welcome)."""
    client = myapi.MyApiClient()
    return {
        "connected": client.connected,
        "scope": client.scope,
        "account": client.account,
    }


def cmd_connect_myapi(_args: argparse.Namespace) -> dict:
    """Exchange a one-time ASC Quick Connect code (minted on the user's
    MyApi dashboard, myapiai.com) for a local Ed25519 identity.

    The code is read from the OMARCHY_AI_MYAPI_CODE environment variable,
    never argv — same reasoning as cmd_set_api_key, even though a Quick
    Connect code is short-lived and single-use rather than a durable
    secret, the private key minted alongside it is not.
    """
    raw = os.environ.get("OMARCHY_AI_MYAPI_CODE")
    if raw is None:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
    code = (raw or "").strip()
    if not code:
        return {"error": "no connection code provided"}

    try:
        myapi.MyApiClient().enroll(code)
    except myapi.MyApiError as e:
        return {"error": str(e)}

    return _snapshot()


def cmd_disconnect_myapi(_args: argparse.Namespace) -> dict:
    myapi.disconnect()
    return _snapshot()


def cmd_myapi_usage(_args: argparse.Namespace) -> dict:
    """Per-service call counts for the omarchy-ai.myapi bar panel's usage
    view — separate from `get`'s snapshot since this is a different
    refresh cadence (polled on a timer by that panel, not on every field
    change) and not something the main settings panel needs at all."""
    return {"services": myapi_usage.aggregate()}


def cmd_configure_sudo(_args: argparse.Namespace) -> dict:
    """Accept a password from the QML process environment, never argv/stdout."""
    from ..execution import sudo_approval

    password = os.environ.get("OMARCHY_AI_SUDO_PASSWORD", "")
    if not password:
        return {"error": "enter your password first"}
    try:
        sudo_approval.store(password)
    except (OSError, ValueError) as error:
        return {"error": str(error)}
    return _snapshot()


def cmd_forget_sudo(_args: argparse.Namespace) -> dict:
    from ..execution import sudo_approval
    try:
        sudo_approval.clear()
    except OSError as error:
        return {"error": str(error)}
    return _snapshot()


def cmd_select_cast_target(args: argparse.Namespace) -> dict:
    """Manual click on a device row in the omarchy-ai.tv-discovery overlay
    — calls the exact same execution.actions.start_casting the voice path
    calls, so a click and a spoken answer are provably the same action,
    not two implementations that could drift apart. Runs in this
    short-lived CLI process, not the daemon — fine, since start_casting
    only touches module-level state in execution/actions.py and the
    shared display/registry.py, neither of which is daemon-process-bound."""
    from ..execution.actions import start_casting

    address = (args.address or "").strip()
    if not address:
        return {"error": "no address given"}
    result = start_casting({"target": address})
    return {"ok": result.ok, "message": result.message}


def cmd_refresh_cast_targets(_args: argparse.Namespace) -> dict:
    """The overlay's "Refresh" button — one real discovery pass through
    the shared registry, pushed back to the overlay immediately rather
    than waiting for its own background poll tick."""
    from ..display import registry, tv_overlay

    devices = registry.refresh()
    tv_overlay.push_update()
    return {"devices": devices}


def cmd_set_api_key(_args: argparse.Namespace) -> dict:
    """Store an OpenAI API key at OMARCHY_KEY_PATH, 0600.

    The key is read from the OMARCHY_AI_API_KEY environment variable, or
    stdin if that's unset — never from argv. A process's argv is
    world-readable via /proc/<pid>/cmdline, so anyone with a shell on this
    machine could read a key passed as an argument straight out of `ps`;
    /proc/<pid>/environ is 0400 owner-only, and a pipe isn't exposed at
    all. That difference is the whole reason this isn't just another
    `set <key> <value>` call.
    """
    raw = os.environ.get("OMARCHY_AI_API_KEY")
    if raw is None:
        raw = "" if sys.stdin.isatty() else sys.stdin.read()
    key = (raw or "").strip()

    if not key:
        return {"error": "no API key provided"}
    if len(key) < 20 or any(c.isspace() for c in key):
        return {"error": "that doesn't look like an API key"}
    # Deliberately loose: OpenAI has shipped sk-, sk-proj-, and others over
    # time, and a too-strict check would reject a valid future format. This
    # only catches obvious paste mistakes. No live API call to verify it —
    # out of scope, and needlessly exercises a secret to answer a question
    # the next real session answers anyway.
    if not key.startswith("sk-"):
        return {"error": "an OpenAI API key normally starts with 'sk-'"}

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    # Create with 0600 from the start rather than writing then chmod-ing —
    # no window where the key sits on disk world-readable.
    fd = os.open(OMARCHY_KEY_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            f.write(key + "\n")
    except Exception:
        os.close(fd)
        raise
    os.chmod(OMARCHY_KEY_PATH, 0o600)

    return _snapshot()


def cmd_get(_args: argparse.Namespace) -> dict:
    return _snapshot()


def cmd_set(args: argparse.Namespace) -> dict:
    if args.key not in SETTABLE:
        return {"error": f"unknown or non-settable key: {args.key!r}"}
    cfg = load_config()
    try:
        value = _validate(args.key, args.value, cfg)
    except ValidationError as e:
        return {"error": str(e)}

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data: dict = {}
    if USER_CONFIG_PATH.exists():
        with open(USER_CONFIG_PATH) as f:
            data = yaml.safe_load(f) or {}

    default_value = getattr(Config(), args.key)
    if value == default_value:
        # Keep the file minimal — only list what differs from the
        # dataclass defaults, matching config.py's own module docstring
        # ("Defaults live here; config.yaml overrides them").
        data.pop(args.key, None)
    else:
        data[args.key] = value

    with open(USER_CONFIG_PATH, "w") as f:
        f.write(
            "# Overrides for src/omarchy_ai/config.py's defaults — only list "
            "what you want to change, it's merged over the defaults.\n"
            "# Written by the Omarchy AI settings panel "
            "(omarchy-ai.settings) — hand edits are fine too, they're\n"
            "# read the same way.\n"
        )
        if data:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=True)

    return _snapshot()


def _conversation_busy() -> tuple[bool, str]:
    is_active = subprocess.run(
        ["systemctl", "--user", "is-active", SERVICE],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    if is_active != "active":
        return False, f"service is not running ({is_active or 'unknown'}), safe to (re)start"

    proc = subprocess.run(
        ["journalctl", "--user", "-u", SERVICE, "-n", "300", "--no-pager", "-o", "cat"],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        # Can't confirm either way — err toward caution, same as this
        # project's own hand-run restart discipline elsewhere in STATUS.md.
        return True, "could not read journalctl, assuming a conversation may be active"

    busy = False
    for line in proc.stdout.splitlines():
        if _START_MARKER in line:
            busy = True
        elif _END_MARKER in line:
            busy = False
    if busy:
        return True, "a conversation appears to be in progress"
    return False, "no conversation currently in progress"


def cmd_restart_status(_args: argparse.Namespace) -> dict:
    busy, reason = _conversation_busy()
    return {"busy": busy, "reason": reason}


def cmd_restart(_args: argparse.Namespace) -> dict:
    proc = subprocess.run(
        ["systemctl", "--user", "restart", SERVICE],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return {"restarted": False, "reason": (proc.stderr or proc.stdout or "systemctl restart failed").strip()}
    return {"restarted": True, "reason": "service restarted"}


def cmd_pair_phone(_args: argparse.Namespace) -> dict:
    """Mints a single-use, 5-minute pairing token and turns its URL into
    a QR PNG the panel can display directly. The daemon (a separate,
    long-running process) reads the token file this writes when a phone
    hits `GET /pair?token=...` — see phone/server.py's module docstring
    for why this is a shared file rather than a call between the two
    processes."""
    cfg = load_config()
    if not cfg.phone_bridge_enabled:
        return {"error": "phone bridge is turned off — enable it above first"}
    if shutil.which("qrencode") is None:
        return {"error": "qrencode is not installed (pacman -S qrencode)"}

    from ..phone.server import mint_pairing_token

    info = mint_pairing_token(cfg)
    proc = subprocess.run(
        [
            "qrencode", "-t", "PNG", "-s", "8",
            # Recolored to this project's own accent instead of qrencode's
            # plain black-on-white default — confirmed live with
            # `magick -unique-colors` that this actually produces a real
            # 2-color (accent/dark) PNG, not just plausible-looking flags.
            # High-contrast pair (bright green on near-black), so this
            # doesn't trade away real-world scannability for the look.
            f"--foreground={QR_FOREGROUND}", f"--background={QR_BACKGROUND}",
            "-o", "-", info["url"],
        ],
        capture_output=True, check=False,
    )
    if proc.returncode != 0 or not proc.stdout:
        return {"error": (proc.stderr or b"qrencode failed").decode(errors="replace").strip()}

    return {
        "url": info["url"],
        "expires_at": info["expires_at"],
        "qr_png_base64": base64.b64encode(proc.stdout).decode(),
    }


def cmd_revoke_phones(_args: argparse.Namespace) -> dict:
    from ..phone.server import revoke_all_sessions

    revoke_all_sessions()
    return _snapshot()


def cmd_activate(_args):
    return control.request('activate')


def cmd_myapi_dashboard(args):
    from ..myapi.dashboard import fetch
    try:
        return fetch(args.period)
    except (myapi.MyApiError, OSError, ValueError) as error:
        return {'error': str(error)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omarchy-ai-settings")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("get")
    p_set = sub.add_parser("set")
    p_set.add_argument("key")
    p_set.add_argument("value")
    sub.add_parser("set-api-key")
    sub.add_parser("restart-status")
    sub.add_parser("restart")
    sub.add_parser("pair-phone")
    sub.add_parser("revoke-phones")
    sub.add_parser("connect-myapi")
    sub.add_parser("disconnect-myapi")
    sub.add_parser("myapi-usage")
    sub.add_parser("configure-sudo")
    sub.add_parser("forget-sudo")
    p_dashboard = sub.add_parser('myapi-dashboard')
    p_dashboard.add_argument('period', choices=('24h', '7d', '30d'), default='7d', nargs='?')
    sub.add_parser('activate')
    p_select_cast = sub.add_parser("select-cast-target")
    p_select_cast.add_argument("address")
    sub.add_parser("refresh-cast-targets")

    args = parser.parse_args(argv)
    handler = {
        "get": cmd_get,
        "set": cmd_set,
        "set-api-key": cmd_set_api_key,
        "restart-status": cmd_restart_status,
        "pair-phone": cmd_pair_phone,
        "revoke-phones": cmd_revoke_phones,
        "connect-myapi": cmd_connect_myapi,
        "disconnect-myapi": cmd_disconnect_myapi,
        "myapi-usage": cmd_myapi_usage,
        "configure-sudo": cmd_configure_sudo,
        "forget-sudo": cmd_forget_sudo,
        "myapi-dashboard": cmd_myapi_dashboard,
        "activate": cmd_activate,
        "select-cast-target": cmd_select_cast_target,
        "refresh-cast-targets": cmd_refresh_cast_targets,
        "restart": cmd_restart,
    }[args.command]
    result = handler(args)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
