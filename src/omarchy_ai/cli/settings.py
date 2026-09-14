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

Only a small, deliberately curated whitelist of Config fields is settable
here (SETTABLE) — not every dataclass field. Internal plumbing
(api_key_path, context_max_chars, instructions, ...) is left out on
purpose; see docs/ADR-0001-architecture.md's settings-menu entry for the
reasoning.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys

import yaml

from ..config import CONFIG_DIR, USER_CONFIG_PATH, WAKE_MODELS_DIR, Config, load_config

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

# name -> (validate_and_normalize(raw_json_value, current_config) -> value, description)
SETTABLE = (
    "custom_wake_model_paths",
    "wake_threshold",
    "watchdog_enabled",
    "watchdog_display_mode",
    "voice",
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
        {"name": p.stem, "path": str(p)}
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
    return {
        "fields": fields,
        "defaults": default_fields,
        "wake_models": _wake_models(),
        "watchdog_display_modes": list(WATCHDOG_DISPLAY_MODES),
        "voice_options": voice_options,
        "config_path": str(USER_CONFIG_PATH),
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

    if key == "watchdog_display_mode":
        if value not in WATCHDOG_DISPLAY_MODES:
            raise ValidationError(f"watchdog_display_mode must be one of {WATCHDOG_DISPLAY_MODES}")
        return value

    if key == "voice":
        if not isinstance(value, str) or not value.strip():
            raise ValidationError("voice must be a non-empty string")
        return value.strip()

    raise ValidationError(f"unknown or non-settable key: {key!r}")


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
    busy, reason = _conversation_busy()
    if busy:
        return {"restarted": False, "reason": reason}
    proc = subprocess.run(
        ["systemctl", "--user", "restart", SERVICE],
        capture_output=True, text=True, check=False,
    )
    if proc.returncode != 0:
        return {"restarted": False, "reason": (proc.stderr or proc.stdout or "systemctl restart failed").strip()}
    return {"restarted": True, "reason": reason}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omarchy-ai-settings")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("get")
    p_set = sub.add_parser("set")
    p_set.add_argument("key")
    p_set.add_argument("value")
    sub.add_parser("restart-status")
    sub.add_parser("restart")

    args = parser.parse_args(argv)
    handler = {
        "get": cmd_get,
        "set": cmd_set,
        "restart-status": cmd_restart_status,
        "restart": cmd_restart,
    }[args.command]
    result = handler(args)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
