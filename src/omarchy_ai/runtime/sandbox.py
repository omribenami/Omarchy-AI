"""Second layer for executed commands: a bubblewrap sandbox chosen by risk.

The first layer stays as it is: runtime/permissions.py classifies every
command (and denies or asks), and the environment is scrubbed of secrets.
This module turns the risk level a command was allowed at into a `bwrap`
argv, so a command that slipped past the classifier (obfuscated, or a
script whose contents it never saw) still cannot read private keys or the
assistant's own secrets, or write outside the paths its level allows.

Policy by level (`for_command`):

- LOW / NORMAL: `/` read-only. Writable: the task workspace (the whole home
  when the workspace is the home directory, as for voice tasks), the
  working directory and `cd` targets under home, /tmp, /var/tmp, per-user
  caches and toolchains (~/.cache, ~/.local/share, ~/.local/state, ~/.cargo,
  ~/.npm, ...), and every path the classifier saw the command write
  (Assessment.writes). Read-only even then: ~/.config, ~/.local/bin,
  ~/.local/share/applications, shell rc files, the assistant's state
  directory and its installed tools. Hidden (empty): ~/.ssh, ~/.gnupg,
  ~/.password-store, keyring files, browser profiles and the assistant's
  key files and MyApi identity. $XDG_RUNTIME_DIR is an empty tmpfs, so no
  session socket (Wayland, Hyprland, PipeWire, D-Bus, keyring, gpg-agent,
  the assistant's control socket) is reachable unless granted below.
  Network: shared with the host unless the policy says otherwise.
- ELEVATED: the same, plus the approved write targets inside the
  read-only areas (a config file the user approved changing).
- HIGH: no sandbox -- it only runs after a human tap.

Per-program grants (the plan's allowlist): a command that runs one of these
programs gets only what that program needs. ssh/scp/rsync/sshpass/git see
~/.ssh read-only; gpg/pass get ~/.gnupg and the agent socket; the MyApi
vault gets its identity, the keyring over a filtered D-Bus and its runtime
directory. Desktop programs get the specific sockets listed in SOCKETS.

User tools (`for_tool`): read-only system and home, the MyApi vault (the
documented way a tool gets its secrets), private writes to /tmp and
~/.cache, network. A tool.json "sandbox" field widens this: {"needs":
["assistant_keys", "desktop"]} or false (no sandbox).

Without bwrap (or without user namespaces) commands run as before and a
warning is logged once. `sandbox_commands: false` in config.yaml turns the
sandbox off.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
import functools
import logging
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tempfile
from typing import Iterator

from .permissions import Risk, _substitutions, _tokens, split_commands, unwrap

log = logging.getLogger(__name__)

MARKER_ENV = "OMARCHY_AI_SANDBOX"

# Paths below are relative to the home directory (resolved per call).
# Hidden at every sandboxed level unless a program grant exposes them.
BROWSER_PROFILES = (
    ".config/chromium",
    ".config/google-chrome",
    ".config/google-chrome-beta",
    ".config/google-chrome-unstable",
    ".config/BraveSoftware",
    ".config/microsoft-edge",
    ".config/vivaldi",
    ".config/opera",
    ".config/thorium",
    ".mozilla",
    ".librewolf",
    ".zen",
    ".floorp",
    ".waterfox",
)
SECURITY = (".ssh", ".gnupg", ".password-store", ".local/share/keyrings", ".pki")

# Writable at LOW/NORMAL besides the workspace: caches and per-user toolchains
# that ordinary builds and package managers write without naming them.
USER_WRITABLE = (
    ".cache",
    ".local/share",
    ".local/state",
    ".cargo",
    ".rustup",
    ".npm",
    ".bun",
    ".gradle",
    ".m2",
    "go",
    ".pnpm-store",
    ".yarn",
)

# Read-only even inside a writable home (permissions._CONFIG_DIRS: writing
# them is ELEVATED; the tools directory and the assistant's state are never
# a command's to write).
PROTECTED = (
    ".config",
    ".local/bin",
    ".local/share/applications",
    ".bashrc",
    ".zshrc",
    ".profile",
    ".bash_profile",
    ".bash_login",
    ".zprofile",
    ".zshenv",
    ".xprofile",
)

# Writable although under ~/.config: Omarchy's ~/.local/bin wrappers (gh, uv,
# claude, codex...) run `mise use -g` on every call, which rewrites this.
TOOLCHAIN_CONFIG = (".config/mise",)

# Desktop programs and the session sockets they need. The sockets live in
# $XDG_RUNTIME_DIR, which is otherwise an empty tmpfs.
#   wayland  : $XDG_RUNTIME_DIR/$WAYLAND_DISPLAY
#   hyprland : $XDG_RUNTIME_DIR/hypr/$HYPRLAND_INSTANCE_SIGNATURE/ (.socket.sock, .socket2.sock)
#   pipewire : $XDG_RUNTIME_DIR/pipewire-0, pipewire-0-manager, pulse/native (+ the
#              bus filtered to the realtime portal / RealtimeKit)
#   dbus     : the session bus at $XDG_RUNTIME_DIR/bus, filtered through
#              xdg-dbus-proxy to the names below when a program lists them,
#              the raw bus when it lists none
#   uwsm     : $XDG_RUNTIME_DIR/uwsm-app-daemon-in/-out and uwsm/ (app launches)
_WAYLAND = {"wayland"}
_PIPEWIRE = {"pipewire"}
SOCKETS: dict[str, tuple[set[str], frozenset[str]]] = {
    **{
        p: (_WAYLAND, frozenset())
        for p in (
            "grim",
            "slurp",
            "wl-copy",
            "wl-paste",
            "wtype",
            "hyprpicker",
            "wlr-randr",
            "wayshot",
            "satty",
            "swappy",
            "cliphist",
            "wf-recorder",
            "wl-screenrec",
        )
    },
    "hyprctl": ({"hyprland"}, frozenset()),
    "hyprshot": ({"wayland", "hyprland"}, frozenset()),
    # PipeWire clients also ask the session bus for realtime scheduling.
    **{
        p: (
            _PIPEWIRE | {"dbus"},
            frozenset(
                {"org.freedesktop.portal.Desktop", "org.freedesktop.RealtimeKit1"}
            ),
        )
        for p in (
            "wpctl",
            "pw-cli",
            "pw-dump",
            "pw-top",
            "pw-metadata",
            "pw-play",
            "pw-record",
            "pw-cat",
            "pw-link",
            "pactl",
            "pamixer",
            "paplay",
            "parec",
            "pacmd",
            "pw-mon",
        )
    },
    "notify-send": ({"dbus"}, frozenset({"org.freedesktop.Notifications"})),
    "makoctl": (
        {"dbus"},
        frozenset({"org.freedesktop.Notifications", "fr.emersion.Mako"}),
    ),
    "playerctl": ({"dbus"}, frozenset({"org.mpris.MediaPlayer2.*"})),
    **{
        p: ({"dbus"}, frozenset())
        for p in ("gdbus", "busctl", "dbus-send", "gsettings", "dconf")
    },
    **{
        p: ({"wayland", "hyprland", "dbus", "uwsm"}, frozenset())
        for p in ("uwsm", "uwsm-app", "xdg-open", "gtk-launch", "gio")
    },
}
# Every omarchy-* helper: the whole session, plus its own config and state.
_OMARCHY_SOCKETS = {"wayland", "hyprland", "pipewire", "dbus", "uwsm"}
_OMARCHY_WRITABLE = (".config/omarchy", ".local/state/omarchy")

_SSH_PROGRAMS = {
    "ssh",
    "scp",
    "sftp",
    "rsync",
    "sshpass",
    "ssh-copy-id",
    "ssh-keyscan",
    "git",
}
_GPG_PROGRAMS = {"gpg", "gpg2", "gpgconf", "pass"}
_VAULT_PROGRAMS = {"omarchy-ai-vault", "secret-tool"}
_TOOLS_CLI = re.compile(r"^(?:\S*/)?omarchy-ai-tool$")
_TOOLS_MODULE = re.compile(r"^(?:\S*/)?python3?(?:\.\d+)?$")


@dataclass
class Policy:
    level: Risk
    writable: list[Path] = field(default_factory=list)
    # Writable inside the read-only areas: approved write targets (ELEVATED
    # only) and TOOLCHAIN_CONFIG.
    approved: list[Path] = field(default_factory=list)
    readonly: list[Path] = field(default_factory=list)
    hidden: list[Path] = field(default_factory=list)
    sockets: set[str] = field(default_factory=set)
    # None: the raw session bus (when "dbus" is in sockets); names: filtered.
    dbus_talk: set[str] | None = None
    # Extra binds after the hidden ones: (path, writable).
    expose: list[tuple[Path, bool]] = field(default_factory=list)
    network: bool = True
    label: str = ""

    def describe(self) -> str:
        return (
            f"{self.label or self.level.name}: writable {', '.join(map(str, self.writable + self.approved)) or '-'}; "
            f"sockets {', '.join(sorted(self.sockets)) or '-'}; network {'on' if self.network else 'off'}"
        )


# --------------------------------------------------------------------- setup
def enabled() -> bool:
    try:
        from ..config import load_config

        return bool(getattr(load_config(), "sandbox_commands", True))
    except Exception:  # noqa: BLE001 -- a broken config must not turn this off silently
        return True


@functools.lru_cache(maxsize=1)
def available() -> bool:
    """bwrap is installed and can create a sandbox here (user namespaces)."""
    bwrap = shutil.which("bwrap")
    if not bwrap:
        log.warning(
            "Sandbox: bwrap is not installed; commands run unsandboxed (install bubblewrap)"
        )
        return False
    try:
        done = subprocess.run(
            [bwrap, "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "true"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("Sandbox: bwrap could not run (%s); commands run unsandboxed", exc)
        return False
    if done.returncode:
        log.warning(
            "Sandbox: bwrap cannot create a sandbox here (%s); commands run unsandboxed",
            done.stderr.strip()[:200],
        )
        return False
    return True


def assistant_secrets() -> list[Path]:
    """The assistant's own key files and identities (config.py)."""
    from .. import config

    found = [
        config.OMARCHY_KEY_PATH,
        config.GEMINI_KEY_PATH,
        config.VERCEL_GATEWAY_KEY_PATH,
        config.LEGACY_KEY_PATH,
        config.MYAPI_DIR,
        config.CONFIG_DIR / "phone_bridge",
        config.CONFIG_DIR / "approval_pin.json",
    ]
    with contextlib.suppress(OSError):
        found += [p for p in config.CONFIG_DIR.glob("*key*") if p.is_file()]
    return list(dict.fromkeys(found))


def _assistant_keys() -> list[Path]:
    from .. import config

    return [
        p
        for p in assistant_secrets()
        if p
        not in (
            config.MYAPI_DIR,
            config.CONFIG_DIR / "phone_bridge",
            config.CONFIG_DIR / "approval_pin.json",
        )
    ]


def _protected_assistant() -> list[Path]:
    from .. import config

    return [config.STATE_DIR, config.CONFIG_DIR / "tools"]


# ------------------------------------------------------------------ programs
def programs(command: str) -> set[str]:
    """Every program name `command` runs (wrappers, `bash -c`, $(...), xargs, find -exec)."""
    found: set[str] = set()

    def visit(text: str, depth: int = 0) -> None:
        if depth > 4:
            return
        for inner in _substitutions(text):
            visit(inner, depth + 1)
        for sub in split_commands(text):
            raw = _tokens(
                re.sub(r"(?:[0-9]|&)?>>?\|?\s*[^\s;&|<>]+|<\s*[^\s;&|<>]+", " ", sub)
            )
            tokens, wrappers = unwrap(raw)
            found.update(wrappers)
            if not tokens:
                continue
            word = os.path.basename(tokens[0])
            found.add(word)
            args = tokens[1:]
            if (
                word in ("bash", "sh", "zsh", "dash")
                and "-c" in args
                and args.index("-c") + 1 < len(args)
            ):
                visit(args[args.index("-c") + 1], depth + 1)
            elif word == "xargs":
                rest = [a for a in args if not a.startswith("-")]
                if rest:
                    found.add(os.path.basename(rest[0]))
            elif word in ("find", "fd"):
                for flag in (
                    "-exec",
                    "-execdir",
                    "-ok",
                    "-okdir",
                    "-x",
                    "--exec",
                    "-X",
                    "--exec-batch",
                ):
                    if flag in args and args.index(flag) + 1 < len(args):
                        found.add(os.path.basename(args[args.index(flag) + 1]))
            elif _TOOLS_MODULE.match(word) and "omarchy_ai.cli.tools" in args:
                found.add("omarchy-ai-tool")

    visit(command)
    return found


def tools_cli_only(command: str) -> bool:
    """The whole command is `omarchy-ai-tool ...` (optionally fed by echo/printf):
    the CLI sandboxes the tool it runs itself (for_tool)."""
    seen = False
    for sub in split_commands(command):
        tokens, _ = unwrap(_tokens(sub))
        if not tokens:
            continue
        word = tokens[0]
        if _TOOLS_CLI.match(word) or (
            _TOOLS_MODULE.match(word) and tokens[1:3] == ["-m", "omarchy_ai.cli.tools"]
        ):
            seen = True
        elif os.path.basename(word) not in ("echo", "printf"):
            return False
    return seen and not _substitutions(command)


def _cd_targets(command: str, cwd: Path) -> list[Path]:
    targets = []
    for sub in split_commands(command):
        tokens = _tokens(sub)
        if len(tokens) == 2 and tokens[0] in ("cd", "pushd"):
            path = Path(os.path.expandvars(os.path.expanduser(tokens[1])))
            targets.append(path if path.is_absolute() else cwd / path)
    return targets


# ------------------------------------------------------------------ policies
def _at(home: Path, relative) -> list[Path]:
    return [home / p for p in relative]


def _under(path: Path, roots) -> bool:
    return any(path == r or r in path.parents for r in roots)


def _existing(path: Path) -> Path:
    """`path`, or its nearest existing ancestor, as a directory to bind."""
    path = Path(os.path.normpath(path))
    if path.is_dir():
        return path
    path = path.parent
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _system(path: Path) -> bool:
    from .permissions import _SYSTEM_DIRS

    return str(path).startswith(_SYSTEM_DIRS) or path == Path("/")


def for_command(
    command: str,
    risk: Risk,
    *,
    cwd: str | Path,
    workspace: str | Path | None,
    writes: list[Path] | tuple[Path, ...] = (),
    network: bool = True,
) -> Policy | None:
    """The sandbox for a Task Runtime command allowed at `risk`, or None (run unsandboxed)."""
    if risk >= Risk.HIGH:
        return None
    if tools_cli_only(command):
        return None
    names = programs(command)
    cwd = Path(cwd).expanduser()
    home = Path.home()
    hidden = [*_at(home, SECURITY), *_at(home, BROWSER_PROFILES), *assistant_secrets()]
    protected_home = _at(home, PROTECTED)
    protected = [*protected_home, *_protected_assistant()]
    policy = Policy(level=risk, network=network, label=f"command at {risk.name}")
    off_limits = protected + hidden

    writable: list[Path] = [Path("/tmp"), Path("/var/tmp"), *_at(home, USER_WRITABLE)]
    ws = Path(workspace).expanduser() if workspace else None
    if ws is not None and (_system(ws) or _under(ws, off_limits)):
        ws = None
    if ws is not None:
        writable.append(ws)
    for place in (cwd, *_cd_targets(command, cwd)):
        if (
            home in place.parents or (ws is not None and _under(place, [ws]))
        ) and not _under(place, off_limits):
            writable.append(place)
    for target in writes:
        target = Path(target)
        if _under(target, hidden) or _under(target, _protected_assistant()):
            continue
        if not (
            _under(target, [home, Path("/tmp"), Path("/var/tmp")])
            or (ws is not None and _under(target, [ws]))
        ):
            continue  # outside home: the classifier made it HIGH, never sandboxed here
        place = _existing(target)
        if _under(place, protected_home):
            if risk >= Risk.ELEVATED:
                policy.approved.append(place)
            continue
        writable.append(place)

    expose: list[tuple[Path, bool]] = []
    sockets: set[str] = set()
    talk: set[str] = set()
    raw_bus = False
    user_scope = "--user" in command
    for name in names:
        if name in SOCKETS:
            needs, bus_names = SOCKETS[name]
            sockets |= needs
            if "dbus" in needs:
                raw_bus = raw_bus or not bus_names
                talk |= bus_names
        elif name.startswith("omarchy-") and not name.startswith("omarchy-ai"):
            sockets |= _OMARCHY_SOCKETS
            raw_bus = True
            writable += [
                p for p in _at(home, _OMARCHY_WRITABLE) if not _under(p, hidden)
            ]
        if name in ("systemctl", "systemd-run", "journalctl", "busctl") and user_scope:
            sockets.add("dbus")
            raw_bus = True
    if names & _SSH_PROGRAMS:
        hidden = [p for p in hidden if p != home / ".ssh"]
        expose.append((home / ".ssh", False))
    if names & _GPG_PROGRAMS:
        hidden = [p for p in hidden if p != home / ".gnupg"]
        expose.append((home / ".gnupg", True))
        sockets.add("gnupg")
        if "pass" in names:
            hidden = [p for p in hidden if p != home / ".password-store"]
            expose.append((home / ".password-store", True))
    if names & _VAULT_PROGRAMS or "omarchy-ai-tool" in names:
        hidden = _grant_vault(hidden, expose, sockets)
        talk.add("org.freedesktop.secrets")

    policy.writable = _dedupe(
        [p for p in writable if p.exists() and not _under(p, hidden)]
    )
    policy.readonly = [p for p in protected if p.exists()]
    policy.approved = _dedupe(
        [p for p in (*policy.approved, *_at(home, TOOLCHAIN_CONFIG)) if p.exists()]
    )
    policy.hidden = [p for p in dict.fromkeys(hidden) if p.exists() or p.is_symlink()]
    policy.expose = [(p, w) for p, w in expose if p.exists()]
    policy.sockets = sockets
    policy.dbus_talk = None if raw_bus else talk
    return policy


def _grant_vault(
    hidden: list[Path], expose: list[tuple[Path, bool]], sockets: set[str]
) -> list[Path]:
    """The MyApi vault (myapi/vault.py): its identity, the keyring cache over
    D-Bus (org.freedesktop.secrets) and its record of revealed values in the
    runtime directory. Returns `hidden` without the identity."""
    from .. import config

    expose.append((config.MYAPI_DIR, False))
    expose.append((config.RUNTIME_DIR, True))
    sockets.add("dbus")
    return [p for p in hidden if p != config.MYAPI_DIR]


def for_tool(folder: Path, spec: dict | None) -> Policy | None:
    """The sandbox for an installed (or proposed) user tool, or None when its
    tool.json opts out with "sandbox": false."""
    setting = (spec or {}).get("sandbox", True)
    if setting is False or (
        isinstance(setting, str) and setting.lower() in ("off", "none", "false")
    ):
        return None
    needs = set(setting.get("needs") or []) if isinstance(setting, dict) else set()
    network = bool(setting.get("network", True)) if isinstance(setting, dict) else True
    home = Path.home()
    hidden = [*_at(home, SECURITY), *_at(home, BROWSER_PROFILES), *assistant_secrets()]
    expose: list[tuple[Path, bool]] = [(Path(folder), False)]
    sockets: set[str] = set()
    policy = Policy(
        level=Risk.NORMAL, network=network, label=f"tool {Path(folder).name}"
    )
    hidden = _grant_vault(hidden, expose, sockets)
    talk: set[str] | None = {"org.freedesktop.secrets"}
    if "assistant_keys" in needs:
        keys = _assistant_keys()
        hidden = [p for p in hidden if p not in keys]
    if "desktop" in needs:
        sockets |= {"wayland", "hyprland", "pipewire", "dbus"}
        talk = None
    policy.writable = _dedupe(
        [p for p in (Path("/tmp"), Path("/var/tmp"), home / ".cache") if p.exists()]
    )
    policy.hidden = [p for p in dict.fromkeys(hidden) if p.exists() or p.is_symlink()]
    policy.expose = [(p, w) for p, w in expose if p.exists()]
    policy.sockets = sockets
    policy.dbus_talk = talk
    return policy


def _dedupe(paths: list[Path]) -> list[Path]:
    """Unique, shallow first, without paths already inside another writable one."""
    out: list[Path] = []
    for path in sorted(dict.fromkeys(paths), key=lambda p: len(p.parts)):
        if not any(o == path or o in path.parents for o in out):
            out.append(path)
    return out


# ---------------------------------------------------------------------- argv
def _runtime_dir(env: dict) -> Path | None:
    raw = env.get("XDG_RUNTIME_DIR") or os.environ.get("XDG_RUNTIME_DIR")
    return Path(raw) if raw and Path(raw).is_dir() else None


def _bus_path(env: dict) -> Path | None:
    address = env.get("DBUS_SESSION_BUS_ADDRESS") or ""
    match = re.search(r"unix:path=([^,;]+)", address)
    if match:
        return Path(match.group(1))
    runtime = _runtime_dir(env)
    return runtime / "bus" if runtime and (runtime / "bus").exists() else None


def _socket_paths(kind: str, env: dict) -> list[Path]:
    runtime = _runtime_dir(env)
    if runtime is None:
        return []
    if kind == "wayland":
        display = env.get("WAYLAND_DISPLAY") or "wayland-1"
        return [runtime / display]
    if kind == "hyprland":
        sig = env.get("HYPRLAND_INSTANCE_SIGNATURE")
        return [runtime / "hypr" / sig] if sig else [runtime / "hypr"]
    if kind == "pipewire":
        return [
            runtime / "pipewire-0",
            runtime / "pipewire-0-manager",
            runtime / "pulse" / "native",
        ]
    if kind == "uwsm":
        return [
            runtime / "uwsm-app-daemon-in",
            runtime / "uwsm-app-daemon-out",
            runtime / "uwsm",
        ]
    if kind == "gnupg":
        return [runtime / "gnupg"]
    return []


def _start_dbus_proxy(bus: Path, talk: set[str], directory: Path):
    """xdg-dbus-proxy filtering the session bus to `talk`; (process, socket path, keepalive)."""
    proxy = shutil.which("xdg-dbus-proxy")
    if not proxy:
        return None
    path = directory / "bus"
    ours, theirs = socket.socketpair()
    try:
        proc = subprocess.Popen(
            [
                proxy,
                f"--fd={theirs.fileno()}",
                f"unix:path={bus}",
                str(path),
                "--filter",
                *(f"--talk={name}" for name in sorted(talk)),
            ],
            pass_fds=(theirs.fileno(),),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        ours.close()
        theirs.close()
        return None
    theirs.close()
    ours.settimeout(5)
    try:
        ready = ours.recv(1) == b"x"
    except OSError:
        ready = False
    if not ready:
        ours.close()
        proc.kill()
        return None
    return proc, path, ours


def build_argv(
    argv: list[str],
    policy: Policy,
    *,
    env: dict,
    cwd: str | Path,
    dbus_socket: Path | None = None,
) -> list[str]:
    """The bwrap argv that runs `argv` under `policy` (no side effects)."""
    # /proc stays the host's (ps, pgrep and kill see the user's processes; the
    # kernel refuses /proc/PID/root of a process outside the namespace) and
    # writable, for /proc/self/comm and the like.
    out = [
        "bwrap",
        "--ro-bind",
        "/",
        "/",
        "--dev-bind",
        "/dev",
        "/dev",
        "--bind",
        "/proc",
        "/proc",
    ]
    runtime = _runtime_dir(env)
    if runtime is not None:
        out += ["--tmpfs", str(runtime)]
    for path in policy.writable:
        out += ["--bind", str(path), str(path)]
    for path in policy.readonly:
        if any(w == path or w in path.parents for w in policy.writable):
            out += ["--ro-bind", str(path), str(path)]
    for path in policy.approved:
        out += ["--bind", str(path), str(path)]
    for path in policy.readonly:
        # An approved ~/.config still leaves the installed tools read-only.
        if any(a in path.parents for a in policy.approved):
            out += ["--ro-bind", str(path), str(path)]
    for path in policy.hidden:
        if path.is_dir():
            out += ["--tmpfs", str(path), "--remount-ro", str(path)]
        else:
            out += ["--ro-bind", "/dev/null", str(path)]
    for path, writable in policy.expose:
        if (
            runtime is not None
            and (path == runtime or runtime in path.parents)
            and not path.exists()
        ):
            continue
        out += ["--bind" if writable else "--ro-bind", str(path), str(path)]
    for kind in sorted(policy.sockets - {"dbus"}):
        for path in _socket_paths(kind, env):
            if path.exists():
                out += ["--bind", str(path), str(path)]
    if "dbus" in policy.sockets:
        bus = _bus_path(env)
        source = dbus_socket or bus
        if bus is not None and source is not None and source.exists():
            out += ["--bind", str(source), str(bus)]
    if not policy.network:
        out.append("--unshare-net")
    out += [
        "--die-with-parent",
        "--chdir",
        str(cwd),
        "--setenv",
        MARKER_ENV,
        policy.level.name,
        "--",
        *argv,
    ]
    return out


@contextlib.contextmanager
def wrapped(
    argv: list[str], policy: Policy | None, *, env: dict, cwd: str | Path
) -> Iterator[list[str]]:
    """`argv` under `policy` when the sandbox is on and works here; else `argv`."""
    if policy is None or not enabled() or not available():
        yield argv
        return
    proxy = None
    directory: Path | None = None
    try:
        bus = _bus_path(env) if "dbus" in policy.sockets else None
        if bus is not None and policy.dbus_talk is not None and bus.exists():
            base = _runtime_dir(env) or Path(tempfile.gettempdir())
            directory = Path(tempfile.mkdtemp(prefix="omarchy-ai-sandbox-", dir=base))
            proxy = _start_dbus_proxy(bus, policy.dbus_talk, directory)
            if proxy is None:
                log.warning(
                    "Sandbox: xdg-dbus-proxy unavailable; %s gets the unfiltered session bus",
                    policy.label,
                )
        bwrap = shutil.which("bwrap") or "bwrap"
        built = build_argv(
            argv, policy, env=env, cwd=cwd, dbus_socket=proxy[1] if proxy else None
        )
        yield [bwrap, *built[1:]]
    finally:
        if proxy is not None:
            proc, _path, keepalive = proxy
            keepalive.close()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)


READ_ONLY_HINT = (
    "[sandbox] This command ran at {level}: outside the workspace only /tmp, caches and the paths the "
    "command itself names as write targets (redirect, cp/mv/mkdir/tee/touch) are writable; "
    "~/.config is read-only below ELEVATED, and keys, ~/.ssh, ~/.gnupg and browser profiles are hidden."
)


def explain_failure(output: str, policy: Policy | None) -> str:
    """A note for the agent when a sandboxed command hit the sandbox."""
    if (
        policy is None
        or not re.search(r"Read-only file system|EROFS", output or "")
        or not enabled()
        or not available()
    ):
        return ""
    return READ_ONLY_HINT.format(level=policy.level.name)
