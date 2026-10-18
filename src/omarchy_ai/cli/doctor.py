"""omarchy-ai-doctor: is the assistant healthy, and if not, what to do.

    omarchy-ai-doctor                 every check: green, yellow or red, with a fix
    omarchy-ai-doctor --json          the same as JSON (SCHEMA_VERSION)
    omarchy-ai-doctor --quick         no network checks (Gateway, MyApi, latest release)
    omarchy-ai-doctor --trace ID      one turn, task or tool call from the JSONL logs
    omarchy-ai-doctor --bundle [PATH] a redacted support tarball; prints its path
    omarchy-ai-doctor --leaked [SRC]  what to rotate when a secret may have leaked

`omarchy-ai doctor ...` is the same command. The assistant runs the checks
through its `run_diagnostics` tool (summary()).

Read-only: no check writes a state file (corrupt ones are reported, not set
aside: the daemon does that), restarts anything or sends anything. The
network checks are single read-only GETs with short timeouts. Works in
crash-loop safe mode (core/health.py): nothing here needs the daemon.

Every boundary (commands, HTTP, the environment, paths) comes in through
Context, so the tests run each check against fakes.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime
import io
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import yaml

GREEN, YELLOW, RED = "green", "yellow", "red"
_RANK = {RED: 0, YELLOW: 1, GREEN: 2}
SCHEMA_VERSION = 1
SERVICE = "omarchy-ai"
HTTP_TIMEOUT = 5.0
COMMAND_TIMEOUT = 10.0
CHECK_TIMEOUT = 30.0
GATEWAY_CREDITS_URL = "https://ai-gateway.vercel.sh/v1/credits"
LATEST_RELEASE_URL = (
    "https://api.github.com/repos/omribenami/Omarchy-AI/releases/latest"
)
UNIT_PROPERTIES = (
    "LoadState,ActiveState,SubState,NRestarts,Type,WatchdogUSec,StatusText,"
    "MainPID,WorkingDirectory"
)
LOW_DISK_BYTES = 500 * 1024 * 1024
WARN_DISK_BYTES = 2 * 1024 * 1024 * 1024
RECENT_UNCERTAIN_SECONDS = 24 * 3600
TASK_HORIZON_SECONDS = 7 * 24 * 3600
BUNDLE_LOG_DAYS = 3
BUNDLE_LOG_BYTES = 20 * 1024 * 1024
# A config key with one of these words holds a secret: the bundle drops it.
_SECRET_KEY = re.compile(r"key|token|secret|passw|pin|credential|auth", re.I)
_DEPENDENCY_FIX = (
    "sudo pacman -S --needed android-tools gst-plugins-bad gst-plugins-good qrencode "
    "python-gobject pipewire-audio pipewire-pulse libpulse"
)

Runner = Callable[..., "Completed"]
Http = Callable[[str, Mapping[str, str], float], "Response"]


@dataclass
class Completed:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass
class Response:
    status: int  # 0: never reached the server
    body: bytes = b""
    error: str = ""


@dataclass
class Result:
    id: str
    title: str
    status: str
    reason: str
    fix: str = ""
    details: dict[str, Any] = field(default_factory=dict)


def run_command(
    argv: list[str],
    timeout: float = COMMAND_TIMEOUT,
    env: Mapping[str, str] | None = None,
) -> Completed:
    """A command's result; a missing program is exit 127, a hang exit 124."""
    try:
        done = subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError:
        return Completed(127, "", f"{argv[0]}: not found")
    except subprocess.TimeoutExpired:
        return Completed(124, "", f"{argv[0]}: no answer within {timeout:g} s")
    except OSError as exc:
        return Completed(126, "", f"{argv[0]}: {exc}")
    return Completed(done.returncode, done.stdout or "", done.stderr or "")


def http_get(
    url: str, headers: Mapping[str, str], timeout: float = HTTP_TIMEOUT
) -> Response:
    request = urllib.request.Request(url, headers=dict(headers), method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as reply:
            return Response(reply.status, reply.read(256 * 1024))
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read(64 * 1024) if exc.fp else b"")
    except urllib.error.URLError as exc:
        return Response(0, b"", str(exc.reason))
    except (TimeoutError, OSError) as exc:
        return Response(0, b"", str(exc) or type(exc).__name__)


@dataclass
class Context:
    """What the checks look at. `default()` is this machine."""

    state_dir: Path
    config_dir: Path
    config_path: Path
    run: Runner = run_command
    http: Http = http_get
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))
    which: Callable[[str], str | None] = shutil.which
    exists: Callable[[Path], bool] = lambda path: Path(path).exists()
    now: float = field(default_factory=time.time)
    quick: bool = False
    installed_version: Callable[[], str] | None = None
    _cache: dict[Any, Any] = field(default_factory=dict, repr=False)

    @classmethod
    def default(cls, **overrides: Any) -> "Context":
        from .. import config

        return cls(
            state_dir=config.STATE_DIR,
            config_dir=config.CONFIG_DIR,
            config_path=config.USER_CONFIG_PATH,
            **overrides,
        )

    def command(self, *argv: str, env: Mapping[str, str] | None = None) -> Completed:
        """Each distinct command runs once per doctor run."""
        key = (argv, tuple(sorted(env.items())) if env else None)
        if key not in self._cache:
            self._cache[key] = self.run(list(argv), timeout=COMMAND_TIMEOUT, env=env)
        result: Completed = self._cache[key]
        return result

    # config.yaml, parsed once: (raw mapping or None, error or "")
    def raw_config(self) -> tuple[dict | None, str]:
        if "config" not in self._cache:
            try:
                text = self.config_path.read_text(encoding="utf-8")
            except FileNotFoundError:
                self._cache["config"] = ({}, "")
            except OSError as exc:
                self._cache["config"] = (None, f"cannot read: {exc}")
            else:
                try:
                    data = yaml.safe_load(text) or {}
                except yaml.YAMLError as exc:
                    self._cache["config"] = (None, " ".join(str(exc).split())[:200])
                else:
                    if isinstance(data, dict):
                        self._cache["config"] = (data, "")
                    else:
                        self._cache["config"] = (None, "the top level is not a mapping")
        value: tuple[dict | None, str] = self._cache["config"]
        return value

    def cfg(self) -> Any:
        """The effective Config; the defaults when config.yaml is unusable."""
        if "cfg" not in self._cache:
            from ..config import Config

            data, _ = self.raw_config()
            known = set(Config.__dataclass_fields__)
            try:
                self._cache["cfg"] = Config(
                    **{k: v for k, v in (data or {}).items() if k in known}
                )
            except TypeError:
                self._cache["cfg"] = Config()
        return self._cache["cfg"]

    def unit(self) -> dict[str, str]:
        done = self.command(
            "systemctl", "--user", "show", SERVICE, "-p", UNIT_PROPERTIES
        )
        if done.returncode:
            return {}
        return dict(
            line.split("=", 1) for line in done.stdout.splitlines() if "=" in line
        )


def _read_state(path: Path) -> tuple[Any, str]:
    """(data, "") or (None, why it does not parse). Never quarantines: the
    daemon's statefile reader does that; doctor only looks."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except UnicodeDecodeError:
        return None, "not UTF-8"
    except OSError as exc:
        return None, f"cannot read: {exc.strerror or exc}"
    if not text.strip():
        return None, ""
    try:
        if path.suffix in (".yaml", ".yml"):
            return yaml.safe_load(text), ""
        return json.loads(text), ""
    except (ValueError, yaml.YAMLError) as exc:
        return None, " ".join(str(exc).split())[:160]


def _when(ts: float | None) -> str:
    if not ts:
        return "?"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def _worst(statuses) -> str:
    return min(statuses, key=lambda s: _RANK[s], default=GREEN)


# ------------------------------------------------------------------ checks


def check_config(ctx: Context) -> Result:
    from ..config import Config

    data, error = ctx.raw_config()
    if data is None:
        return Result(
            "config",
            "Config",
            RED,
            f"{ctx.config_path} does not parse ({error}); the assistant runs on defaults",
            f"fix the YAML in {ctx.config_path}, or move it aside and set things again in Assistant Settings",
        )
    if not ctx.config_path.exists():
        return Result("config", "Config", GREEN, "no config.yaml: built-in defaults")
    known = set(Config.__dataclass_fields__)
    unknown = sorted(k for k in data if k not in known)
    try:
        Config(**{k: v for k, v in data.items() if k in known})
    except TypeError as exc:
        return Result(
            "config",
            "Config",
            RED,
            f"config.yaml is not usable: {exc}",
            f"fix {ctx.config_path}",
        )
    if unknown:
        return Result(
            "config",
            "Config",
            YELLOW,
            f"{len(unknown)} unknown setting(s) ignored: {', '.join(unknown[:6])}",
            f"remove or rename them in {ctx.config_path} (a typo means the setting has no effect)",
            {"unknown": unknown},
        )
    return Result(
        "config", "Config", GREEN, f"parsed, {len(data)} setting(s) over the defaults"
    )


def _key_state(path: Path) -> dict[str, Any]:
    """Present, non-empty and its mode -- never the value."""
    try:
        st = path.stat()
    except OSError:
        return {"present": False}
    return {"present": st.st_size > 0, "mode": oct(st.st_mode & 0o777)[2:]}


def check_api_keys(ctx: Context) -> Result:
    cfg = ctx.cfg()
    provider = str(getattr(cfg, "provider", "") or "")
    fallbacks = list(getattr(cfg, "gemini_fallback_models", []) or [])
    keys = [
        ("openai", Path(cfg.api_key_path), provider == "openai"),
        ("gemini", Path(cfg.gemini_api_key_path), provider == "gemini"),
        ("gateway", Path(cfg.vercel_gateway_api_key_path), provider == "omarchy"),
    ]
    details: dict[str, Any] = {}
    statuses, problems, fixes = [], [], []
    for name, path, required in keys:
        state = _key_state(path)
        details[name] = state
        if not state["present"]:
            if required:
                statuses.append(RED)
                problems.append(
                    f"the {name} key your provider ({provider}) needs is missing"
                )
                fixes.append(f"save the {name} key in Assistant Settings")
            elif name == "gateway":
                statuses.append(YELLOW)
                problems.append(
                    "no Vercel AI Gateway key: Jev, transcription and Gateway fallbacks are off"
                )
                fixes.append("save a Gateway key in Assistant Settings")
            continue
        if int(state["mode"], 8) & 0o077:
            statuses.append(YELLOW)
            problems.append(
                f"the {name} key file is readable by other users ({state['mode']})"
            )
            fixes.append(f"chmod 600 {path}")
    identity = ctx.config_dir / "myapi" / "identity.json"
    details["myapi"] = _key_state(identity)
    if getattr(cfg, "myapi_enabled", False) and not details["myapi"]["present"]:
        statuses.append(YELLOW)
        problems.append("MyApi is on but not connected")
        fixes.append("connect MyApi in its bar panel")
    present = [
        n for n in ("openai", "gemini", "gateway", "myapi") if details[n]["present"]
    ]
    status = _worst(statuses)
    reason = (
        "; ".join(problems)
        if problems
        else (f"present: {', '.join(present) or 'none'} (values never shown)")
    )
    return Result("api_keys", "API keys", status, reason, "; ".join(fixes), details)


def check_gateway(ctx: Context) -> Result:
    title = "Vercel AI Gateway"
    if ctx.quick:
        return Result(
            "gateway", title, GREEN, "skipped (--quick)", details={"skipped": True}
        )
    path = Path(ctx.cfg().vercel_gateway_api_key_path)
    try:
        key = path.read_text().strip()
    except OSError:
        key = ""
    if not key:
        return Result(
            "gateway",
            title,
            YELLOW,
            "no Gateway key, not checked",
            "save a Gateway key in Assistant Settings",
            {"skipped": True},
        )
    reply = ctx.http(
        GATEWAY_CREDITS_URL, {"Authorization": f"Bearer {key}"}, HTTP_TIMEOUT
    )
    del key
    if reply.status == 0:
        return Result(
            "gateway",
            title,
            RED,
            f"unreachable: {reply.error[:160]}",
            "check the network connection; Jev and Gateway voices fail until it is back",
        )
    if reply.status in (401, 403):
        return Result(
            "gateway",
            title,
            RED,
            f"the Gateway refused the key (HTTP {reply.status})",
            "create a new key at vercel.com/dashboard/ai-gateway and save it in Assistant Settings",
        )
    if reply.status != 200:
        return Result(
            "gateway",
            title,
            YELLOW,
            f"reachable, but answered HTTP {reply.status}",
            "try again later; if it stays, check status.vercel.com",
        )
    try:
        balance = float(json.loads(reply.body).get("balance"))
    except (ValueError, TypeError, AttributeError):
        return Result("gateway", title, GREEN, "reachable, key accepted")
    details = {"balance": balance}
    if balance <= 0:
        return Result(
            "gateway",
            title,
            RED,
            "key accepted, but the credit balance is used up",
            "add credits at vercel.com/dashboard/ai-gateway",
            details,
        )
    if balance < 1:
        return Result(
            "gateway",
            title,
            YELLOW,
            f"key accepted; credit is low (${balance:.2f})",
            "add credits at vercel.com/dashboard/ai-gateway",
            details,
        )
    return Result(
        "gateway",
        title,
        GREEN,
        f"reachable, key accepted (balance ${balance:.2f})",
        details=details,
    )


def check_myapi(ctx: Context) -> Result:
    title = "MyApi"
    enabled = bool(getattr(ctx.cfg(), "myapi_enabled", False))
    identity_path = ctx.config_dir / "myapi" / "identity.json"
    identity, error = _read_state(identity_path)
    if identity is None and not error:
        if enabled:
            return Result(
                "myapi",
                title,
                YELLOW,
                "on, but not connected",
                "connect MyApi in its bar panel (Quick Connect code from myapiai.com)",
            )
        return Result("myapi", title, GREEN, "not used", details={"skipped": True})
    if error or not isinstance(identity, dict):
        return Result(
            "myapi",
            title,
            RED,
            f"the MyApi identity file does not parse ({error or 'not an object'})",
            "disconnect and connect MyApi again in its bar panel",
        )
    if ctx.quick:
        return Result(
            "myapi",
            title,
            GREEN,
            "connected; reachability skipped (--quick)",
            details={"skipped": True},
        )
    from ..myapi import client as myapi_client

    try:
        signer = myapi_client.MyApiClient.__new__(myapi_client.MyApiClient)
        signer._identity = identity
        headers = signer._sign_headers()
    except Exception as exc:  # noqa: BLE001 -- a broken identity is the finding
        return Result(
            "myapi",
            title,
            RED,
            f"the MyApi identity cannot sign requests ({type(exc).__name__})",
            "disconnect and connect MyApi again in its bar panel",
        )
    headers.update(
        {"Accept": "application/json", "User-Agent": myapi_client._USER_AGENT}
    )
    reply = ctx.http(myapi_client.BASE_URL + "/services", headers, HTTP_TIMEOUT)
    if reply.status == 0:
        return Result(
            "myapi",
            title,
            RED,
            f"myapiai.com unreachable: {reply.error[:160]}",
            "check the network connection",
        )
    if reply.status in (401, 403):
        return Result(
            "myapi",
            title,
            RED,
            f"MyApi refused this device (HTTP {reply.status})",
            "reconnect MyApi in its bar panel; the device may have been removed on myapiai.com",
        )
    if reply.status != 200:
        return Result(
            "myapi",
            title,
            YELLOW,
            f"reachable, but answered HTTP {reply.status}",
            "try again later",
        )
    note = "" if enabled else " (connected, but turned off in settings)"
    return Result("myapi", title, GREEN, "reachable, identity accepted" + note)


def _provider_rows(ctx: Context) -> tuple[list[dict], str]:
    from ..core import provider_health

    target = ctx.state_dir / provider_health.FILE_NAME
    data, error = _read_state(target)
    if error:
        return [], error
    try:
        same = target == provider_health.path()
        return provider_health.snapshot(None if same else target), ""
    except Exception as exc:  # noqa: BLE001
        return [], f"{type(exc).__name__}: {exc}"


def check_quota(ctx: Context) -> Result:
    rows, error = _provider_rows(ctx)
    if error:
        return Result(
            "quota",
            "Quota",
            YELLOW,
            f"provider_health.json unreadable ({error})",
            "see the state files check",
        )
    out = [r for r in rows if r.get("kind") == "quota"]
    if not out:
        return Result("quota", "Quota", GREEN, "no provider out of credits or quota")
    names = ", ".join(f"{r['provider']} until {_when(r['until'])}" for r in out)
    from ..core import quota

    links = "; ".join(
        quota.PROVIDERS[r["provider"]][2]
        for r in out
        if r["provider"] in quota.PROVIDERS
    )
    return Result(
        "quota",
        "Quota",
        RED,
        f"out of credits/quota: {names}",
        "add credits or wait for the quota to reset" + (f": {links}" if links else ""),
        {"providers": out},
    )


def check_provider_health(ctx: Context) -> Result:
    rows, error = _provider_rows(ctx)
    if error:
        return Result(
            "provider_health",
            "Provider health",
            YELLOW,
            f"provider_health.json unreadable ({error})",
            "see the state files check",
        )
    benched = [r for r in rows if r.get("kind") != "quota"]
    if not benched:
        return Result(
            "provider_health", "Provider health", GREEN, "no provider benched"
        )
    text = "; ".join(
        f"{r['provider']} ({r['kind']}) until {_when(r['until'])}: {str(r.get('reason') or r.get('state'))[:80]}"
        for r in benched[:4]
    )
    return Result(
        "provider_health",
        "Provider health",
        YELLOW,
        f"{len(benched)} benched: {text}",
        "nothing to do unless it persists; they come back on their own when the time is up",
        {"benched": benched},
    )


def check_safe_mode(ctx: Context) -> Result:
    from ..core import health

    target = ctx.state_dir / health.FILE_NAME
    data, error = _read_state(target)
    if error:
        return Result(
            "safe_mode",
            "Crash loop / safe mode",
            YELLOW,
            f"health.json unreadable ({error})",
            "see the state files check",
        )
    status = (
        health.status(target)
        if data is not None
        else {"safe_mode": False, "since": None, "crashes": 0}
    )
    status["active_here"] = bool(health.active)
    if status["safe_mode"]:
        return Result(
            "safe_mode",
            "Crash loop / safe mode",
            RED,
            f"in safe mode since {_when(status['since'])}: voice only, scheduled actions and task resume off",
            f"find the crash: journalctl --user -u {SERVICE} -n 200; it clears after "
            f"{int(health.STABLE_SECONDS // 60)} min without a crash",
            status,
        )
    if status["crashes"]:
        return Result(
            "safe_mode",
            "Crash loop / safe mode",
            YELLOW,
            f"{status['crashes']} crash(es) in the last {int(health.CRASH_WINDOW // 60)} min",
            f"journalctl --user -u {SERVICE} -n 200",
            status,
        )
    return Result(
        "safe_mode",
        "Crash loop / safe mode",
        GREEN,
        "no recent crashes",
        details=status,
    )


def check_systemd(ctx: Context) -> Result:
    title = "systemd unit"
    unit = ctx.unit()
    if not unit or unit.get("LoadState") == "not-found":
        return Result(
            "systemd",
            title,
            YELLOW,
            f"{SERVICE}.service is not installed for this user",
            "bash install.sh sets it up",
        )
    details = {
        k: unit.get(k, "")
        for k in (
            "ActiveState",
            "SubState",
            "NRestarts",
            "Type",
            "WatchdogUSec",
            "StatusText",
        )
    }
    state = unit.get("ActiveState", "")
    status_text = unit.get("StatusText") or "-"
    if state == "failed":
        return Result(
            "systemd",
            title,
            RED,
            f"failed ({unit.get('SubState')})",
            f"journalctl --user -u {SERVICE} -n 100, then systemctl --user restart {SERVICE}",
            details,
        )
    if state != "active":
        return Result(
            "systemd",
            title,
            RED,
            f"not running ({state or 'unknown'})",
            f"systemctl --user start {SERVICE}",
            details,
        )
    problems, fixes = [], []
    try:
        restarts = int(unit.get("NRestarts") or 0)
    except ValueError:
        restarts = 0
    if restarts:
        problems.append(f"restarted {restarts} time(s) since it was loaded")
        fixes.append(f"journalctl --user -u {SERVICE} -n 200")
    if unit.get("Type") != "notify" or unit.get("WatchdogUSec") in (
        "",
        "0",
        "infinity",
    ):
        problems.append("the unit has no watchdog (an older unit file)")
        fixes.append("bash install.sh installs the current unit")
    if problems:
        return Result(
            "systemd",
            title,
            YELLOW,
            f"active; {'; '.join(problems)}",
            "; ".join(fixes),
            details,
        )
    return Result(
        "systemd",
        title,
        GREEN,
        f"active, watchdog {unit.get('WatchdogUSec')}, status: {status_text}",
        details=details,
    )


_DEVICE = re.compile(r"^[\s│├└─]*(\*)?\s*(\d+)\.\s+(.*?)\s*(?:\[[^\]]*\])?\s*$")


def parse_wpctl_status(text: str) -> dict[str, list[dict]]:
    """{"sinks": [...], "sources": [...]} of the Audio section; each
    {"id", "name", "default"}."""
    found: dict[str, list[dict]] = {"sinks": [], "sources": []}
    section = sub = ""
    for line in text.splitlines():
        if line and not line[0].isspace() and line[0] not in "│├└":
            section, sub = line.strip(), ""
            continue
        heading = re.match(r"^[\s│]*[├└]─\s*(\w[\w ]*):\s*$", line)
        if heading:
            sub = heading.group(1).strip().lower()
            continue
        if section != "Audio" or sub not in found:
            continue
        match = _DEVICE.match(line)
        if match:
            found[sub].append(
                {
                    "id": int(match.group(2)),
                    "name": match.group(3).strip(),
                    "default": bool(match.group(1)),
                }
            )
    return found


def check_pipewire(ctx: Context) -> Result:
    done = ctx.command("wpctl", "status")
    if done.returncode:
        return Result(
            "pipewire",
            "PipeWire",
            RED,
            f"wpctl status failed: {(done.stderr or done.stdout).strip()[:160] or done.returncode}",
            "systemctl --user status pipewire wireplumber",
        )
    devices = parse_wpctl_status(done.stdout)
    return Result(
        "pipewire",
        "PipeWire",
        GREEN,
        f"running: {len(devices['sinks'])} output(s), {len(devices['sources'])} input(s)",
        details={"sinks": len(devices["sinks"]), "sources": len(devices["sources"])},
    )


def _volume(ctx: Context, target: str) -> tuple[float | None, bool, str]:
    done = ctx.command("wpctl", "get-volume", target)
    if done.returncode:
        return None, False, (done.stderr or done.stdout).strip()[:160]
    match = re.search(r"Volume:\s*([\d.]+)", done.stdout)
    return (float(match.group(1)) if match else None), "MUTED" in done.stdout, ""


def _audio_device(ctx: Context, kind: str) -> Result:
    mic = kind == "sources"
    check_id, title = ("microphone", "Microphone") if mic else ("speaker", "Speaker")
    status_run = ctx.command("wpctl", "status")
    if status_run.returncode:
        return Result(
            check_id, title, RED, "PipeWire is not reachable", "see the PipeWire check"
        )
    devices = parse_wpctl_status(status_run.stdout)[kind]
    default = next((d for d in devices if d["default"]), None)
    volume, muted, error = _volume(
        ctx, "@DEFAULT_AUDIO_SOURCE@" if mic else "@DEFAULT_AUDIO_SINK@"
    )
    details: dict[str, Any] = {
        "default": default["name"] if default else None,
        "count": len(devices),
        "volume": volume,
        "muted": muted,
    }
    if error or (not devices and volume is None):
        return Result(
            check_id,
            title,
            RED,
            f"no default {'input' if mic else 'output'} device",
            "plug in or enable the device, then pick it in the audio panel",
            details,
        )
    configured = getattr(ctx.cfg(), "mic_device", None) if mic else None
    if (
        configured
        and str(configured).isdigit()
        and int(configured) not in {d["id"] for d in devices}
    ):
        return Result(
            check_id,
            title,
            RED,
            f"mic_device {configured} is not an input device now",
            "set mic_device in config.yaml to a current device (wpctl status), or remove it",
            details,
        )
    name = details["default"] or "default device"
    if muted:
        return Result(
            check_id,
            title,
            YELLOW,
            f"{name} is muted",
            f"unmute it: wpctl set-mute {'@DEFAULT_AUDIO_SOURCE@' if mic else '@DEFAULT_AUDIO_SINK@'} 0",
            details,
        )
    if volume is not None and volume < 0.05:
        return Result(
            check_id,
            title,
            YELLOW,
            f"{name} volume is {volume:.2f}",
            "raise it in the audio panel",
            details,
        )
    extra = f", target {configured}" if configured else ""
    shown = f"{volume:.2f}" if volume is not None else "?"
    return Result(
        check_id, title, GREEN, f"{name} (volume {shown}{extra})", details=details
    )


def check_microphone(ctx: Context) -> Result:
    return _audio_device(ctx, "sources")


def check_speaker(ctx: Context) -> Result:
    return _audio_device(ctx, "sinks")


def check_wake_model(ctx: Context) -> Result:
    cfg = ctx.cfg()
    paths = list(
        cfg.custom_wake_model_paths
        or ([cfg.custom_wake_model_path] if cfg.custom_wake_model_path else [])
    )
    if not paths:
        import importlib.util

        if importlib.util.find_spec("openwakeword") is None:
            return Result(
                "wake_model",
                "Wake word model",
                RED,
                "no wake model and openwakeword is not installed",
                "uv sync, then pick a wake model in Assistant Settings",
            )
        return Result(
            "wake_model",
            "Wake word model",
            YELLOW,
            f"no custom model: uses openwakeword's bundled '{cfg.wake_word}'",
            "train or pick a wake model in Assistant Settings for the assistant's own name",
        )
    bad = []
    for raw in paths:
        path = Path(str(raw)).expanduser()
        try:
            size = path.stat().st_size
        except OSError:
            bad.append(f"{path.name}: missing")
            continue
        if size == 0:
            bad.append(f"{path.name}: empty")
        elif path.suffix not in (".onnx", ".tflite"):
            bad.append(f"{path.name}: not an .onnx/.tflite model")
    if bad:
        return Result(
            "wake_model",
            "Wake word model",
            RED,
            "; ".join(bad),
            "put the model file back or pick another in Assistant Settings",
            {"models": len(paths)},
        )
    return Result(
        "wake_model",
        "Wake word model",
        GREEN,
        f"{len(paths)} model(s): {', '.join(Path(str(p)).name for p in paths)}",
    )


def check_desktop(ctx: Context) -> Result:
    title = "Wayland / Hyprland"
    runtime = Path(ctx.env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    wayland = ctx.env.get("WAYLAND_DISPLAY", "")
    signature = ctx.env.get("HYPRLAND_INSTANCE_SIGNATURE", "")
    found_from = "environment"
    if not wayland or not ctx.exists(runtime / wayland):
        sockets = sorted(
            p.name for p in runtime.glob("wayland-*") if not p.name.endswith(".lock")
        )
        wayland, found_from = (sockets[0] if sockets else ""), "runtime dir"
    if not signature or not ctx.exists(runtime / "hypr" / signature / ".socket.sock"):
        instances = sorted(
            runtime.glob("hypr/*/.socket.sock"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        signature, found_from = (
            (instances[0].parent.name if instances else ""),
            "runtime dir",
        )
    if not wayland:
        return Result(
            "desktop",
            title,
            RED,
            "no Wayland display socket",
            "log in to the Hyprland session; the assistant controls that desktop",
        )
    if not signature:
        return Result(
            "desktop",
            title,
            RED,
            f"Wayland {wayland}, but no Hyprland instance socket",
            "start Hyprland (uwsm start hyprland), or log in again",
        )
    env = dict(ctx.env)
    env.update(
        HYPRLAND_INSTANCE_SIGNATURE=signature,
        WAYLAND_DISPLAY=wayland,
        XDG_RUNTIME_DIR=str(runtime),
    )
    done = ctx.command("hyprctl", "version", "-j", env=env)
    if done.returncode:
        return Result(
            "desktop",
            title,
            RED,
            f"Hyprland IPC does not answer: {(done.stderr or done.stdout).strip()[:120] or done.returncode}",
            "restart Hyprland or log in again",
        )
    try:
        tag = json.loads(done.stdout).get("tag") or "?"
    except (ValueError, AttributeError):
        tag = "?"
    return Result(
        "desktop",
        title,
        GREEN,
        f"Wayland {wayland}, Hyprland {tag} answers (found via {found_from})",
        details={"wayland": wayland},
    )


def check_sandbox(ctx: Context) -> Result:
    title = "Command sandbox (bwrap)"
    bwrap = ctx.which("bwrap")
    enabled = bool(getattr(ctx.cfg(), "sandbox_commands", True))
    if not bwrap:
        return Result(
            "sandbox",
            title,
            RED if enabled else YELLOW,
            "bubblewrap is not installed: commands run unsandboxed",
            "sudo pacman -S bubblewrap",
        )
    done = ctx.command(
        bwrap, "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "true"
    )
    if done.returncode:
        return Result(
            "sandbox",
            title,
            RED,
            f"bwrap cannot create a sandbox here: {(done.stderr or '').strip()[:120] or done.returncode}",
            "enable unprivileged user namespaces (kernel.unprivileged_userns_clone=1)",
        )
    if not enabled:
        return Result(
            "sandbox",
            title,
            YELLOW,
            "bwrap works, but sandbox_commands is off in config",
            "remove sandbox_commands: false from config.yaml",
        )
    return Result("sandbox", title, GREEN, "bwrap works; commands run sandboxed")


def check_dependencies(ctx: Context) -> Result:
    """scripts/check-dependencies.sh, folded in."""
    required = ("pw-record", "pw-play", "systemctl")
    optional = ("adb", "gst-inspect-1.0", "pactl", "omarchy-shell")
    missing_required = [c for c in required if not ctx.which(c)]
    missing = [c for c in optional if not ctx.which(c)]
    if not ctx.exists(Path("/usr/lib/spa-0.2/aec/libspa-aec-webrtc.so")):
        missing.append("PipeWire WebRTC echo cancellation (pipewire-audio)")
    if (
        ctx.which("gst-inspect-1.0")
        and ctx.command("gst-inspect-1.0", "webrtcbin").returncode
    ):
        missing.append("GStreamer webrtcbin (gst-plugins-bad)")
    if ctx.command("/usr/bin/python3", "-c", "import gi").returncode:
        missing.append("python-gobject")
    if missing_required:
        return Result(
            "dependencies",
            "Native dependencies",
            RED,
            f"missing: {', '.join(missing_required + missing)}",
            _DEPENDENCY_FIX,
        )
    if missing:
        return Result(
            "dependencies",
            "Native dependencies",
            YELLOW,
            f"missing (casting, echo cancellation or the bar): {', '.join(missing)}",
            _DEPENDENCY_FIX,
        )
    return Result("dependencies", "Native dependencies", GREEN, "all present")


def _state_files(state_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in ("*.json", "*.yaml", "tasks/*.json"):
        files += [p for p in state_dir.glob(pattern) if p.is_file()]
    return sorted(files)


def check_state_files(ctx: Context) -> Result:
    from ..core import statefile

    bad = []
    files = _state_files(ctx.state_dir)
    for path in files:
        _, error = _read_state(path)
        if error:
            bad.append(f"{path.relative_to(ctx.state_dir)} ({error[:80]})")
    quarantined = statefile.corrupt_files(ctx.state_dir)
    details = {
        "checked": len(files),
        "corrupt": bad,
        "quarantined": [str(p.relative_to(ctx.state_dir)) for p in quarantined],
    }
    if bad:
        return Result(
            "state_files",
            "State files",
            RED,
            f"{len(bad)} do not parse: {'; '.join(bad[:3])}",
            "the daemon sets a corrupt file aside on its next read and starts a new one; "
            "restore it from the .corrupt- copy if you need what was in it",
            details,
        )
    if quarantined:
        names = ", ".join(p.name for p in quarantined[-3:])
        return Result(
            "state_files",
            "State files",
            YELLOW,
            f"{len(quarantined)} file(s) were set aside as corrupt (newest: {names})",
            f"look at them under {ctx.state_dir}, recover anything needed, then delete them",
            details,
        )
    return Result(
        "state_files",
        "State files",
        GREEN,
        f"{len(files)} valid, none quarantined",
        details=details,
    )


def check_pending_actions(ctx: Context) -> Result:
    from ..core import pending_actions as pa

    title = "Pending actions"
    data, error = _read_state(ctx.state_dir / "pending_actions.json")
    if error or (data is not None and not isinstance(data, dict)):
        return Result(
            "pending_actions",
            title,
            YELLOW,
            "pending_actions.json unreadable",
            "see the state files check",
        )
    actions = [a for a in (data or {}).values() if isinstance(a, dict)]
    stuck = [
        a for a in actions if a.get("status") == pa.RUNNING and pa._stuck(a, ctx.now)
    ]
    unsure = [a for a in actions if a.get("status") == pa.UNCERTAIN]
    waiting = [a for a in actions if a.get("status") == pa.WAITING]
    expired = [a for a in waiting if pa.expired(a, ctx.now)]
    details = {
        "waiting": len(waiting),
        "stuck": len(stuck),
        "uncertain": len(unsure),
        "expired": len(expired),
    }

    def names(items):
        return ", ".join(
            str(a.get("title") or a.get("method") or "?")[:50] for a in items[:3]
        )

    if stuck:
        return Result(
            "pending_actions",
            title,
            RED,
            f"{len(stuck)} stuck running (its process is gone): {names(stuck)}",
            "the daemon settles these from the effect journal on its next heartbeat (not in safe mode); "
            "then check whether each one went out",
            details,
        )
    if unsure or expired:
        parts = []
        if unsure:
            parts.append(f"{len(unsure)} may have been sent: {names(unsure)}")
        if expired:
            parts.append(f"{len(expired)} expired while waiting")
        return Result(
            "pending_actions",
            title,
            YELLOW,
            "; ".join(parts),
            "check whether it happened, then Resend or Drop on its card in the chat",
            details,
        )
    return Result(
        "pending_actions",
        title,
        GREEN,
        f"{len(waiting)} waiting for your OK, none stuck",
        details=details,
    )


def check_effects(ctx: Context) -> Result:
    from ..core import effects
    from ..runtime.task import owner_alive

    title = "Uncertain side effects"
    data, error = _read_state(ctx.state_dir / effects.INDEX_NAME)
    if error or (data is not None and not isinstance(data, dict)):
        return Result(
            "effects",
            title,
            YELLOW,
            "effects_index.json unreadable",
            "see the state files check",
        )
    entries = (data or {}).get("effects") or {}
    recent, interrupted, older = [], [], 0
    for entry in entries.values() if isinstance(entries, dict) else []:
        if not isinstance(entry, dict):
            continue
        state, ts = (
            entry.get("state"),
            float(entry.get("updated") or entry.get("ts") or 0),
        )
        if state in (effects.PROPOSED, effects.DISPATCHED) and not owner_alive(
            entry.get("owner")
        ):
            interrupted.append(entry)
        elif state == effects.UNCERTAIN:
            if ctx.now - ts <= RECENT_UNCERTAIN_SECONDS:
                recent.append(entry)
            else:
                older += 1
    details = {
        "uncertain_24h": len(recent),
        "uncertain_older": older,
        "interrupted": len(interrupted),
    }
    flagged = recent + [e for e in interrupted if entry_state(e) == effects.DISPATCHED]
    if flagged:
        text = "; ".join(
            f"{e.get('tool')} -> {str(e.get('target') or '-')[:40]} at {_when(e.get('ts'))} ({e.get('effect_id')})"
            for e in flagged[:3]
        )
        return Result(
            "effects",
            title,
            YELLOW,
            f"{len(flagged)} may have happened without confirmation: {text}",
            "check each at its target before repeating it; omarchy-ai-doctor --trace <effect_id> "
            "shows what led to it",
            details,
        )
    return Result(
        "effects",
        title,
        GREEN,
        "none in the last 24 h" + (f" ({older} older)" if older else ""),
        details=details,
    )


def entry_state(entry: dict) -> str:
    return str(entry.get("state") or "")


def check_tasks(ctx: Context) -> Result:
    from ..runtime import task as rt

    title = "Tasks"
    store = rt.TaskStore(ctx.state_dir / "tasks")
    tasks = [
        t
        for t in store.list(50)
        if ctx.now - float(t.updated_at or 0) <= TASK_HORIZON_SECONDS
    ]
    interrupted = [t for t in tasks if t.status == rt.INTERRUPTED]
    orphaned = [
        t for t in tasks if t.status in rt.ACTIVE and not rt.owner_alive(t.owner)
    ]
    waiting = [t for t in tasks if t.status in (rt.WAITING_APPROVAL, rt.WAITING_USER)]
    running = [t for t in tasks if t.status in rt.ACTIVE and rt.owner_alive(t.owner)]
    details = {
        "interrupted": [t.id for t in interrupted],
        "orphaned": [t.id for t in orphaned],
        "waiting": [t.id for t in waiting],
        "running": len(running),
    }
    if interrupted or orphaned:
        ids = ", ".join(t.id for t in (interrupted + orphaned)[:4])
        return Result(
            "tasks",
            title,
            YELLOW,
            f"{len(interrupted)} interrupted, {len(orphaned)} orphaned (no live process): {ids}",
            "omarchy-ai-task show ID, then omarchy-ai-task resume ID or cancel ID",
            details,
        )
    note = f", {len(waiting)} waiting for you" if waiting else ""
    return Result(
        "tasks",
        title,
        GREEN,
        f"{len(running)} running{note}, none interrupted",
        details=details,
    )


def check_agenda(ctx: Context) -> Result:
    from ..core import agenda

    title = "Agenda runs"
    data, error = _read_state(ctx.state_dir / "agenda.json")
    if error or (data is not None and not isinstance(data, list)):
        return Result(
            "agenda",
            title,
            YELLOW,
            "agenda.json unreadable",
            "see the state files check",
        )
    jobs = [j for j in data or [] if isinstance(j, dict)]
    held, hung, stale = [], [], []
    for job in jobs:
        lease = job.get("running_by")
        if agenda._lease_held(job):
            held.append(job)
            continue
        if isinstance(lease, dict) and lease.get("pid") and job.get("started_at"):
            from ..runtime.task import owner_alive

            if owner_alive({"pid": lease["pid"], "start": lease.get("start_time")}):
                hung.append(job)  # alive past its lease
                continue
        if (
            job.get("started_at")
            and ctx.now - float(job["started_at"]) > agenda.STALE_RUN_SECONDS
        ):
            stale.append(job)
    details = {
        "jobs": len(jobs),
        "running": len(held),
        "hung": len(hung),
        "interrupted": len(stale),
    }

    def names(items):
        return ", ".join(str(j.get("title") or j.get("id"))[:40] for j in items[:3])

    if hung:
        return Result(
            "agenda",
            title,
            YELLOW,
            f"{len(hung)} run(s) past their {agenda.LEASE_SECONDS // 3600} h lease: "
            f"{names(hung)}",
            "list_scheduled_tasks / the routines HUD; cancel the run if it is stuck",
            details,
        )
    if stale:
        return Result(
            "agenda",
            title,
            YELLOW,
            f"{len(stale)} interrupted run(s) not yet reported: {names(stale)}",
            "the daemon reports them on its next heartbeat (not in safe mode); check whether they happened",
            details,
        )
    return Result(
        "agenda",
        title,
        GREEN,
        f"{len(jobs)} job(s), {len(held)} running now, none interrupted",
        details=details,
    )


def check_disk(ctx: Context) -> Result:
    target = ctx.state_dir
    while not target.exists() and target != target.parent:
        target = target.parent
    usage = shutil.disk_usage(target)
    free = usage.free
    details = {
        "free_bytes": free,
        "total_bytes": usage.total,
        "path": str(ctx.state_dir),
    }
    text = f"{free / 1024**3:.1f} GB free of {usage.total / 1024**3:.0f} GB"
    if free < LOW_DISK_BYTES:
        return Result(
            "disk",
            "Disk space",
            RED,
            f"{text} for the state dir: saves may fail",
            "free some space (journalctl --user --vacuum-size, old downloads, caches)",
            details,
        )
    if free < WARN_DISK_BYTES or free < usage.total * 0.05:
        return Result(
            "disk", "Disk space", YELLOW, f"{text}", "free some space soon", details
        )
    return Result("disk", "Disk space", GREEN, text, details=details)


def _pyproject_version(root: Path) -> str | None:
    import tomllib

    try:
        with (root / "pyproject.toml").open("rb") as source:
            return str(tomllib.load(source)["project"]["version"])
    except (OSError, KeyError, ValueError):
        return None


def check_version(ctx: Context) -> Result:
    """Version vs the latest release; scripts/diagnose-update.sh folded in
    (which install the service runs, the last update attempt)."""
    from ..core import updates

    title = "Version"
    try:
        current = (
            ctx.installed_version()
            if ctx.installed_version
            else updates.installed_version()
        )
    except (OSError, KeyError, ValueError):
        current = "?"
    details: dict[str, Any] = {"installed": current}
    problems, fixes = [], []
    workdir = ctx.unit().get("WorkingDirectory", "")
    if workdir:
        served = _pyproject_version(Path(workdir))
        details["service_version"] = served
        if served and current != "?" and served != current:
            problems.append(
                f"the service runs {served} from {workdir}, this copy is {current}"
            )
            fixes.append(
                f"systemctl --user cat {SERVICE} shows which install it starts"
            )
    install, _ = _read_state(ctx.state_dir / "updates" / "install.json")
    if isinstance(install, dict) and install.get("state") == "failed":
        problems.append(
            f"the last update failed: {str(install.get('message') or '')[:100]}"
        )
        fixes.append("get_update_status / the settings panel; retry the update")
    latest: str | None = None
    cache, _ = _read_state(ctx.state_dir / "updates" / "check.json")
    if (
        isinstance(cache, dict)
        and cache.get("ok")
        and ctx.now - float(cache.get("checked_at") or 0) < 86400
    ):
        latest = str(cache.get("latest_version") or "") or None
        details["latest_from"] = "cache"
    elif not ctx.quick:
        reply = ctx.http(
            LATEST_RELEASE_URL,
            {
                "User-Agent": "Omarchy-AI-Doctor",
                "Accept": "application/vnd.github+json",
            },
            HTTP_TIMEOUT,
        )
        if reply.status == 200:
            try:
                latest = (
                    str(json.loads(reply.body).get("tag_name") or "").lstrip("v")
                    or None
                )
                details["latest_from"] = "github"
            except (ValueError, AttributeError):
                latest = None
        else:
            details["latest_error"] = reply.error or f"HTTP {reply.status}"
    details["latest"] = latest
    try:
        newer = bool(latest) and updates.version_tuple(latest) > updates.version_tuple(
            current
        )
    except ValueError:
        newer = False
    if newer:
        problems.append(f"{latest} is available")
        fixes.append("ask the assistant to update itself, or use Assistant Settings")
    if problems:
        return Result(
            "version",
            title,
            YELLOW,
            f"{current}: {'; '.join(problems)}",
            "; ".join(fixes),
            details,
        )
    if latest is None:
        note = (
            "latest not checked (--quick)"
            if ctx.quick
            else "could not check the latest release"
        )
        return Result(
            "version",
            title,
            GREEN if ctx.quick else YELLOW,
            f"{current}; {note}",
            "" if ctx.quick else "check the network; get_update_status",
            details,
        )
    return Result(
        "version",
        title,
        GREEN,
        f"{current}, up to date (latest {latest})",
        details=details,
    )


CHECKS: list[tuple[str, Callable[[Context], Result]]] = [
    ("config", check_config),
    ("api_keys", check_api_keys),
    ("gateway", check_gateway),
    ("myapi", check_myapi),
    ("quota", check_quota),
    ("provider_health", check_provider_health),
    ("safe_mode", check_safe_mode),
    ("systemd", check_systemd),
    ("pipewire", check_pipewire),
    ("microphone", check_microphone),
    ("speaker", check_speaker),
    ("wake_model", check_wake_model),
    ("desktop", check_desktop),
    ("sandbox", check_sandbox),
    ("dependencies", check_dependencies),
    ("state_files", check_state_files),
    ("pending_actions", check_pending_actions),
    ("effects", check_effects),
    ("tasks", check_tasks),
    ("agenda", check_agenda),
    ("disk", check_disk),
    ("version", check_version),
]


def run_checks(
    ctx: Context | None = None, only: list[str] | None = None
) -> list[Result]:
    """Every check (or `only` these ids), in CHECKS order. A check that
    raises or hangs is itself a red result, never the end of the run."""
    ctx = ctx or Context.default()
    chosen = [(cid, fn) for cid, fn in CHECKS if not only or cid in only]
    ctx.cfg()  # load once before the threads share it
    results: dict[str, Result] = {}
    pool = concurrent.futures.ThreadPoolExecutor(
        max_workers=8, thread_name_prefix="doctor"
    )
    try:
        futures = {cid: pool.submit(fn, ctx) for cid, fn in chosen}
        deadline = time.monotonic() + CHECK_TIMEOUT
        for cid, future in futures.items():
            try:
                results[cid] = future.result(
                    timeout=max(0.1, deadline - time.monotonic())
                )
            except concurrent.futures.TimeoutError:
                results[cid] = Result(
                    cid,
                    cid,
                    RED,
                    f"the check did not finish within {CHECK_TIMEOUT:g} s",
                    "run omarchy-ai-doctor again; a hung command or network call is the cause",
                )
            except Exception as exc:  # noqa: BLE001 -- one broken check must not hide the rest
                results[cid] = Result(
                    cid,
                    cid,
                    RED,
                    f"the check itself failed: {type(exc).__name__}: {exc}"[:200],
                    "report it (omarchy-ai-doctor --bundle)",
                )
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return [results[cid] for cid, _ in chosen]


def _version() -> str:
    try:
        from ..core import updates

        return str(updates.installed_version())
    except Exception:  # noqa: BLE001
        return "?"


def report(results: list[Result]) -> dict[str, Any]:
    """The --json document."""
    counts = {s: sum(r.status == s for r in results) for s in (RED, YELLOW, GREEN)}
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": datetime.datetime.now(datetime.timezone.utc).isoformat(
            timespec="seconds"
        ),
        "omarchy_ai_version": _version(),
        "status": _worst([r.status for r in results]),
        "counts": counts,
        "checks": [dataclasses.asdict(r) for r in results],
    }


def ordered(results: list[Result]) -> list[Result]:
    return sorted(results, key=lambda r: _RANK[r.status])


def summary(results: list[Result], limit: int = 1200) -> str:
    """The run_diagnostics answer: counts, then reds and yellows with their
    fixes, then the green ids. Short: it goes to the model."""
    counts = {s: sum(r.status == s for r in results) for s in (RED, YELLOW, GREEN)}
    lines = [
        f"Diagnostics: {counts[RED]} red, {counts[YELLOW]} yellow, {counts[GREEN]} green."
    ]
    for r in ordered(results):
        if r.status == GREEN:
            continue
        line = f"{r.status.upper()} {r.id}: {r.reason}"
        if r.fix:
            line += f" Fix: {r.fix}"
        lines.append(line[:300])
    greens = [r.id for r in results if r.status == GREEN]
    if greens:
        lines.append("Green: " + ", ".join(greens) + ".")
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 3] + "..."


_COLORS = {RED: "\033[31m", YELLOW: "\033[33m", GREEN: "\033[32m"}


def render(results: list[Result], color: bool = False) -> str:
    def tag(status: str) -> str:
        word = f"{status.upper():<6}"
        return f"{_COLORS[status]}{word}\033[0m" if color else word

    width = max((len(r.title) for r in results), default=10)
    lines = [f"Omarchy AI doctor  {_version()}  {time.strftime('%Y-%m-%d %H:%M')}", ""]
    for r in ordered(results):
        lines.append(f"  {tag(r.status)}  {r.title:<{width}}  {r.reason}")
        if r.fix and r.status != GREEN:
            lines.append(f"  {'':6}  {'':<{width}}  fix: {r.fix}")
    counts = {s: sum(r.status == s for r in results) for s in (RED, YELLOW, GREEN)}
    lines += ["", f"{counts[RED]} red, {counts[YELLOW]} yellow, {counts[GREEN]} green"]
    return "\n".join(lines)


# ------------------------------------------------------------------- trace


def _epoch(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return datetime.datetime.fromisoformat(str(value)).timestamp()
    except ValueError:
        return 0.0


def _log_files(state_dir: Path) -> list[Path]:
    from ..core import logs

    directory = state_dir / "logs"
    stem, suffix = Path(logs.FILE_NAME).stem, Path(logs.FILE_NAME).suffix
    rolled = sorted(directory.glob(f"{stem}.*{suffix}"))
    current = directory / logs.FILE_NAME
    return [p for p in rolled if p != current] + ([current] if current.exists() else [])


def _ids(record: dict) -> dict[str, str]:
    """The correlation IDs a record carries: a log line's own fields, or an
    effect record's `log` block plus its effect_id."""
    from ..core import logs

    found = {k: str(record[k]) for k in logs.FIELDS if record.get(k)}
    nested = record.get("log")
    if isinstance(nested, dict):
        for k in logs.FIELDS:
            if nested.get(k) and k not in found:
                found[k] = str(nested[k])
    return found


def trace(trace_id: str, state_dir: Path | None = None) -> list[dict]:
    """Every log and effect record in the chain of `trace_id` (a session,
    turn, tool call, task, step, effect or run id), oldest first.

    A record matches when it carries the id, or an id below it in the chain
    (FIELDS order: session -> turn -> tool call -> task -> step -> effect) that
    a matching record introduced: a turn pulls in its tool calls, and a tool
    call the task it started and that task's steps."""
    from .. import config
    from ..core import effects, logs, statefile

    state_dir = state_dir or config.STATE_DIR
    records: list[dict] = []
    for path in _log_files(state_dir):
        for row in statefile.read_jsonl(path):
            if isinstance(row, dict):
                records.append({**row, "_source": "log"})
    for row in statefile.read_jsonl(state_dir / effects.JOURNAL_NAME):
        if isinstance(row, dict):
            records.append({**row, "_source": "effect"})
    order = {name: i for i, name in enumerate(logs.FIELDS)}
    wanted: dict[str, int] = {}  # id -> the deepest level it was found at
    for record in records:
        for name, value in _ids(record).items():
            if value == trace_id:
                wanted[value] = min(wanted.get(value, 99), order[name])
    picked: dict[int, dict] = {}
    changed = bool(wanted)
    while changed:
        changed = False
        for index, record in enumerate(records):
            ids = _ids(record)
            hits = [
                order[n]
                for n, v in ids.items()
                if v in wanted and order[n] >= wanted[v]
            ]
            if not hits:
                continue
            picked[index] = record
            level = min(hits)
            for name, value in ids.items():
                if order[name] > level and value not in wanted:
                    wanted[value] = order[name]
                    changed = True
    rows = sorted(picked.values(), key=lambda r: _epoch(r.get("ts")))
    return rows


def render_trace(trace_id: str, rows: list[dict]) -> str:
    if not rows:
        return f"No log or effect record carries {trace_id}."
    lines = [f"Trace {trace_id}: {len(rows)} record(s)"]
    for row in rows:
        ts = _epoch(row.get("ts"))
        when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "?"
        ids = " ".join(f"{k.removesuffix('_id')}={v}" for k, v in _ids(row).items())
        if row.get("_source") == "effect":
            text = f"effect {row.get('tool')} -> {row.get('target')} [{row.get('state')}] {row.get('effect_id')}"
        else:
            text = (
                f"{row.get('level', '')} {row.get('logger', '')}: {row.get('msg', '')}"
            )
            metrics = [
                f"{k}={row[k]}"
                for k in ("tool", "outcome", "duration_ms", "error_class")
                if k in row
            ]
            if metrics:
                text += "  (" + ", ".join(metrics) + ")"
        lines.append(f"{when}  {text[:400]}  [{ids}]")
    return "\n".join(lines)


# ------------------------------------------------------------------- bundle


def _secret_values(ctx: Context) -> list[str]:
    """Local secrets to mask in the bundle: the API key files and the MyApi
    identity. Read only from disk (no keyring, no vault fetch)."""
    from ..execution import passwords

    values: list[str] = []
    try:
        values += passwords._key_values()
    except Exception:  # noqa: BLE001
        pass
    for path in sorted(ctx.config_dir.glob("*key*")):
        try:
            if path.is_file() and path.stat().st_size <= 8192:
                value = path.read_text(errors="replace").strip()
                if len(value) >= 8:
                    values.append(value)
        except OSError:
            continue
    identity, _ = _read_state(ctx.config_dir / "myapi" / "identity.json")
    if isinstance(identity, dict):
        values += [
            str(v) for v in identity.values() if isinstance(v, str) and len(v) >= 8
        ]
    return sorted(set(values), key=len, reverse=True)


def _mask(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "[redacted]")
        escaped = json.dumps(secret)[1:-1]
        if escaped != secret:
            text = text.replace(escaped, "[redacted]")
    return text


def redacted_config(data: Any) -> Any:
    """config.yaml without anything that holds a secret."""
    if isinstance(data, dict):
        return {
            k: (
                "[redacted]"
                if _SECRET_KEY.search(str(k)) and not str(k).endswith("_path")
                else redacted_config(v)
            )
            for k, v in data.items()
        }
    if isinstance(data, list):
        return [redacted_config(v) for v in data]
    return data


def bundle(
    path: Path | None = None,
    ctx: Context | None = None,
    results: list[Result] | None = None,
) -> Path:
    """Write the redacted support tarball; returns its path."""
    ctx = ctx or Context.default()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = Path(path) if path else Path.home() / f"omarchy-ai-support-{stamp}.tar.gz"
    if path.is_dir():
        path = path / f"omarchy-ai-support-{stamp}.tar.gz"
    secrets = _secret_values(ctx)
    results = results if results is not None else run_checks(ctx)
    members: list[tuple[str, bytes]] = []

    def add(name: str, text: str) -> None:
        members.append((name, _mask(text, secrets).encode("utf-8")))

    add(
        "doctor.json",
        json.dumps(report(results), indent=1, ensure_ascii=False, default=str),
    )
    data, error = ctx.raw_config()
    if data is None:
        add("config.yaml", f"# config.yaml did not parse ({error}); not included\n")
    else:
        add(
            "config.yaml",
            yaml.safe_dump(redacted_config(data), allow_unicode=True, sort_keys=False),
        )
    unit = ctx.unit()
    add(
        "versions.txt",
        "\n".join(
            [
                f"omarchy-ai {_version()}",
                f"service WorkingDirectory {unit.get('WorkingDirectory', '?')}",
                f"python {platform.python_version()} ({sys.executable})",
                f"platform {platform.platform()}",
                f"created {datetime.datetime.now().isoformat(timespec='seconds')}",
                "",
            ]
        ),
    )
    cutoff = ctx.now - BUNDLE_LOG_DAYS * 86400
    budget = BUNDLE_LOG_BYTES
    for log_path in reversed(_log_files(ctx.state_dir)):
        try:
            if log_path.stat().st_mtime < cutoff:
                continue
            raw = log_path.read_bytes()
        except OSError:
            continue
        if len(raw) > budget:
            raw = raw[-budget:]
            raw = raw[raw.find(b"\n") + 1 :]
        budget -= len(raw)
        add(f"logs/{log_path.name}", raw.decode("utf-8", "replace"))
        if budget <= 0:
            break
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with (
        os.fdopen(fd, "wb") as stream,
        tarfile.open(fileobj=stream, mode="w:gz") as tar,
    ):
        for name, payload in members:
            info = tarfile.TarInfo(f"omarchy-ai-support/{name}")
            info.size, info.mtime, info.mode = len(payload), int(ctx.now), 0o600
            tar.addfile(info, io.BytesIO(payload))
    return path


# --------------------------------------------------------------------- CLI


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="omarchy-ai-doctor",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--json", action="store_true", help="print JSON (with --trace too)"
    )
    parser.add_argument("--quick", action="store_true", help="skip the network checks")
    parser.add_argument(
        "--trace",
        metavar="ID",
        help="reassemble one turn, task, tool call or effect from the logs",
    )
    parser.add_argument(
        "--bundle",
        nargs="?",
        const="",
        metavar="PATH",
        help="write a redacted support tarball",
    )
    parser.add_argument(
        "--leaked",
        nargs="*",
        metavar="SOURCE",
        help="what to rotate when a secret may have leaked (sudo, spoken, vault, api_key)",
    )
    args = parser.parse_args(argv)

    if args.leaked is not None:
        from ..execution import passwords

        print(passwords.rotate_on_leak(args.leaked or None))
        return 0
    if args.trace:
        rows = trace(args.trace)
        if args.json:
            print(
                json.dumps(
                    {"trace_id": args.trace, "records": rows},
                    indent=1,
                    ensure_ascii=False,
                    default=str,
                )
            )
        else:
            print(render_trace(args.trace, rows))
        return 0 if rows else 1
    ctx = Context.default(quick=args.quick)
    results = run_checks(ctx)
    if args.bundle is not None:
        written = bundle(Path(args.bundle) if args.bundle else None, ctx, results)
        print(f"Wrote {written} (redacted; check it before sharing)")
        return 0
    if args.json:
        print(json.dumps(report(results), indent=1, ensure_ascii=False, default=str))
    else:
        print(render(results, color=sys.stdout.isatty()))
    return 1 if any(r.status == RED for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
