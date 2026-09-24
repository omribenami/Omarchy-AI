"""Risk classification and enforcement for everything the Task Runtime runs.

The harness, not a prompt, owns this boundary: every shell command, coding
agent invocation, desktop action and rollback goes through `check()` before
it runs, and nothing (Jev, the System agent, a config value) can lift a
BLOCKED verdict or auto-approve a HIGH one.

Levels (from the product requirements):

- LOW       read files, inspect processes, read logs, query state, diagnostics
- NORMAL    project-local file changes, launching apps, ordinary user commands
- ELEVATED  installing packages, changing configuration, restarting services
- HIGH      sudo/root, deleting significant data, security settings,
            credential access, force-pushing
- BLOCKED   catastrophic or irreversible (disk erase, `rm -rf ~`, fork bomb,
            reverse shells, base64-to-shell): refused, never askable

Adapted from MiniMax Code's permission module
(packages/agent-modules/permission, MIT): the hard-blocked and soft-risk
pattern registries (classifier/dangerous-patterns.ts), the quote-aware
tokenizer and subcommand split (tools/shell-tokenize.ts, bash-split.ts),
wrapper unwrapping (bash-wrapper-unwrap.ts: sudo/env/timeout/...), the
redirect write-target check (bash-write-target.ts) and the SIGKILL
demotion (bash-safe-first-words.ts). MiniMax answers allow/deny/ask and
then consults a cloud LLM classifier for soft risks; here the output is a
graded risk level instead, and there is no LLM in the loop at all: soft
risks become ELEVATED/HIGH and go to the user.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
import os
from pathlib import Path
import re
import shlex
import tempfile


class Risk(IntEnum):
    LOW = 0
    NORMAL = 1
    ELEVATED = 2
    HIGH = 3
    BLOCKED = 4

    @classmethod
    def parse(cls, value, default: "Risk") -> "Risk":
        try:
            return cls[str(value).upper()]
        except KeyError:
            return default


@dataclass
class Assessment:
    risk: Risk
    reasons: list[str] = field(default_factory=list)

    def raise_to(self, risk: Risk, reason: str) -> None:
        if risk > self.risk:
            self.risk = risk
        if reason and reason not in self.reasons:
            self.reasons.append(reason)

    def merge(self, other: "Assessment") -> None:
        for reason in other.reasons:
            self.raise_to(other.risk, reason)
        if other.risk > self.risk:
            self.risk = other.risk

    def as_dict(self) -> dict:
        return {"risk": self.risk.name, "reasons": self.reasons[:6]}


# ---------------------------------------------------------------------------
# BLOCKED: catastrophic / irreversible / exfiltration. Adapted from MiniMax's
# HARD_BLOCKED_REGISTRY (Windows/macOS-only entries dropped).
# ---------------------------------------------------------------------------
_SENSITIVE_PATH = (r"(?:/etc/(?:shadow|gshadow|sudoers)|\.ssh/id_(?:rsa|dsa|ecdsa|ed25519)\b|"
                   r"\.aws/credentials|\.gnupg/private|\.(?:p12|pfx|keystore|jks)\b|private[^/\s]*\.pem\b)")
_INTERPRETERS = r"(?:bash|sh|zsh|dash|fish|ksh|python3?|perl|ruby|node)"

_BLOCKED = [
    (r"(?:^|[;&|]\s*)rm\s+(?:-[a-zA-Z]*\s+)*(?:--no-preserve-root\s+)?(?:/|/\*|~/?|~/\*|\$HOME/?|\"?\$HOME\"?/?|\*)(?:\s|$)",
     "removes the root filesystem, the home directory or everything in the cwd"),
    (r":\(\)\s*\{\s*:\s*\|\s*:&\s*\}\s*;\s*:", "fork bomb"),
    (r"\b(?:shred|srm|wipe|secure-delete)\b", "irrecoverable delete"),
    (r"\brsync\b[^|;&]*\s--remove-source-files\b", "rsync that deletes its source"),
    (r"\b(?:wipefs|blkdiscard)\b", "disk erase"),
    (r"\bsgdisk\b.*(?:--zap-all|-Z)\b", "partition table erase"),
    (r"\bcryptsetup\s+luksErase\b", "LUKS erase"),
    (r"\bmkfs(?:\.\w+)?\b", "formats a filesystem"),
    (r"\bdd\b[^|;&]*\bof=/dev/", "raw write to a block device"),
    (r"\bzfs\s+destroy\b|\bbtrfs\s+subvolume\s+delete\b|\blvremove\b", "deletes a storage volume"),
    (r"\bdebugfs\s+-w\b", "direct inode edit"),
    (r"\btar\b[^|;&]*--remove-files\b", "archive that deletes its source"),
    (r"/dev/(?:tcp|udp)/", "raw network socket from the shell (reverse-shell vector)"),
    (rf"\bbase64\s+(?:-d|--decode)\b.*\|\s*(?:sudo\s+)?{_INTERPRETERS}\b", "base64-decoded payload piped to a shell"),
    (r"\bsource\s+/dev/stdin\b", "in-memory script injection"),
    (rf"\b(?:curl|wget|http|aria2c)\b[^|;&]*(?:--data|-d|-F|--form|--upload-file|-T|@)[^|;&]*{_SENSITIVE_PATH}",
     "uploads a credential file"),
    (r">\s*/dev/(?:sd[a-z]|nvme\d|mmcblk\d|vd[a-z])", "overwrites a block device"),
    (r"\bchmod\s+(?:-R\s+)?[0-7]*777\s+/(?:\s|$)", "makes the root filesystem world-writable"),
]
_BLOCKED = [(re.compile(p, re.I), why) for p, why in _BLOCKED]

# Credential access: never automatic, but a user can approve it.
_CREDENTIAL = [
    (re.compile(_SENSITIVE_PATH, re.I), "touches a private key or credential file"),
    (re.compile(r"\bsecret-tool\s+(?:lookup|search)\b|\bpass\s+show\b|\bgpg\s+(?:-d|--decrypt)\b", re.I),
     "reads a stored secret"),
    (re.compile(r"(?:\$\{?|printenv\s+)\w*(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_?KEY)\b", re.I),
     "reads a credential environment variable"),
]
# Remote code execution idioms (curl | bash): MiniMax demoted these from hard
# block to a bypass-immune ask so real installers (rustup, nvm) still work.
_REMOTE_EXEC = re.compile(rf"\b(?:curl|wget)\b[^;&]*\|\s*(?:sudo\s+)?{_INTERPRETERS}\b", re.I)

# ---------------------------------------------------------------------------
# Command vocabulary
# ---------------------------------------------------------------------------
# Read-only inspection: safe to run automatically whatever the arguments
# (arguments that write are caught by the redirect/flag checks below).
_READ_ONLY = {
    "ls", "cat", "head", "tail", "wc", "sort", "uniq", "diff", "cmp", "file", "stat", "du", "df", "tree",
    "which", "type", "whereis", "command", "date", "whoami", "id", "hostname", "hostnamectl", "uname",
    "uptime", "echo", "printf", "pwd", "printenv", "env", "grep", "egrep", "rg", "ag", "fd", "nl", "less",
    "more", "jq", "yq", "tr", "cut", "column", "strings", "hexdump", "xxd", "od", "readelf", "objdump",
    "nm", "ldd", "md5sum", "sha1sum", "sha256sum", "sha512sum", "cksum", "b2sum", "test", "[", "true",
    "false", "sleep", "basename", "dirname", "realpath", "readlink", "seq", "free", "vmstat", "iostat",
    "lscpu", "lsblk", "lsusb", "lspci", "lsmod", "lshw", "dmidecode", "sensors", "inxi", "fastfetch",
    "ps", "pgrep", "pidof", "top", "htop", "btop", "lsof", "ss", "netstat", "ping", "dig", "host",
    "nslookup", "tracepath", "traceroute", "mtr", "resolvectl", "iw", "iwctl", "journalctl",
    "coredumpctl", "loginctl", "timedatectl", "localectl", "busctl", "udevadm", "dmesg", "last", "w",
    "who", "man", "info", "apropos", "whatis", "tldr", "ffprobe", "mediainfo", "exiftool", "identify",
    "pdftotext", "pdfinfo", "wpctl", "pw-cli", "pw-dump", "pw-top", "pw-metadata", "pactl", "pamixer",
    "brightnessctl", "playerctl", "hyprctl", "wlr-randr", "xdg-mime", "fc-list", "locale", "getent",
    "awk", "gawk", "vainfo", "glxinfo", "vulkaninfo", "nvidia-smi", "upower",
    "powerprofilesctl", "fwupdmgr", "smartctl", "blkid", "findmnt", "mount", "swapon", "zramctl",
    "rfkill", "bluetoothctl", "nmcli", "ip", "systemctl", "git", "pacman", "yay", "paru", "flatpak",
    "zcat", "zgrep", "bat", "tac", "comm", "join", "paste", "fold", "fmt", "expand", "look",
}
# Subcommand-sensitive tools: {tool: (read-only first args, {arg: (risk, why)})}.
# Anything not listed as read-only for these tools is NORMAL unless flagged.
_SUBCOMMANDS: dict[str, tuple[set, dict]] = {
    "systemctl": ({"status", "show", "cat", "is-active", "is-enabled", "is-failed", "list-units",
                   "list-unit-files", "list-timers", "list-sockets", "list-dependencies", "get-default",
                   "--version", "help"},
                  {"start": (Risk.ELEVATED, "starts a service"), "stop": (Risk.ELEVATED, "stops a service"),
                   "restart": (Risk.ELEVATED, "restarts a service"), "reload": (Risk.ELEVATED, "reloads a service"),
                   "try-restart": (Risk.ELEVATED, "restarts a service"), "kill": (Risk.ELEVATED, "signals a service"),
                   "enable": (Risk.ELEVATED, "changes what starts at boot"),
                   "disable": (Risk.ELEVATED, "changes what starts at boot"),
                   "mask": (Risk.HIGH, "masks a unit"), "unmask": (Risk.ELEVATED, "unmasks a unit"),
                   "daemon-reload": (Risk.ELEVATED, "reloads unit files"), "edit": (Risk.ELEVATED, "edits a unit"),
                   "set-property": (Risk.ELEVATED, "changes a unit property"),
                   "isolate": (Risk.HIGH, "switches the system target"),
                   "poweroff": (Risk.HIGH, "powers off"), "reboot": (Risk.HIGH, "reboots"),
                   "suspend": (Risk.ELEVATED, "suspends"), "hibernate": (Risk.ELEVATED, "hibernates")}),
    "journalctl": ({"*"}, {"--vacuum-size": (Risk.HIGH, "deletes logs"), "--vacuum-time": (Risk.HIGH, "deletes logs"),
                           "--vacuum-files": (Risk.HIGH, "deletes logs"), "--rotate": (Risk.ELEVATED, "rotates logs"),
                           "--flush": (Risk.ELEVATED, "flushes logs")}),
    "git": ({"status", "log", "diff", "show", "branch", "tag", "remote", "rev-parse", "ls-files", "blame",
             "grep", "describe", "shortlog", "config", "reflog", "stash", "fetch", "--version", "help", "ls-remote"},
            {"push": (Risk.ELEVATED, "publishes commits to a remote"),
             "reset": (Risk.NORMAL, "moves the branch"), "clean": (Risk.ELEVATED, "deletes untracked files"),
             "filter-branch": (Risk.HIGH, "rewrites history"), "filter-repo": (Risk.HIGH, "rewrites history")}),
    "pacman": ({"-Q", "-Qi", "-Qs", "-Ql", "-Qo", "-Qe", "-Qm", "-Qdt", "-Qu", "-Ss", "-Si", "-Sl", "-F", "-Fl",
                "-Fx", "--version", "-h", "--help"},
               {"-S": (Risk.ELEVATED, "installs packages"), "-Syu": (Risk.ELEVATED, "upgrades the system"),
                "-U": (Risk.ELEVATED, "installs a package file"), "-R": (Risk.HIGH, "removes packages"),
                "-Rns": (Risk.HIGH, "removes packages"), "-Rs": (Risk.HIGH, "removes packages"),
                "-Sc": (Risk.ELEVATED, "cleans the package cache"), "-Scc": (Risk.HIGH, "empties the package cache")}),
    "bluetoothctl": ({"show", "list", "devices", "info", "paired-devices", "--version", "help"},
                     {"remove": (Risk.ELEVATED, "unpairs a device"), "power": (Risk.NORMAL, "toggles the adapter")}),
    "nmcli": ({"general", "g", "device", "d", "dev", "connection", "c", "con", "radio", "r", "monitor",
               "-t", "-f", "-g", "--version", "help"},
              {"modify": (Risk.ELEVATED, "changes a network connection"), "delete": (Risk.ELEVATED, "deletes a network connection"),
               "add": (Risk.ELEVATED, "adds a network connection"), "off": (Risk.NORMAL, "turns networking off")}),
    "ip": ({"addr", "a", "address", "link", "l", "route", "r", "neigh", "n", "rule", "-br", "-j", "-s",
            "-4", "-6", "-c", "--version", "help"},
           {"add": (Risk.HIGH, "changes network configuration"), "del": (Risk.HIGH, "changes network configuration"),
            "set": (Risk.HIGH, "changes network configuration"), "flush": (Risk.HIGH, "flushes network configuration")}),
    "docker": ({"ps", "images", "logs", "inspect", "info", "version", "stats", "top", "port", "events",
                "--version", "help"},
               {"rm": (Risk.ELEVATED, "removes containers"), "rmi": (Risk.ELEVATED, "removes images"),
                "prune": (Risk.HIGH, "prunes data"), "exec": (Risk.ELEVATED, "runs code in a container"),
                "run": (Risk.ELEVATED, "runs a container")}),
    "adb": ({"devices", "version", "get-state", "get-serialno", "logcat", "help"},
            {"install": (Risk.ELEVATED, "installs an app on a device"), "uninstall": (Risk.HIGH, "uninstalls an app on a device"),
             "reboot": (Risk.ELEVATED, "reboots a device"), "root": (Risk.HIGH, "roots a device"),
             "push": (Risk.NORMAL, "writes to a device"), "shell": (Risk.NORMAL, "runs a device shell")}),
    "flatpak": ({"list", "info", "search", "remotes", "--version", "help"},
                {"install": (Risk.ELEVATED, "installs an app"), "uninstall": (Risk.HIGH, "removes an app"),
                 "update": (Risk.ELEVATED, "updates apps")}),
    "hyprctl": ({"clients", "activewindow", "activeworkspace", "workspaces", "monitors", "devices", "layers",
                 "binds", "version", "getoption", "cursorpos", "-j", "splash", "systeminfo", "instances"},
                {"kill": (Risk.NORMAL, "enters kill mode"), "keyword": (Risk.NORMAL, "changes a live setting")}),
    "wpctl": ({"status", "inspect", "get-volume", "--help", "-h"}, {}),
    "pactl": ({"list", "info", "stat", "get-default-sink", "get-default-source", "get-sink-volume",
               "get-source-volume", "get-sink-mute", "get-source-mute", "subscribe", "--version", "help"},
              {"unload-module": (Risk.ELEVATED, "unloads an audio module"), "load-module": (Risk.ELEVATED, "loads an audio module")}),
    "rfkill": ({"list", "--output", "-o", "-n", "--noheadings", "-J", "--json"},
               {"block": (Risk.ELEVATED, "disables a radio")}),
    "omarchy": ({"--help", "help", "version", "theme"}, {}),
    "adb-shell": (set(), {}),
}
for _tool in ("yay", "paru"):
    _SUBCOMMANDS[_tool] = (_SUBCOMMANDS["pacman"][0] | {"-Ps", "-Gp"}, dict(_SUBCOMMANDS["pacman"][1]))
_SUBCOMMANDS["podman"] = _SUBCOMMANDS["docker"]

# Always at least this risk regardless of arguments.
_TOOL_FLOOR = {
    "sudo": (Risk.HIGH, "runs as root"), "su": (Risk.HIGH, "switches user"), "doas": (Risk.HIGH, "runs as root"),
    "pkexec": (Risk.HIGH, "runs as root"), "run0": (Risk.HIGH, "runs as root"),
    "passwd": (Risk.HIGH, "changes a password"), "chpasswd": (Risk.HIGH, "changes passwords"),
    "useradd": (Risk.HIGH, "changes users"), "userdel": (Risk.HIGH, "changes users"), "usermod": (Risk.HIGH, "changes users"),
    "visudo": (Risk.HIGH, "edits sudoers"), "ufw": (Risk.HIGH, "changes the firewall"), "iptables": (Risk.HIGH, "changes the firewall"),
    "nft": (Risk.HIGH, "changes the firewall"), "firewall-cmd": (Risk.HIGH, "changes the firewall"),
    "fdisk": (Risk.HIGH, "edits partitions"), "parted": (Risk.HIGH, "edits partitions"), "gdisk": (Risk.HIGH, "edits partitions"),
    "cfdisk": (Risk.HIGH, "edits partitions"), "mount": (Risk.NORMAL, ""), "umount": (Risk.ELEVATED, "unmounts a filesystem"),
    "crontab": (Risk.ELEVATED, "changes scheduled jobs"), "chsh": (Risk.HIGH, "changes the login shell"),
    "setfacl": (Risk.ELEVATED, "changes file ACLs"), "chattr": (Risk.HIGH, "changes file attributes"),
    "shutdown": (Risk.HIGH, "powers off"), "reboot": (Risk.HIGH, "reboots"), "poweroff": (Risk.HIGH, "powers off"),
    "eval": (Risk.ELEVATED, "evaluates a dynamic string"), "exec": (Risk.NORMAL, ""),
    "nc": (Risk.ELEVATED, "opens raw network connections"), "ncat": (Risk.ELEVATED, "opens raw network connections"),
    "netcat": (Risk.ELEVATED, "opens raw network connections"), "socat": (Risk.ELEVATED, "opens raw network connections"),
    "insmod": (Risk.HIGH, "loads a kernel module"), "rmmod": (Risk.HIGH, "unloads a kernel module"),
    "modprobe": (Risk.HIGH, "changes kernel modules"), "sysctl": (Risk.NORMAL, ""),
    "npx": (Risk.NORMAL, ""), "uvx": (Risk.NORMAL, ""),
}
# Services a careless restart has already broken (CLAUDE.md): restarting or
# stopping them is HIGH, not ELEVATED.
_CRITICAL_SERVICES = re.compile(r"\b(?:pipewire(?:-pulse)?|wireplumber|omarchy-ai|hyprland|sddm|gdm|"
                                r"systemd-logind|dbus|NetworkManager|sshd|display-manager)(?:\.service|\.socket)?\b", re.I)
# Wrappers whose real command follows (MiniMax bash-wrapper-unwrap.ts).
_WRAPPERS = {"env", "command", "exec", "nohup", "nice", "ionice", "time", "stdbuf", "timeout", "setsid",
             "uwsm-app", "uwsm", "xdg-terminal-exec", "chrt", "taskset", "unbuffer", "caffeinate", "systemd-run",
             "sudo", "doas", "run0", "pkexec", "flock"}
_WRAPPER_ARG_OPTS = {"timeout": 1, "nice": 0, "flock": 1, "chrt": 1, "taskset": 1}
_SHELLS = {"bash", "sh", "zsh", "dash", "fish", "ksh"}
_INTERPRETER_INLINE = re.compile(r"\b(?:python3?|node|perl|ruby|php|lua)\s+(?:-\w+\s+)*-[ce]\b", re.I)
_SIGKILL = re.compile(r"(?:^|\s)(?:-(?:9|KILL|SIGKILL)|--signal=(?:9|KILL|SIGKILL)|-s\s+(?:9|KILL|SIGKILL))(?:\s|$)", re.I)


# ---------------------------------------------------------------------------
# Shell text -> subcommands. Port of MiniMax's quote-aware split; it does not
# execute anything and it errs on the side of seeing more commands.
# ---------------------------------------------------------------------------
def split_commands(command: str) -> list[str]:
    parts, current, quote, escaped, depth = [], [], None, False, 0
    text = command.replace("\r\n", "\n")
    i = 0
    while i < len(text):
        ch = text[i]
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\" and quote != "'":
            current.append(ch)
            escaped = True
        elif quote:
            current.append(ch)
            if ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            current.append(ch)
        elif ch == "(" and i > 0 and text[i - 1] == "$":
            depth += 1
            current.append(ch)
        elif ch == ")" and depth:
            depth -= 1
            current.append(ch)
        elif depth == 0 and (ch in ";\n" or ch == "|" or (ch == "&" and text[i + 1:i + 2] != ">")):
            if ch == "&" and text[i - 1:i] in (">", "<"):  # 2>&1
                current.append(ch)
            else:
                parts.append("".join(current))
                current = []
                if text[i + 1:i + 2] == ch:  # && ||
                    i += 1
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip() and p.strip() not in ("{", "}", "(", ")")]


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, comments=True)
    except ValueError:
        return command.split()


def _substitutions(command: str) -> list[str]:
    """Bodies of $(...) and `...` -- executed, so classified like any command."""
    found, i = [], 0
    while True:
        start = command.find("$(", i)
        if start < 0:
            break
        depth, j = 1, start + 2
        while j < len(command) and depth:
            depth += {"(": 1, ")": -1}.get(command[j], 0)
            j += 1
        found.append(command[start + 2:j - 1])
        i = j
    found += re.findall(r"`([^`]*)`", command)
    return found


def unwrap(tokens: list[str]) -> tuple[list[str], list[str]]:
    """(real command tokens, wrappers seen). `sudo -u x env A=1 timeout 5 ls`
    -> (['ls'], ['sudo', 'env', 'timeout'])."""
    wrappers = []
    tokens = list(tokens)
    while tokens:
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens.pop(0)
        if not tokens:
            break
        word = os.path.basename(tokens[0])
        if word not in _WRAPPERS:
            break
        wrappers.append(word)
        tokens.pop(0)
        if word == "command" and tokens and tokens[0] in ("-v", "-V"):
            return [], wrappers  # `command -v x` only looks x up
        if word in ("xdg-terminal-exec", "uwsm") and tokens and tokens[0] == "app":
            tokens.pop(0)
        # Skip the wrapper's own options (and their values where known).
        while tokens and tokens[0].startswith("-"):
            opt = tokens.pop(0)
            if opt == "--":
                break
            if word in ("sudo", "doas") and opt in ("-u", "-g", "-C", "-h", "-p", "-U") and tokens:
                tokens.pop(0)
            if word == "systemd-run" and opt in ("-p", "--unit", "-u", "--property") and tokens:
                tokens.pop(0)
        for _ in range(_WRAPPER_ARG_OPTS.get(word, 0)):
            if tokens and not tokens[0].startswith("-"):
                tokens.pop(0)
        while tokens and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", tokens[0]):
            tokens.pop(0)
    return tokens, wrappers


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
@dataclass
class Scope:
    """Where the current assignment may write without asking."""
    workspaces: list[Path] = field(default_factory=list)
    scratch: list[Path] = field(default_factory=lambda: [Path("/tmp"), Path("/dev/null")])

    def resolve(self, raw: str, cwd: Path) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not path.is_absolute():
            path = cwd / path
        try:
            return path.resolve(strict=False)
        except (OSError, RuntimeError):
            return path

    def in_workspace(self, path: Path) -> bool:
        # The home directory (or /) as a workspace is not a project: it would
        # make every file under it "project-local" (code review 2026-09-23).
        return any(path == w or w in path.parents for w in self.workspaces if w not in (_HOME, Path("/")))

    def in_scratch(self, path: Path) -> bool:
        return any(path == s or s in path.parents for s in self.scratch)


_HOME = Path.home()
_SYSTEM_DIRS = ("/etc", "/usr", "/boot", "/bin", "/sbin", "/lib", "/lib64", "/opt", "/var", "/srv", "/root", "/sys", "/proc")
_CONFIG_DIRS = (_HOME / ".config", _HOME / ".local/share/applications", _HOME / ".bashrc", _HOME / ".zshrc",
                _HOME / ".profile", _HOME / ".bash_profile", _HOME / ".ssh", _HOME / ".gnupg", _HOME / ".local/bin")


def write_risk(path: Path, scope: Scope, *, delete: bool = False, recursive: bool = False) -> tuple[Risk, str]:
    """Risk of creating/modifying (or deleting) `path`."""
    text = str(path)
    verb = "deletes" if delete else "writes"
    if path in (Path("/"), _HOME):
        return Risk.BLOCKED, f"{verb} {text}"
    if text.startswith(("/dev/null", "/dev/stdout", "/dev/stderr", "/dev/fd/")):
        return Risk.LOW, ""
    if scope.in_scratch(path):
        return Risk.LOW if not delete else Risk.NORMAL, ""
    if (_HOME / ".ssh") in path.parents or (_HOME / ".gnupg") in path.parents:
        return Risk.HIGH, f"{verb} security material under {text}"
    if text.startswith(_SYSTEM_DIRS):
        return Risk.HIGH, f"{verb} system path {text}"
    # Configuration before the workspace check: a workspace containing it
    # (e.g. ~ or ~/.config) must not make config edits project-local.
    if any(path == c or c in path.parents for c in _CONFIG_DIRS):
        return Risk.ELEVATED, f"{verb} user configuration {text}"
    if scope.in_workspace(path):
        if delete and recursive:
            return Risk.ELEVATED, f"recursively deletes {text} inside the workspace"
        return Risk.NORMAL, ""
    if _HOME in path.parents:
        if delete and recursive:
            return Risk.HIGH, f"recursively deletes {text} outside the workspace"
        if delete:
            return Risk.ELEVATED, f"deletes {text} outside the workspace"
        return Risk.NORMAL, ""
    return Risk.HIGH, f"{verb} {text} outside the home directory"


_REDIRECT = re.compile(r"(?<![<0-9&])(?:[0-9]|&)?>>?\|?\s*(?!&)([^\s;&|<>]+)")


def _redirect_targets(subcommand: str) -> list[str]:
    # Mask quoted strings so '>' inside data is not a redirect.
    masked = re.sub(r"'[^']*'|\"(?:\\.|[^\"\\])*\"", lambda m: "_" * len(m.group(0)), subcommand)
    return [subcommand[m.start(1):m.end(1)] for m in _REDIRECT.finditer(masked)]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------
def classify(command: str, cwd: str | Path | None = None, scope: Scope | None = None) -> Assessment:
    """Graded risk of running `command` (bash) in `cwd`."""
    scope = scope or Scope()
    cwd = Path(cwd or os.getcwd())
    result = Assessment(Risk.LOW)
    if not command.strip():
        result.raise_to(Risk.BLOCKED, "empty command")
        return result
    for pattern, why in _BLOCKED:
        if pattern.search(command):
            result.raise_to(Risk.BLOCKED, why)
            return result
    for pattern, why in _CREDENTIAL:
        if pattern.search(command):
            result.raise_to(Risk.HIGH, why)
    if _REMOTE_EXEC.search(command):
        result.raise_to(Risk.HIGH, "pipes a download into an interpreter")
    for inner in _substitutions(command):
        result.merge(classify(inner, cwd, scope))
    if re.search(r"<\(|>\(", command):
        result.raise_to(Risk.NORMAL, "process substitution")
    for sub in split_commands(command):
        result.merge(_classify_one(sub, cwd, scope))
        if result.risk is Risk.BLOCKED:
            break
    return result


def _classify_one(sub: str, cwd: Path, scope: Scope) -> Assessment:
    result = Assessment(Risk.LOW)
    for target in _redirect_targets(sub):
        if target.startswith("&"):
            continue
        risk, why = write_risk(scope.resolve(target, cwd), scope)
        result.raise_to(risk, why or ("writes a file" if risk > Risk.LOW else ""))
    raw = _tokens(re.sub(r"(?:[0-9]|&)?>>?\|?\s*[^\s;&|<>]+|<\s*[^\s;&|<>]+", " ", sub))
    tokens, wrappers = unwrap(raw)
    for w in wrappers:
        if w in _TOOL_FLOOR and _TOOL_FLOOR[w][1]:
            result.raise_to(*_TOOL_FLOOR[w])
    if not tokens:
        return result
    word = os.path.basename(tokens[0])
    args = tokens[1:]
    if word in _TOOL_FLOOR and _TOOL_FLOOR[word][1]:
        result.raise_to(*_TOOL_FLOOR[word])
    if word in ("cd", "export", "unset", "set", "for", "while", "do", "done", "if", "then", "fi", "else",
                "case", "esac", "in", "until", "local", "read", "shift", "return", "break", "continue", ":"):
        return result
    if "--help" in args or "-h" == (args[0] if args else "") or (args and args[0] == "help") or args == ["--version"]:
        return result
    if word in _SHELLS:
        if "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args):
                result.merge(classify(args[i + 1], cwd, scope))
        elif args:
            result.raise_to(Risk.NORMAL, f"runs script {args[0]}")
        return result
    if _INTERPRETER_INLINE.search(sub):
        result.raise_to(Risk.NORMAL, "runs inline interpreter code")
    if word in _SUBCOMMANDS:
        _classify_subcommand(word, args, sub, result)
    elif word in ("rm", "rmdir", "unlink", "trash", "trash-put", "gio"):
        if word == "gio" and (not args or args[0] not in ("remove", "trash")):
            pass
        else:
            recursive = any(re.fullmatch(r"-[a-zA-Z]*[rR][a-zA-Z]*|--recursive", a) for a in args) or word == "rmdir"
            targets = [a for a in args if not a.startswith("-")] or ["?"]
            for t in targets:
                if word == "gio" and t in ("remove", "trash"):
                    continue
                risk, why = write_risk(scope.resolve(t, cwd), scope, delete=True, recursive=recursive)
                if re.search(r"[*?]", t) and risk < Risk.ELEVATED and recursive:
                    risk, why = Risk.ELEVATED, f"recursive glob delete {t}"
                result.raise_to(risk, why or f"deletes {t}")
    elif word in ("mv", "cp", "ln", "install", "rsync", "scp", "touch", "mkdir", "tee", "dd", "truncate",
                  "chmod", "chown", "chgrp", "unzip", "tar", "ffmpeg", "magick", "convert", "sed", "perl"):
        _classify_writer(word, args, sub, cwd, scope, result)
    elif word == "fuser":
        if any(re.fullmatch(r"-[a-zA-Z]*k[a-zA-Z]*|--kill", a) for a in args):
            result.raise_to(Risk.ELEVATED, "fuser -k kills the processes using a file/port")
    elif word in ("kill", "pkill", "killall"):
        if _SIGKILL.search(sub):
            result.raise_to(Risk.ELEVATED, "force-kills a process (SIGKILL)")
        else:
            result.raise_to(Risk.NORMAL, "signals a process")
        if _CRITICAL_SERVICES.search(sub):
            result.raise_to(Risk.HIGH, "kills a critical desktop/audio process")
    elif word in ("find", "fd"):
        if any(a in ("-delete", "--exec", "-x", "--exec-batch", "-X") for a in args):
            result.raise_to(Risk.ELEVATED, f"{word} that deletes or executes on matches")
        for flag in ("-exec", "-execdir", "-ok", "-okdir"):
            if flag in args:
                inner = " ".join(args[args.index(flag) + 1:]).split(";")[0].replace("{}", "x").rstrip("\\ ")
                result.merge(classify(inner, cwd, scope))
                result.raise_to(Risk.NORMAL, f"find {flag}")
    elif word == "xargs":
        inner = [a for a in args if not a.startswith("-")]
        if inner:
            result.merge(classify(" ".join(inner), cwd, scope))
        result.raise_to(Risk.NORMAL, "xargs runs a command per input")
    elif word in ("curl", "wget"):
        if any(a in ("-o", "-O", "--output", "--remote-name", "-P") for a in args) or word == "wget":
            result.raise_to(Risk.NORMAL, "downloads a file")
        if any(a in ("-X", "--request", "-d", "--data", "-F", "--form", "-T", "--upload-file") for a in args):
            result.raise_to(Risk.NORMAL, "sends data to a server")
    elif word in ("pip", "pip3", "npm", "pnpm", "yarn", "bun", "cargo", "go", "gem", "uv"):
        verb = args[0] if args else ""
        glob = any(a in ("-g", "--global", "--user", "--break-system-packages") for a in args)
        if verb in ("install", "i", "add", "uninstall", "remove", "rm", "tool", "update", "upgrade") and glob:
            result.raise_to(Risk.ELEVATED, f"{word} {verb} outside the project")
        elif word in ("pip", "pip3") and verb in ("install", "uninstall"):
            result.raise_to(Risk.ELEVATED, "pip changes a Python environment")
        elif verb in ("publish", "login", "adduser"):
            result.raise_to(Risk.HIGH, f"{word} {verb}")
        elif verb not in ("list", "ls", "show", "view", "info", "outdated", "freeze", "--version", "search", "why", "tree"):
            result.raise_to(Risk.NORMAL, "")
    elif word in ("ssh",):
        result.raise_to(Risk.NORMAL, "runs a command on another machine")
    elif word in ("gh",):
        verb = " ".join(args[:2])
        if re.match(r"(?:pr|issue|release|repo)\s+(?:create|merge|close|delete|edit|comment)|api", verb):
            result.raise_to(Risk.ELEVATED, f"gh {verb} changes GitHub state")
    elif word in ("claude", "codex"):
        result.raise_to(Risk.ELEVATED, "starts another coding agent outside the executor layer")
    elif word in ("omarchy", "omarchy-cmd", "omarchy-launch", "omarchy-menu") or word.startswith("omarchy-"):
        if re.match(r"omarchy-(?:install|remove|update|pkg|reinstall|refresh|setup|drive|hw|dev)", word):
            result.raise_to(Risk.ELEVATED, f"{word} changes installed software or system setup")
        elif word.startswith("omarchy-") or args:
            result.raise_to(Risk.NORMAL, "")
    elif word in ("awk", "gawk") and re.search(r"\bsystem\s*\(|\|\s*\"|>\s*\"", sub):
        result.raise_to(Risk.NORMAL, "awk program runs commands or writes files")
    elif word in _READ_ONLY:
        pass
    else:
        result.raise_to(Risk.NORMAL, "")
    return result


def _classify_subcommand(word: str, args: list[str], sub: str, result: Assessment) -> None:
    read_only, flagged = _SUBCOMMANDS[word]
    positional = [a for a in args if not a.startswith("-") or a in flagged or word in ("pacman", "yay", "paru")]
    if word in ("pacman", "yay", "paru"):
        op = next((a for a in args if a.startswith("-")), "")
        if not args and word in ("yay", "paru"):
            result.raise_to(Risk.ELEVATED, "upgrades the system")
            return
        if op in read_only or re.fullmatch(r"-Q\w*|-S[si]\w*|-F\w*", op):
            return
        for key, (risk, why) in sorted(flagged.items(), key=lambda kv: -len(kv[0])):
            if op == key or (op.startswith(key) and key != "-S") or (key == "-S" and op.startswith("-S")):
                result.raise_to(risk, why)
                return
        if re.fullmatch(r"-R\w*", op):
            result.raise_to(Risk.HIGH, "removes packages")
        else:
            result.raise_to(Risk.ELEVATED, f"{word} {op}")
        return
    if word == "systemctl":
        user = "--user" in args
        verbs = [a for a in args if not a.startswith("-")]
        verb = verbs[0] if verbs else "list-units"
        if verb in read_only:
            return
        risk, why = flagged.get(verb, (Risk.ELEVATED, f"systemctl {verb}"))
        if not user and verb not in ("suspend", "hibernate"):
            risk = max(risk, Risk.HIGH)
            why = f"{why} (system-wide)"
        if _CRITICAL_SERVICES.search(" ".join(verbs[1:])) and verb in ("stop", "restart", "kill", "disable", "mask", "reload", "try-restart"):
            risk = max(risk, Risk.HIGH)
            why = f"{why}: a critical desktop/audio service"
        result.raise_to(risk, why)
        return
    if word == "journalctl":
        for a in args:
            key = a.split("=")[0]
            if key in flagged:
                result.raise_to(*flagged[key])
        return
    if word == "git":
        verbs = [a for a in args if not a.startswith("-")]
        verb = verbs[0] if verbs else "status"
        if verb == "push" and any(a in ("-f", "--force", "--force-with-lease", "--mirror", "--delete") or a.startswith("+") for a in args):
            result.raise_to(Risk.HIGH, "force-pushes or deletes remote refs")
            return
        if verb == "reset" and "--hard" in args:
            result.raise_to(Risk.ELEVATED, "discards local changes (reset --hard)")
            return
        if verb in ("checkout", "restore") and ("--" in args or "." in args) and "-b" not in args:
            result.raise_to(Risk.ELEVATED, "discards local changes")
            return
        if verb == "stash" and len(verbs) > 1 and verbs[1] in ("drop", "clear"):
            result.raise_to(Risk.ELEVATED, "deletes stashed work")
            return
        if verb == "branch" and any(a in ("-D", "-d", "--delete") for a in args):
            result.raise_to(Risk.ELEVATED, "deletes a branch")
            return
        if verb in ("config",) and any(a in ("--global", "--system") for a in args) and len(verbs) > 2:
            result.raise_to(Risk.ELEVATED, "changes global git config")
            return
        if verb in read_only:
            return
        if verb in flagged:
            result.raise_to(*flagged[verb])
            return
        result.raise_to(Risk.NORMAL, "")
        return
    first = positional[0] if positional else (args[0] if args else "")
    for a in args:
        if a in flagged:
            result.raise_to(*flagged[a])
    if result.risk > Risk.LOW:
        return
    if not args and word in ("rfkill", "nmcli", "pactl", "wpctl"):
        return
    if first in read_only or "*" in read_only:
        if word == "nmcli" and len(args) > 1 and args[1] in ("up", "down", "connect", "disconnect", "wifi"):
            result.raise_to(Risk.NORMAL, "changes a network connection")
        elif word == "bluetoothctl" and len(args) > 1:
            pass
        return
    if word == "adb" and first == "shell":
        inner = " ".join(args[args.index("shell") + 1:])
        if re.search(r"\b(?:pm\s+(?:uninstall|clear)|rm\s+-r|reboot|settings\s+put|svc\s)", inner):
            result.raise_to(Risk.ELEVATED, "changes the device")
        elif inner:
            result.raise_to(Risk.NORMAL, "runs a device shell command")
        return
    result.raise_to(Risk.NORMAL, "")


def _classify_writer(word, args, sub, cwd, scope, result):
    positional = [a for a in args if not a.startswith("-")]
    targets: list[str] = []
    if word in ("mv", "cp", "ln", "install", "rsync", "scp"):
        if "-t" in args and args.index("-t") + 1 < len(args):
            targets = [args[args.index("-t") + 1]]
        elif positional:
            targets = positional[-1:]
        if word == "mv":
            targets += positional[:-1]  # the source disappears
    elif word in ("touch", "mkdir", "truncate", "tee"):
        targets = positional
    elif word == "dd":
        targets = [a[3:] for a in args if a.startswith("of=")]
    elif word in ("chmod", "chown", "chgrp"):
        targets = positional[1:]
        if "-R" in args or "--recursive" in args:
            result.raise_to(Risk.ELEVATED, f"recursive {word}")
        if word == "chown" and positional and positional[0].split(":")[0] == "root":
            result.raise_to(Risk.HIGH, "gives files to root")
        if word == "chmod" and positional and re.search(r"[0-7]*7[0-7]{2}$|[+]s|u\+s|g\+s", positional[0]):
            result.raise_to(Risk.ELEVATED, "broad or setuid permission change")
    elif word in ("sed", "perl"):
        if any(re.fullmatch(r"-[a-zA-Z]*i.*|--in-place.*", a) for a in args):
            targets = positional[1:]
        else:
            return
    elif word == "unzip":
        if "-d" in args and args.index("-d") + 1 < len(args):
            targets = [args[args.index("-d") + 1]]
        else:
            targets = ["."]
    elif word == "tar":
        if not re.search(r"(?:^|\s)-?[a-zA-Z]*[xc]", " ".join(args[:1])) and "--extract" not in args and "--create" not in args:
            return
        if "-C" in args and args.index("-C") + 1 < len(args):
            targets = [args[args.index("-C") + 1]]
        elif "-f" in args and args.index("-f") + 1 < len(args) and "c" in args[0]:
            targets = [args[args.index("-f") + 1]]
        else:
            targets = ["."]
    elif word in ("ffmpeg", "magick", "convert"):
        targets = positional[-1:] if positional else []
    for t in targets:
        if re.fullmatch(r"[\w.-]+@[\w.-]+:.*|[\w.-]+:.+", t) and word in ("rsync", "scp"):
            result.raise_to(Risk.NORMAL, f"copies to remote {t}")
            continue
        risk, why = write_risk(scope.resolve(t, cwd), scope)
        result.raise_to(max(risk, Risk.NORMAL) if risk is not Risk.LOW else risk, why or f"{word} writes {t}")


# ---------------------------------------------------------------------------
# Enforcement
# ---------------------------------------------------------------------------
# Auto-approval can be raised to ELEVATED at most: HIGH always asks
# (MiniMax's "bypassImmune") and BLOCKED is never runnable.
MAX_AUTO_APPROVE = Risk.ELEVATED


@dataclass
class Decision:
    behavior: str  # allow | ask | deny
    assessment: Assessment
    fingerprint: str

    @property
    def allowed(self) -> bool:
        return self.behavior == "allow"

    def as_dict(self) -> dict:
        return {"behavior": self.behavior, **self.assessment.as_dict()}


def fingerprint(kind: str, subject: str) -> str:
    return f"{kind}:{' '.join(subject.split())}"


def decide(kind: str, subject: str, assessment: Assessment, *, auto_approve: Risk, grants: set[str]) -> Decision:
    fp = fingerprint(kind, subject)
    if assessment.risk is Risk.BLOCKED:
        return Decision("deny", assessment, fp)
    if assessment.risk <= min(auto_approve, MAX_AUTO_APPROVE):
        return Decision("allow", assessment, fp)
    if fp in grants:
        return Decision("allow", assessment, fp)
    return Decision("ask", assessment, fp)


# Sensitive environment variables are scrubbed from every subprocess the
# runtime starts (MiniMax agent-core/bash-subprocess-env.ts).
SENSITIVE_ENV = re.compile(r"(?:^|_)(?:API_?KEY|KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIALS?|PRIVATE_?KEY)S?(?:_|$)", re.I)
_KEEP_ENV = {"PATH", "HOME", "SHELL", "LANG", "LOGNAME", "USER", "TERM", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS",
             "WAYLAND_DISPLAY", "DISPLAY", "HYPRLAND_INSTANCE_SIGNATURE", "SSH_AUTH_SOCK", "GPG_AGENT_INFO"}


def scrubbed_env(base: dict | None = None) -> dict:
    env = dict(os.environ if base is None else base)
    return {k: v for k, v in env.items() if k in _KEEP_ENV or not SENSITIVE_ENV.search(k)}


def coding_agent_risk(workspace: Path, write: bool) -> tuple[Risk, list[str]]:
    """Risk of letting an external coding agent loose in `workspace`.

    Read-only review is LOW. Writing inside a git repository under the home
    directory is NORMAL (project-local, and the runtime checkpoints it so it
    can be rolled back). Writing anywhere without git, in configuration, or
    outside home asks first."""
    workspace = Path(workspace).expanduser().resolve()
    if not write:
        return Risk.LOW, []
    if workspace in (Path("/"), _HOME) or str(workspace).startswith(_SYSTEM_DIRS):
        return Risk.HIGH, [f"coding agent writing in {workspace}"]
    if any(workspace == c or c in workspace.parents for c in _CONFIG_DIRS):
        return Risk.ELEVATED, [f"coding agent writing user configuration in {workspace}"]
    scratch = Path(tempfile.gettempdir()).resolve()
    if _HOME not in workspace.parents and scratch not in workspace.parents:
        return Risk.HIGH, [f"coding agent writing outside the home directory ({workspace})"]
    if not any((p / ".git").exists() for p in (workspace, *workspace.parents)):
        return Risk.ELEVATED, [f"{workspace} is not a git repository, so changes cannot be rolled back"]
    return Risk.NORMAL, []
