"""What a `sudo` fingerprint request on the phone is about: the command, who
runs it, and why.

Flux's PAM helper tells the phone only "sudo for <user> on <host>", so every
approval looked the same whether the user typed the command or an agent ran
it (2026-10-05: "a fixed screen with the same approval request"). The helper
and its signed message belong to the stock Flux package and stay as they
are. Instead the phone app asks this machine's Omarchy AI, over the paired
bridge (phone/server.py, /api/approvals/context), and shows the answer next
to the request. The answer is a description, not part of what the phone
signs; the app says so.

Everything here is read from /proc and from the agents' own session files:
- the command: the argv of the `sudo` that is waiting on the helper;
- who: the nearest ancestor process that is a known agent (Claude Code,
  Codex, Omarchy AI), else the user in a terminal;
- why: the description the agent gave for that very command (Claude Code's
  Bash `description`, Codex's `justification`), or the task Omarchy AI is on.

Omarchy AI's own approvals (execution/flux_approve.request) do not need
this: they carry requester and reason inside the signed fields.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shlex
import time
from pathlib import Path

log = logging.getLogger(__name__)

HELPER = "flux-approve"
LABELS = {"claude": "Claude Code", "codex": "Codex", "omarchy": "Omarchy AI", "jev": "Jev", "you": "You"}
MAX_TEXT = 300
TAIL_BYTES = 400_000   # of an agent's session file: the call being approved is its newest
NOTE_SECONDS = 150     # fluxd waits for the phone at most 120s
WRITE_LAG = 0.6        # before a second look at a session file with no unfinished call
# sudo options that take a value (sudo(8)); the rest stand alone.
_VALUE_OPTIONS = {"-u", "-g", "-C", "-h", "-p", "-U", "-T", "-D", "-R", "-r", "-t",
                  "--user", "--group", "--close-from", "--host", "--prompt", "--other-user",
                  "--command-timeout", "--chdir", "--chroot", "--role", "--type"}


def _note_path() -> Path:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    return Path(runtime) / "omarchy-ai" / "sudo-why.json"


def note(why: str, by: str = "omarchy") -> None:
    """Omarchy AI is about to run `sudo` for this reason (runtime._sudo's callers)."""
    try:
        path = _note_path()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.write_text(json.dumps({"why": why[:MAX_TEXT], "by": by, "at": time.time()}))
    except OSError:
        log.debug("could not note the sudo reason", exc_info=True)


def _noted(now: float) -> dict:
    try:
        data = json.loads(_note_path().read_text())
        return data if isinstance(data, dict) and now - float(data.get("at") or 0) < NOTE_SECONDS else {}
    except (OSError, ValueError, TypeError):
        return {}


def _process(proc: Path, pid: int) -> dict | None:
    try:
        stat = (proc / str(pid) / "stat").read_text()
        name, rest = stat[stat.index("(") + 1:stat.rindex(")")], stat[stat.rindex(")") + 2:].split()
        # An empty argument is real (`sudo -p ''`); only the terminator is not.
        argv = (proc / str(pid) / "cmdline").read_bytes().decode(errors="replace").removesuffix("\0").split("\0")
    except (OSError, ValueError):
        return None
    return {"pid": pid, "name": name, "parent": int(rest[1]), "started": int(rest[19]), "argv": argv}


def _ancestors(proc: Path, pid: int) -> list[dict]:
    chain, seen = [], set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        process = _process(proc, pid)
        if process is None:
            break
        chain.append(process)
        pid = process["parent"]
    return chain


def command_of(argv: list[str]) -> str:
    """`sudo -S -p '' pacman -S ffmpeg` -> `pacman -S ffmpeg`."""
    words = list(argv[1:])
    while words and words[0].startswith("-"):
        option = words.pop(0)
        if option == "--":
            break
        if option in _VALUE_OPTIONS and words:
            words.pop(0)
    return shlex.join(words)[:MAX_TEXT] if words else "(a root shell)"


def agent_of(process: dict) -> str | None:
    """Which agent this process is, by its program, never by words in its
    arguments: Claude Code's own `bash -c "... cd ~/Git/omarchy-ai ..."`
    was taken for Omarchy AI (seen on the real process tree, 2026-10-05)."""
    argv = process["argv"]
    names = {process["name"], *(os.path.basename(a) for a in argv[:2])}
    if "claude" in names or any("/claude-code/" in a for a in argv[:2]):
        return "claude"
    if any(n == "codex" or n.startswith("codex-") for n in names):
        return "codex"
    module = argv[argv.index("-m") + 1] if "-m" in argv[:4] and argv.index("-m") + 1 < len(argv) else ""
    if module == "omarchy_ai" or any(n.startswith("omarchy-ai") for n in names):
        return "omarchy"
    return None


def _tail(path: Path) -> list[dict]:
    try:
        with open(path, "rb") as handle:
            handle.seek(max(0, path.stat().st_size - TAIL_BYTES))
            lines = handle.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # the cut first line, or not JSON
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _newest(folder: Path, pattern: str) -> Path | None:
    try:
        return max(folder.glob(pattern), key=lambda p: p.stat().st_mtime, default=None)
    except OSError:
        return None


def _same_command(candidate: str, command: str) -> bool:
    words = command.split()[:2]
    return "sudo" in candidate and all(word in candidate for word in words)


def claude_reason(cwd: str, command: str, home: Path | None = None, running: str = "") -> str:
    """The `description` Claude Code gave the Bash call that runs this
    command: of its unfinished calls, the one for this very command, else
    the one whose text is in `running` (the command lines between Claude and
    sudo), else the newest. A `sudo` inside a script or a test run is in no
    call's text (2026-10-05 to 10-07: 33 requests for `true`, all "No reason
    given"), and a finished call that only mentions sudo is not the one
    asking. With nothing unfinished in the tail, the newest call for the
    command."""
    home = home or Path.home()
    session = _newest(home / ".claude" / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd), "*.jsonl")
    finished, calls = _claude_calls(session, command)
    if not calls and session:
        # Claude Code writes a call to the file up to half a second after it
        # starts the command (measured 2026-10-07); the phone waits 1.5 s.
        time.sleep(WRITE_LAG)
        finished, calls = _claude_calls(session, command)
    if not calls:
        return finished
    same = [c for c in calls if _same_command(str(c.get("command") or ""), command)]
    ours = [c for c in calls if str(c.get("command") or "").strip()[:80] in running] if running else []
    return str((same or ours or calls)[-1]["description"])


def _claude_calls(session: Path | None, command: str) -> tuple[str, list[dict]]:
    """(the description of the newest call for `command`, the unfinished Bash calls)."""
    finished, open_calls = "", {}
    for entry in _tail(session) if session else []:
        content = (entry.get("message") or {}).get("content")
        for block in content if isinstance(content, list) else []:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_result":
                open_calls.pop(block.get("tool_use_id"), None)
            if block.get("type") != "tool_use" or block.get("name") != "Bash":
                continue
            call = block.get("input") or {}
            if not call.get("description"):
                continue
            if _same_command(str(call.get("command") or ""), command):
                finished = str(call["description"])
            open_calls[block.get("id") or len(open_calls)] = call
    return finished, list(open_calls.values())


_JUSTIFICATION = re.compile(r'justification\\?["\']?\s*:\s*\\?"((?:[^"\\]|\\.)*?)\\?"')


def codex_reason(command: str, home: Path | None = None) -> str:
    """Why Codex ran this command: the `justification` of its newest shell
    call for it, else what it told the user just before the call. A call is
    JSON `arguments` in older sessions and a line of JavaScript
    (`tools.exec_command({cmd: ...})`) in `input` since 2026."""
    home = home or Path.home()
    session = _newest(home / ".codex" / "sessions", "**/rollout-*.jsonl")
    best = said = last = ""
    for entry in _tail(session) if session else []:
        payload = entry.get("payload") if isinstance(entry.get("payload"), dict) else entry
        if payload.get("type") == "message" and payload.get("role") == "assistant":
            parts = payload.get("content")
            said = " ".join(str(p.get("text") or "") for p in parts if isinstance(p, dict)) if isinstance(parts, list) else said
            continue
        call = payload.get("arguments") if isinstance(payload.get("arguments"), str) else payload.get("input")
        if not isinstance(call, str):
            continue
        given = _JUSTIFICATION.search(call)
        last = given.group(1).replace('\\"', '"') if given else said
        if _same_command(call, command):
            best = last
    return best or last  # a sudo inside a script: the newest call is the one running it


def _text(value: str) -> str:
    return " ".join("".join(ch for ch in str(value) if ch.isprintable() or ch == " ").split())[:MAX_TEXT]


def sudo_context(proc: Path = Path("/proc"), home: Path | None = None, now: float | None = None) -> dict | None:
    """The `sudo` now waiting for a fingerprint, as the phone shows it, or
    None when no such request is open."""
    home = home or Path.home()
    waiting = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        process = _process(proc, int(entry.name))
        if process and process["name"] == HELPER:
            waiting.append(process)
    if not waiting:
        return None
    helper = max(waiting, key=lambda p: p["started"])  # the newest request is the one on the phone
    chain = _ancestors(proc, helper["parent"])
    sudo = next((p for p in chain if p["name"] in ("sudo", "sudo-rs")), None)
    if sudo is None:
        return None
    command = command_of(sudo["argv"])
    callers = chain[chain.index(sudo) + 1:]
    by, agent = next(((agent_of(p), p) for p in callers if agent_of(p)), ("you", callers[0] if callers else None))
    where = ""
    if agent is not None:
        try:
            where = os.readlink(proc / str(agent["pid"]) / "cwd")
        except OSError:
            pass
    why = ""
    if by == "claude":
        between = callers[:callers.index(agent)]
        why = claude_reason(where, command, home, " ".join(" ".join(p["argv"]) for p in between))
    elif by == "codex":
        why = codex_reason(command, home)
    elif by == "omarchy":
        noted = _noted(time.time() if now is None else now)
        why, by = noted.get("why", ""), noted.get("by") if noted.get("by") in LABELS else by
    if where == str(home) or where.startswith(str(home) + "/"):
        where = "~" + where[len(str(home)):]
    return {"found": True, "request": _text(command), "by": by, "by_label": LABELS[by], "why": _text(why),
            "where": _text(where)}
