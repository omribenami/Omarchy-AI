"""User terminals (tile logs) and the assistant's own tmux terminals.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

import logging
import re

from ...config import load_config
from .. import sudo_approval
from .base import ActionResult, _run_detached

log = logging.getLogger("omarchy_ai.execution.actions")


def open_terminal(args: dict) -> ActionResult:
    # Wraps the launch so this terminal's output gets tracked in a
    # per-tile log (see tile_logs.py) — read_tile_log can then answer
    # "what happened in that terminal" from real text instead of a
    # describe_screen vision call. Falls back to a plain, untracked
    # launch if tile-log setup itself fails for any reason; a terminal
    # that opens without logging is far better than one that doesn't
    # open at all.
    try:
        from .. import tile_logs

        _initial_log_path, argv_prefix, terminal_label = tile_logs.start_terminal_log()
        result = _run_detached(["omarchy-launch-terminal", *argv_prefix])
        if result.ok:
            result.message = (
                f"opened {terminal_label}; use that exact title when focusing it "
                "or reading its terminal log"
            )
        return result
    except Exception:  # noqa: BLE001
        log.exception("tile_logs setup failed, opening terminal untracked")
        return _run_detached(["omarchy-launch-terminal"])


def read_tile_log(args: dict) -> ActionResult:
    from .. import tile_logs, workbench

    window = args.get("window")
    if window and not str(window).startswith("0x") and workbench.exists(str(window)):
        # An assistant terminal from terminal_task (2026-09-24: 'find-docker-compose').
        return workbench.read(str(window))
    text = tile_logs.read_log(window)
    available = not text.startswith(("no terminal", "more than one terminal", "failed to read log:"))
    return ActionResult(available, text)


def terminal_task(args: dict) -> ActionResult:
    """Run a command in the assistant's own terminal (tmux): visible on the
    user's screen when she is the only operator, in the background while the
    user is working. Nothing is typed through the user's keyboard."""
    from .. import operator, workbench
    command = str(args.get("command") or "").strip()
    if not command:
        return ActionResult(False, "command is required")
    try:
        name = workbench.resolve(args.get("name") or command.split()[0])
    except ValueError as exc:
        return ActionResult(False, str(exc))
    if not workbench.exists(name) or workbench._busy(workbench._session(name)):
        # Reuse an idle terminal instead of opening one more window.
        idle = workbench.idle_terminal()
        if idle and idle != name:
            workbench.aliases[workbench.slug(args.get("name") or command.split()[0])] = idle
            name = idle
    started = workbench.start(name)
    if not started.ok:
        return started
    show = args.get("show", "auto")
    on_screen = operator.visible(show)
    if on_screen:
        shown = workbench.show(name)
        if not shown.ok:
            return shown
    elif show in (None, "", "auto"):
        workbench.pending_handover.add(name)
    from .. import passwords
    machine = workbench.where(name)  # before the command: where it is about to run
    if re.match(r"(sshpass\s+)?(ssh|mosh)\s", command):
        machine += (f" Once it connects, commands for that machine go to terminal_task with name={name!r}; "
                    "any other name runs on this computer.")
    # sshpass/ssh-copy-id: the harness answers the password prompt; a literal
    # `sshpass -p SECRET` never reaches the terminal or its scrollback.
    typed = workbench.send(name, passwords.ssh_terminal_command(command))
    if not typed.ok:
        return typed
    where = ("on the user's screen" if on_screen else
             "in the background because the user is working (it moves to their screen when they stop "
             "touching it for ~30s, or say terminal_show)")
    return ActionResult(True, f"started {command!r} in assistant terminal {name!r}, {where}.{machine} Output is "
                              "NOT verified yet: use terminal_read, or schedule_task watch with "
                              f"terminal={name!r} to be told when it finishes.")


def terminal_read(args: dict) -> ActionResult:
    from .. import workbench
    name = str(args.get("name") or "")
    result = workbench.read(name, int(args.get("lines") or 120), settle=True)
    if not result.ok:
        return result
    return ActionResult(True, f"[{workbench.where(name).strip()}]\n{result.message}")


def terminal_type(args: dict) -> ActionResult:
    from .. import passwords, workbench
    return workbench.send(str(args.get("name") or ""), passwords.ssh_terminal_command(str(args.get("text") or "")),
                          bool(args.get("enter", True)))


def terminal_show(args: dict) -> ActionResult:
    from .. import workbench
    name = str(args.get("name") or "")
    workbench.pending_handover.discard(name)
    return workbench.show(name)


def terminal_hide(args: dict) -> ActionResult:
    from .. import workbench
    return workbench.hide(str(args.get("name") or ""))


def terminal_close(args: dict) -> ActionResult:
    from .. import workbench
    name = str(args.get("name") or "")
    try:
        workbench.pending_handover.discard(workbench.resolve(name))
    except ValueError:
        pass
    return workbench.stop(name)


def terminal_sudo(args: dict) -> ActionResult:
    """Answer the password prompt in an assistant terminal: sudo gets the
    saved Sudo Access password; an SSH/ssh-copy-id/scp prompt gets the
    password the user stated (captured into the keyring), else the saved one
    (2026-09-27: "same password as root" for a home server)."""
    from .. import passwords, workbench
    name = str(args.get("name") or "")
    screen = workbench.read(name, 6, settle=False) if workbench.exists(name) else None
    prompt = next((line for line in reversed((screen.message if screen and screen.ok else "").lower().splitlines())
                   if line.strip()), "")
    if "password" in prompt and "[sudo]" not in prompt:  # "user@host's password:", not "[sudo] password for"
        password = passwords.ssh_password()
        if password is None:
            return ActionResult(False, "no password to give: ask the user to say it (\"password: ...\"), or save "
                                       "one in Assistant Settings > Sudo Access")
        submitted = workbench.submit_password(name, password)
        return ActionResult(submitted.ok, "password submitted to the login prompt" if submitted.ok else submitted.message)
    if not load_config().sudo_access_enabled:
        return ActionResult(False, "persistent Sudo Access is disabled in Assistant Settings")
    if not workbench.exists(name):
        # 2026-09-24 00:28: called twice on the user's own terminal, failed,
        # and she told the user to type the password themselves.
        return ActionResult(False, f"{name!r} is not an assistant terminal from terminal_task. For a regular "
                            "terminal window: focus_window it, read_tile_log to confirm the password prompt, "
                            "then call submit_sudo_password.")
    password = sudo_approval.retrieve()
    if password is None:
        return ActionResult(False, "no sudo password is saved in GNOME Keyring; add it in Assistant Settings")
    return workbench.submit_password(name, password)
