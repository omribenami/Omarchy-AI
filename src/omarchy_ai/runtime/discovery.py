"""What is installed here, and how is it used? Answered from the machine itself.

The System agent does not rely on a fixed list of wrapped tools: it can
search the PATH and the local man-page index (apropos/whatis) for tools
that fit a task, and read `--help` / `man` / `tldr` for the exact version
installed, instead of guessing flags from training data.

All of these are LOW-risk reads run with short timeouts; tool names are
validated so a "tool name" can never smuggle a shell command.
"""
from __future__ import annotations

from functools import lru_cache
import os
from pathlib import Path
import re
import shutil
import subprocess

_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,80}")
MAX_HELP_CHARS = 6000

# Illustrative only -- used to give an agent a first inventory in its
# brief. Discovery is never limited to this list (see search_tools).
NOTABLE = {
    "search": ["rg", "fd", "grep", "find", "jq", "yq", "fzf"],
    "system": ["systemctl", "journalctl", "coredumpctl", "ps", "htop", "btop", "lsof", "dmesg", "udevadm"],
    "network": ["ip", "ss", "nmcli", "iwctl", "ping", "dig", "curl", "wget", "ssh", "rsync", "tailscale"],
    "bluetooth": ["bluetoothctl", "rfkill", "btmgmt"],
    "audio": ["wpctl", "pactl", "pw-cli", "pw-dump", "pw-record", "pw-play", "arecord", "pamixer"],
    "media": ["ffmpeg", "ffprobe", "magick", "mediainfo", "exiftool", "yt-dlp"],
    "desktop": ["hyprctl", "omarchy", "walker", "grim", "slurp", "wl-copy", "wl-paste", "notify-send",
                "brightnessctl", "playerctl", "xdg-open"],
    "packages": ["pacman", "yay", "paru", "flatpak", "uv", "pip", "npm", "pnpm", "cargo", "mise"],
    "dev": ["git", "gh", "python3", "node", "make", "cmake", "gcc", "go", "rustc", "docker", "podman"],
    "devices": ["adb", "fastboot", "lsusb", "lspci", "lsblk", "smartctl", "sensors", "upower", "fwupdmgr"],
    "agents": ["claude", "codex", "aider", "gemini"],
}


def valid_name(name: str) -> bool:
    return bool(_NAME.fullmatch(name or ""))


def which(name: str) -> str | None:
    return shutil.which(name) if valid_name(name) else None


@lru_cache(maxsize=1)
def path_executables() -> frozenset[str]:
    names = set()
    for directory in os.environ.get("PATH", "").split(os.pathsep):
        try:
            for entry in os.scandir(directory):
                if entry.is_file() and os.access(entry.path, os.X_OK):
                    names.add(entry.name)
        except OSError:
            continue
    return frozenset(names)


def inventory() -> dict[str, list[str]]:
    """Notable tools actually installed, by area (for an agent's first brief)."""
    present = path_executables()
    return {area: [t for t in tools if t in present] for area, tools in NOTABLE.items()}


def omarchy_commands(prefix: str = "omarchy-") -> list[str]:
    return sorted(n for n in path_executables() if n.startswith(prefix))


def _run(argv: list[str], timeout: float = 6) -> tuple[int, str]:
    env = dict(os.environ, MANPAGER="cat", PAGER="cat", MANWIDTH="100", NO_COLOR="1", TERM="dumb")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env,
                              stdin=subprocess.DEVNULL, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def search_tools(query: str, limit: int = 25) -> list[dict]:
    """Installed tools matching a need, from executable names and the man index."""
    terms = [t for t in re.findall(r"[a-z0-9][a-z0-9+-]{1,}", query.lower()) if len(t) > 2][:6]
    found: dict[str, str] = {}
    present = path_executables()
    for term in terms:
        for name in sorted(present):
            if term in name.lower() and name not in found:
                found[name] = ""
        code, out = _run(["apropos", "-s", "1,8", term], timeout=4)
        if code == 0:
            for line in out.splitlines():
                m = re.match(r"(\S+) \((\d)\w*\)\s+-\s+(.*)", line)
                if m and m[1] in present:
                    found[m[1]] = m[3][:120]
    ranked = sorted(found.items(), key=lambda kv: (-sum(t in (kv[0] + " " + kv[1]).lower() for t in terms), kv[0]))
    return [{"tool": n, "about": d} for n, d in ranked[:limit]]


def tool_help(name: str, topic: str = "") -> dict:
    """Local documentation for `name`: --help, then man, then tldr.
    With `topic`, keep the lines around its mentions."""
    if not valid_name(name):
        return {"tool": name, "ok": False, "text": "invalid tool name"}
    path = which(name)
    if not path:
        return {"tool": name, "ok": False, "text": f"{name} is not installed (not on PATH)"}
    text, source = "", ""
    for argv, src in (([name, "--help"], "--help"), (["man", name], "man"), ([name, "help"], "help"),
                      (["tldr", name], "tldr"), ([name, "-h"], "-h")):
        if argv[0] != name and not which(argv[0]):
            continue
        code, out = _run(argv)
        if not out.strip() or re.search(r"^No manual entry", out, re.M):
            continue
        # Some tools exit non-zero for --help but still print usage.
        if code == 0 or re.search(r"\busage\b", out, re.I):
            text, source = out, src
            if len(out) > 200:
                break
    if not text:
        return {"tool": name, "path": path, "ok": False, "text": "no local documentation found"}
    text = re.sub(r".\x08", "", text)
    if topic:
        text = _focus(text, topic)
    version = ""
    code, out = _run([name, "--version"], timeout=3)
    if code == 0 and out.strip():
        version = out.strip().splitlines()[0][:120]
    return {"tool": name, "path": path, "ok": True, "source": source, "version": version,
            "text": text[:MAX_HELP_CHARS] + ("…" if len(text) > MAX_HELP_CHARS else "")}


def _focus(text: str, topic: str) -> str:
    lines = text.splitlines()
    terms = [t.lower() for t in re.findall(r"[\w-]{2,}", topic)]
    keep = set()
    for i, line in enumerate(lines):
        if any(t in line.lower() for t in terms):
            keep.update(range(max(0, i - 2), min(len(lines), i + 6)))
    if not keep:
        return text
    head = lines[:8]
    body = [lines[i] for i in sorted(keep)]
    return "\n".join(head + ["…"] + body)


def home_relative(path: str) -> str:
    try:
        return "~/" + str(Path(path).relative_to(Path.home()))
    except ValueError:
        return path
