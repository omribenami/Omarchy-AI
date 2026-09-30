"""Tools the assistant wrote for herself, installed only with the user's approval.

When an escalated task (voice/escalation.py) solves a problem the live model
had no tool for, its worker can package the solution as a tool so next time
it is one call. The user asked for this with one condition: nothing is added
without their approval.

A tool is a folder:

    tool.json   {"name": "file_github_issue", "description": "...",
                 "parameters": {JSON schema object}}
    run         executable; JSON arguments on stdin; exit 0 = success;
                stdout is the result the assistant reads (<= 4000 chars)
    test        executable; exit 0 = the tool works (run by the harness at
                propose and again at install)

Lifecycle (cli/tools.py): `propose <dir>` validates and tests it and copies
it to tools/_proposed/<name>; `install <name>` always asks the user
(runtime/permissions.py: always_ask, whatever task_auto_approve says), then
tests again, moves it to tools/<name> and pins a SHA-256 of every file in
tools/approvals.json. A tool whose files no longer match its pin is neither
offered nor run until it is approved again. Writing into tools/ directly is
HIGH risk in the Task Runtime.

Installed tools join the catalog (execution/catalog.py), which is read on
every use_tool call: a tool approved mid-conversation works in that same
conversation, and the task that asked for approval resumes and uses it.

Caveat: this is a same-user boundary, not a sandbox. It reliably gates the
normal flow and mistakes; a worker deliberately writing the ledger itself is
not something a same-user check can stop.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from ..config import CONFIG_DIR, STATE_DIR

log = logging.getLogger(__name__)

TOOLS_DIR = CONFIG_DIR / "tools"
PROPOSED_DIR = TOOLS_DIR / "_proposed"
LEDGER = TOOLS_DIR / "approvals.json"
NAME = re.compile(r"^[a-z][a-z0-9_]{2,40}$")
RUN_TIMEOUT = 120
TEST_TIMEOUT = 180
OUTPUT_CHARS = 4000


def _builtin_names() -> set[str]:
    from .actions import ACTIONS
    from .tools import MYAPI_TOOLS
    return set(ACTIONS) | {t["name"] for t in MYAPI_TOOLS} | {"use_tool", "end_conversation"}


def _hashes(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file() and "__pycache__" not in p.parts}


def _ledger() -> dict:
    try:
        return json.loads(LEDGER.read_text())
    except (OSError, ValueError):
        return {}


def validate(folder: Path) -> tuple[dict | None, list[str]]:
    """(spec, problems) for a tool folder."""
    folder = Path(folder).expanduser()
    try:
        spec = json.loads((folder / "tool.json").read_text())
    except (OSError, ValueError) as exc:
        return None, [f"tool.json missing or not JSON: {exc}"]
    problems = []
    name = spec.get("name", "")
    if not isinstance(name, str) or not NAME.match(name):
        problems.append("name must be 3-41 chars of a-z, 0-9, _ starting with a letter")
    elif name in _builtin_names():
        problems.append(f"name {name!r} is already a built-in tool")
    description = spec.get("description")
    if not isinstance(description, str) or not 20 <= len(description) <= 1000:
        problems.append("description must say what the tool does and when to use it (20-1000 chars)")
    params = spec.get("parameters", {"type": "object", "properties": {}})
    if not isinstance(params, dict) or params.get("type") != "object" or not isinstance(params.get("properties", {}), dict):
        problems.append('parameters must be a JSON schema object: {"type": "object", "properties": {...}}')
    for part in ("run", "test"):
        path = folder / part
        if not path.is_file() or not path.stat().st_mode & 0o111:
            problems.append(f"{part} must be an executable file")
    spec["parameters"] = params if isinstance(params, dict) else {}
    return spec, problems


def _test(folder: Path) -> tuple[bool, str]:
    from ..runtime.permissions import scrubbed_env
    try:
        done = subprocess.run([str(folder / "test")], cwd=folder, capture_output=True, text=True,
                              timeout=TEST_TIMEOUT, env=scrubbed_env())
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"test could not run: {exc}"
    output = (done.stdout + done.stderr).strip()[-1500:]
    return done.returncode == 0, output


def propose(source: str | Path) -> tuple[bool, str]:
    """Validate and test a tool folder, then stage it for the user's approval."""
    source = Path(source).expanduser().resolve()
    spec, problems = validate(source)
    if problems:
        return False, "Not proposed: " + "; ".join(problems)
    passed, output = _test(source)
    if not passed:
        return False, f"Not proposed: its test failed:\n{output}"
    target = PROPOSED_DIR / spec["name"]
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(source, target, ignore=shutil.ignore_patterns("__pycache__"))
    log.info("Tool proposed: %s from %s", spec["name"], source)
    return True, (f"Proposed {spec['name']} (test passed). Install it with `install {spec['name']}`: that asks the "
                  "user for approval, and once approved it is usable at once.")


def approval_summary(name: str) -> str:
    """What the user is asked to approve (shown in the approval request)."""
    folder = PROPOSED_DIR / name
    spec, problems = validate(folder)
    if spec is None:
        return f"installs the assistant tool {name!r} (no proposal found)"
    try:
        run = (folder / "run").read_text()[:600]
    except (OSError, UnicodeDecodeError):
        run = "(binary)"
    return (f"installs a tool the assistant wrote, {name!r}: {spec.get('description', '')[:300]} -- review "
            f"{folder}; run begins: {run[:300]!r}")


APPROVED_ENV = "OMARCHY_AI_TOOL_INSTALL_APPROVED"
APPROVAL_SECONDS = 600


def user_approves_install(name: str) -> tuple[bool, str]:
    """Ask the user, every time, before a tool is installed.

    2026-09-28: `omarchy-ai-tool install` said "ALWAYS asks the user first" but
    only the Task Runtime asked (permissions.py marks the command always_ask);
    run directly, it asked nothing, and Claude Code installed a user tool
    without the user's approval. Now the command asks: a y/N prompt in a
    terminal, otherwise a desktop Approve/Deny notification (no answer = no).
    The only pass: the runtime, after the user approved that exact install
    command, sets APPROVED_ENV to the tool's name. Same-user boundary, as
    the module docstring says: it stops mistakes and agents, not the user.
    """
    import os
    import sys
    if os.environ.get(APPROVED_ENV) == name:
        return True, "approved in the Task Runtime"
    summary = approval_summary(name)
    if sys.stdin.isatty():
        answer = input(f"{summary}\nInstall the assistant tool {name!r}? [y/N] ").strip().lower()
        return answer in ("y", "yes"), "answered in the terminal"
    # 2026-09-30: the user looked for this on the phone and it was not there --
    # only the desktop notification could answer it. It is listed with the
    # phone's approvals now (a fingerprint approves, Deny declines), and
    # whichever answers first decides.
    # The daemon turns the request into a card in the phone's chats and says
    # so on the phone (core/conversations.sync_installs): this process may
    # be a CLI whose memory the daemon never sees.
    request = _open_install_request(name, summary)
    desktop: dict = {}

    def ask_desktop():
        try:
            proc = subprocess.run(["notify-send", "-a", "Omarchy AI", "-u", "critical", "-A", "approve=Install",
                                   "-A", "deny=Don't install", f"Install assistant tool: {name}?", summary[:600]],
                                  capture_output=True, text=True, timeout=APPROVAL_SECONDS)
            desktop["choice"] = proc.stdout.strip() or "dismissed"
        except subprocess.TimeoutExpired:
            desktop["choice"] = "timeout"
        except FileNotFoundError:
            desktop["choice"] = None  # no desktop notifications here: the phone is the only way to answer
    import threading
    threading.Thread(target=ask_desktop, daemon=True).start()
    deadline = time.monotonic() + APPROVAL_SECONDS
    try:
        while time.monotonic() < deadline:
            decided = _install_requests().get(request, {}).get("decision")
            if decided is not None:
                return bool(decided["approve"]), decided["how"]
            choice = desktop.get("choice")
            if choice in ("approve", "deny"):
                return choice == "approve", "approved on the desktop" if choice == "approve" else "declined on the desktop"
            if choice == "dismissed":
                return False, "the notification was dismissed"
            if choice == "timeout":
                break
            time.sleep(0.2)
        return False, f"no answer within {APPROVAL_SECONDS // 60} minutes"
    finally:
        _close_install_request(request)


INSTALL_REQUESTS = STATE_DIR / "tool_install_requests.json"


def _install_requests() -> dict:
    try:
        data = json.loads(INSTALL_REQUESTS.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_install_requests(data: dict) -> None:
    INSTALL_REQUESTS.parent.mkdir(parents=True, exist_ok=True)
    tmp = INSTALL_REQUESTS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    tmp.replace(INSTALL_REQUESTS)


def _open_install_request(name: str, summary: str) -> str:
    import secrets
    request = secrets.token_hex(8)
    data = _install_requests()
    data[request] = {"name": name, "summary": summary[:1500], "asked_at": time.time(), "decision": None}
    _write_install_requests(data)
    return request


def _close_install_request(request: str) -> None:
    data = _install_requests()
    if data.pop(request, None) is not None:
        _write_install_requests(data)


def pending_install_requests() -> list[dict]:
    """Tool installs waiting for the user, for the phone's approvals list."""
    now = time.time()
    return [{"id": rid, **r} for rid, r in _install_requests().items()
            if r.get("decision") is None and now - float(r.get("asked_at") or 0) < APPROVAL_SECONDS]


def decide_install(request: str, approve: bool, how: str) -> bool:
    """The phone's answer to a waiting install (a fingerprint, or Deny)."""
    data = _install_requests()
    if request not in data or data[request].get("decision") is not None:
        return False
    data[request]["decision"] = {"approve": bool(approve), "how": how}
    _write_install_requests(data)
    return True


def install(name: str) -> tuple[bool, str]:
    """Only after the user approved: callers go through user_approves_install."""
    folder = PROPOSED_DIR / name
    spec, problems = validate(folder)
    if spec is None or problems:
        return False, f"Not installed: {'; '.join(problems) or 'no proposal named ' + name}"
    passed, output = _test(folder)
    if not passed:
        return False, f"Not installed: its test failed now:\n{output}"
    target = TOOLS_DIR / name
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(folder), str(target))
    ledger = _ledger()
    ledger[name] = {"approved_at": time.strftime("%Y-%m-%d %H:%M"), "sha256": _hashes(target)}
    LEDGER.write_text(json.dumps(ledger, indent=1))
    log.info("Tool installed: %s", name)
    return True, (f"Installed {name}. The voice assistant can use it now (use_tool), and this task can run it with "
                  f"`run {name} '<json args>'`.")


def remove(name: str) -> tuple[bool, str]:
    ledger = _ledger()
    target = TOOLS_DIR / name
    if name not in ledger and not target.exists():
        return False, f"no installed tool named {name}"
    ledger.pop(name, None)
    LEDGER.write_text(json.dumps(ledger, indent=1))
    if target.exists():
        shutil.rmtree(target)
    return True, f"Removed {name}."


def _approved(name: str) -> Path | None:
    record = _ledger().get(name)
    folder = TOOLS_DIR / name
    if not record or not folder.is_dir() or not NAME.match(name):
        return None
    if _hashes(folder) != record.get("sha256"):
        log.warning("Tool %s changed since it was approved; not offered until approved again", name)
        return None
    return folder


def installed() -> list[dict]:
    tools = []
    for name, record in sorted(_ledger().items()):
        folder = _approved(name)
        if folder is None:
            continue
        spec, problems = validate(folder)
        if spec and not problems:
            tools.append({**spec, "approved_at": record.get("approved_at")})
    return tools


def schemas() -> list[dict]:
    """Catalog entries (same shape as execution/tools.py)."""
    return [{"type": "function", "name": t["name"], "parameters": t["parameters"],
             "description": f"{t['description']} [a tool the assistant wrote; approved {t['approved_at']}]"}
            for t in installed()]


def exists(name: str) -> bool:
    return _approved(name) is not None


def run(name: str, args: dict):
    from .actions import ActionResult
    from ..runtime.permissions import scrubbed_env
    folder = _approved(name)
    if folder is None:
        return ActionResult(False, f"{name} is not an approved tool (or changed since it was approved)")
    try:
        done = subprocess.run([str(folder / "run")], cwd=folder, input=json.dumps(args or {}), capture_output=True,
                              text=True, timeout=RUN_TIMEOUT, env=scrubbed_env())
    except subprocess.TimeoutExpired:
        return ActionResult(False, f"{name} timed out after {RUN_TIMEOUT}s")
    except OSError as exc:
        return ActionResult(False, f"{name} could not start: {exc}")
    output = (done.stdout.strip() or done.stderr.strip())[:OUTPUT_CHARS]
    if done.returncode:
        return ActionResult(False, f"{name} failed (exit {done.returncode}): {output or 'no output'}")
    return ActionResult(True, output or "done")
