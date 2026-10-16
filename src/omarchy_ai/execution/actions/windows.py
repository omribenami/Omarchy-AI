"""Hyprland window and workspace targeting, focus and movement.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path

from .base import ActionResult, _hyprctl_dispatch, _run
from .desktop import SCREEN_LOCKED, _screen_locked

log = logging.getLogger("omarchy_ai.execution.actions")


# --- window / workspace -----------------------------------------------------

def workspace_switch(args: dict) -> ActionResult:
    number = args.get("number")
    if not isinstance(number, int) or not (1 <= number <= 99):
        return ActionResult(False, "no valid workspace number given")
    return _hyprctl_dispatch(
        f"hl.dsp.focus({{ workspace = {number} }})", ["workspace", str(number)]
    )


def workspace_next(args: dict) -> ActionResult:
    return _hyprctl_dispatch('hl.dsp.focus({ workspace = "e+1" })', ["workspace", "e+1"])


def workspace_prev(args: dict) -> ActionResult:
    return _hyprctl_dispatch('hl.dsp.focus({ workspace = "e-1" })', ["workspace", "e-1"])


def close_window(args: dict) -> ActionResult:
    return _hyprctl_dispatch("hl.dsp.window.close()", ["killactive"])


def window_fullscreen_toggle(args: dict) -> ActionResult:
    return _hyprctl_dispatch("hl.dsp.window.fullscreen()", ["fullscreen"])


# Wrappers between a terminal and what the user is actually running.
_WRAPPER_COMMS = {"bash", "zsh", "fish", "sh", "dash", "nu", "script", "tmux", "screen", "login",
                  "sudo", "su", "env", "xdg-terminal-exec", "uwsm-app", "setsid"}


def _process_children() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    try:
        pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit()]
    except OSError:
        return children
    for pid in pids:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
            children.setdefault(int(stat[1]), []).append(pid)
        except (OSError, ValueError, IndexError):
            continue
    return children


def _running_program(root_pid, children) -> str | None:
    """The shallowest non-wrapper process under a terminal: "claude",
    "codex", "nvim", "htop"... Claude Code titles its window "✳ <task>", so
    without this "the Claude terminal" matched nothing at all."""
    if not root_pid:
        return None
    level = list(children.get(root_pid, []))
    for _ in range(8):
        next_level = []
        for pid in sorted(level):
            try:
                comm = Path(f"/proc/{pid}/comm").read_text().strip()
            except OSError:
                continue
            if comm and comm not in _WRAPPER_COMMS:
                return comm
            next_level.extend(children.get(pid, []))
        if not next_level:
            return None
        level = next_level
    return None


# ssh options that take a value (man ssh): the destination is the first
# argument that is neither an option nor an option's value.
_SSH_VALUE_FLAGS = set("BbcDEeFIiJLlmOoPpQRSWw")


def _ssh_destination(argv: list[str]) -> str | None:
    """user@host from an ssh command line ("ssh -p 2222 -l me box" -> me@box)."""
    user, i = None, 1
    while i < len(argv):
        arg = argv[i]
        if arg == "--":
            i += 1
            break
        if not arg.startswith("-") or arg == "-":
            break
        for j, flag in enumerate(arg[1:], start=1):
            if flag in _SSH_VALUE_FLAGS:
                value = arg[j + 1:]
                if not value:
                    i += 1
                    value = argv[i] if i < len(argv) else ""
                if flag == "l":
                    user = value
                break
        i += 1
    if i >= len(argv) or not argv[i]:
        return None
    dest = argv[i]
    if dest.startswith("ssh://"):
        dest = dest[6:].split("/", 1)[0].rsplit(":", 1)[0]
    return f"{user}@{dest}" if user and "@" not in dest else dest


def _remote_session(root_pid, children) -> str | None:
    """The ssh destination a terminal is connected to, if any.

    Real bug (2026-09-24 22:42-22:44): the user's ssh terminal to the home
    server was closed, then `docker ps` in a local terminal came back empty
    and the assistant told the user "no containers on this server" and
    started the Minecraft compose on THIS machine. Nothing in list_windows
    said which machine a terminal was on; the title alone was not enough."""
    level = list(children.get(root_pid, [])) if root_pid else []
    for _ in range(8):
        next_level = []
        for pid in sorted(level):
            try:
                if Path(f"/proc/{pid}/comm").read_text().strip() in ("ssh", "mosh-client"):
                    argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode(errors="replace").rstrip("\0")
                    return _ssh_destination(argv.split("\0")) or "unknown host"
            except OSError:
                continue
            next_level.extend(children.get(pid, []))
        if not next_level:
            return None
        level = next_level
    return None


def _with_programs(clients: list[dict]) -> list[dict]:
    children = None
    for c in clients:
        if any(k in str(c.get("class") or "").lower() for k in _WINDOW_KINDS["terminal"]):
            children = children if children is not None else _process_children()
            c["running"] = _running_program(c.get("pid"), children)
            remote = _remote_session(c.get("pid"), children)
            c["machine"] = (f"REMOTE {remote} (ssh): commands typed here run on that machine"
                            if remote else f"local ({socket.gethostname()}): this computer")
    return clients


def list_windows(args: dict) -> ActionResult:
    """The model has no idea what's actually on screen otherwise — this is
    what lets it target the *right* window instead of guessing that
    "focused" already means whichever one the user is talking about."""
    r = _run(["hyprctl", "clients", "-j"])
    if not r.ok:
        return r
    active = _run(["hyprctl", "activewindow", "-j"])
    active_address = None
    if active.ok:
        try:
            active_address = json.loads(active.message).get("address")
        except (json.JSONDecodeError, AttributeError):
            pass
    try:
        clients = json.loads(r.message)
    except json.JSONDecodeError:
        return ActionResult(False, "could not parse window list")
    clients = _with_programs(clients)
    windows = [
        {
            "address": c.get("address"),
            "app": c.get("class"),
            "title": c.get("title"),
            **({"running": c["running"]} if c.get("running") else {}),
            **({"machine": c["machine"]} if c.get("machine") else {}),
            "workspace": (c.get("workspace") or {}).get("id"),
            "fullscreen": bool(c.get("fullscreen")),
            "focused": c.get("address") == active_address,
        }
        for c in clients
        if c.get("mapped")
    ]
    return ActionResult(True, json.dumps(windows))


def _matching_windows(targets, clients: list[dict]) -> list[dict]:
    """Same fuzzy app/title substring match focus_window uses, applied per
    target in a list. First match wins per target; already-matched windows
    are skipped so two vague targets can't both resolve to the same window."""
    chosen: list[dict] = []
    seen_addresses: set = set()
    for target in targets:
        needle = str(target or "").strip().lower()
        if not needle:
            continue
        match = next(
            (
                c for c in clients
                if c.get("address") not in seen_addresses
                and (needle in (c.get("class") or "").lower() or needle in (c.get("title") or "").lower())
            ),
            None,
        )
        if match is not None:
            chosen.append(match)
            seen_addresses.add(match.get("address"))
    return chosen


def show_window_labels(args: dict) -> ActionResult:
    """Floating name-label badges pinned over each candidate window's real
    on-screen rectangle — the visual "which window do you mean?" aid,
    rendered by the omarchy-ai.window-labels Quickshell plugin (same IPC
    pattern as omarchy-osd). Geometry comes straight from hyprctl clients -j
    (same shape list_windows already parses), so the badges track this
    machine's actual current window layout rather than a guess."""
    targets = args.get("targets")
    if targets is not None and not isinstance(targets, list):
        return ActionResult(False, "targets must be a list of strings or omitted")

    r = _run(["hyprctl", "clients", "-j"])
    if not r.ok:
        return r
    try:
        clients = json.loads(r.message)
    except json.JSONDecodeError:
        return ActionResult(False, "could not parse window list")
    mapped = [c for c in clients if c.get("mapped")]

    chosen = _matching_windows(targets, mapped) if targets else mapped
    if not chosen:
        return ActionResult(False, "no matching windows found")

    windows = []
    for c in chosen:
        at = c.get("at") or [0, 0]
        size = c.get("size") or [0, 0]
        label = c.get("title") or c.get("class") or "window"
        # Long titles would draw a chip wider than the window it's labeling
        # — the plugin elides overflow but has no cap on the chip's own
        # width, so keep it from growing unbounded in the first place.
        if len(label) > 40:
            label = label[:39].rstrip() + "…"
        windows.append({"x": at[0], "y": at[1], "width": size[0], "height": size[1], "label": label})

    payload = json.dumps({"windows": windows})
    r2 = _run(["omarchy-shell", "-q", "windowLabels", "show", payload])
    if not r2.ok:
        return r2
    return ActionResult(True, f"labeled {len(windows)} window(s)")


def hide_window_labels(args: dict) -> ActionResult:
    return _run(["omarchy-shell", "-q", "windowLabels", "hide"])


# Generic words people use for a kind of window, matched against app class.
_WINDOW_KINDS = {
    "terminal": ("foot", "kitty", "alacritty", "ghostty", "wezterm", "konsole", "terminal"),
    "browser": ("chromium", "chrome", "firefox", "brave", "zen", "vivaldi"),
    "editor": ("code", "nvim", "neovide", "zed", "gedit", "kate", "sublime"),
    "files": ("nautilus", "thunar", "dolphin", "files"),
}
_THIS_WINDOW = {"focused", "current", "this", "active", "focused window", "current window",
                "focused terminal", "current terminal", "this terminal", "the focused terminal"}


def _rank_windows(clients: list[dict], needle: str, active_address, active_workspace):
    """Matching windows, best first.

    Real bug (2026-09-22): the first hyprctl client whose class/title
    contained "terminal" won. On workspace 5 that was a terminal on workspace
    1 (its title happened to contain "Terminal"), so every "type in the
    terminal" jumped the user to workspace 1, twice in a row. Now the
    focused window wins, then windows on the current workspace, then the
    most recently used; generic words ("terminal") match by app kind."""
    kinds = next((names for word, names in _WINDOW_KINDS.items()
                  if needle in (word, "the " + word, word + "s")), None)
    matches = []
    for c in clients:
        if not c.get("mapped"):
            continue
        app, title = (c.get("class") or "").lower(), (c.get("title") or "").lower()
        running = (c.get("running") or "").lower()
        if kinds is not None:
            hit = any(k in app for k in kinds)
        else:
            words = [w for w in needle.replace("the ", " ").replace(" terminal", " ").replace(" window", " ").split() if w]
            hit = needle in app or needle in title or bool(running and any(w == running for w in words))
        if hit:
            matches.append(c)
    def key(c):
        return (c.get("address") != active_address,
                (c.get("workspace") or {}).get("id") != active_workspace,
                c.get("focusHistoryID", 1_000))
    return sorted(matches, key=key)


def move_window_to_workspace(args: dict) -> ActionResult:
    """One Hyprland dispatch, then verified -- no command search, no model
    round trip. Real session (2026-09-23 00:11): "move the window to
    workspace 4" went list_windows -> list_commands("move window") -> the
    SUPER+LEFT MOUSE drag binding at p=0.99, and nothing happened."""
    try:
        number = int(args.get("number"))
    except (TypeError, ValueError):
        return ActionResult(False, "number must be a workspace number")
    if not 1 <= number <= 99:
        return ActionResult(False, "workspace number must be 1-99")
    follow = bool(args.get("follow", False))
    target = str(args.get("target") or "focused").strip()
    r = _run(["hyprctl", "clients", "-j"])
    active = _run(["hyprctl", "activewindow", "-j"])
    workspace = _run(["hyprctl", "activeworkspace", "-j"])
    try:
        clients = _with_programs(json.loads(r.message)) if r.ok else []
        active_address = json.loads(active.message).get("address") if active.ok else None
        active_workspace = json.loads(workspace.message).get("id") if workspace.ok else None
    except (ValueError, AttributeError):
        return ActionResult(False, "could not read windows")
    if target.startswith("0x"):
        address = target
    elif target.lower() in _THIS_WINDOW:
        address = active_address
    else:
        ranked = _rank_windows(clients, target.lower(), active_address, active_workspace) or \
            _jev_window(clients, target, active_workspace)
        address = ranked[0]["address"] if ranked else None
    if not address:
        return ActionResult(False, f"no window matching '{target}' found")
    result = _hyprctl_dispatch(
        f'hl.dsp.window.move({{ workspace = {number}, window = "address:{address}", follow = {"true" if follow else "false"} }})',
        [("movetoworkspace" if follow else "movetoworkspacesilent"), f"{number},address:{address}"])
    if not result.ok:
        return result
    for _ in range(10):
        after = _run(["hyprctl", "clients", "-j"])
        try:
            moved = next((c for c in json.loads(after.message) if c.get("address") == address), None)
        except ValueError:
            moved = None
        if moved and (moved.get("workspace") or {}).get("id") == number:
            title = str(moved.get("title") or moved.get("class") or address)[:60]
            return ActionResult(True, f"verified: {title!r} is now on workspace {number}"
                                      + ("; view followed it" if follow else "; view stayed where it was"))
        time.sleep(0.03)
    return ActionResult(False, f"move dispatched but window {address} is not verified on workspace {number}")


def _jev_window(clients: list[dict], target: str, active_workspace) -> list[dict]:
    """Semantic fallback when no window matches by name ("the one with the
    drone project", "the Codex window"): one Jev Choice over the mapped
    windows. Only a clear pick counts; otherwise report no match rather than
    focus a guess."""
    mapped = [c for c in clients if c.get("mapped")][:100]
    if not mapped:
        return []
    try:
        from ...core.jev import Jev, JevError, choice
        criteria = {"none": "No listed window is the one meant."}
        for i, c in enumerate(mapped):
            criteria[str(i)] = json.dumps({"app": c.get("class"), "title": c.get("title"),
                                           "running": c.get("running"),
                                           "workspace": (c.get("workspace") or {}).get("id"),
                                           "on_current_workspace": (c.get("workspace") or {}).get("id") == active_workspace},
                                          ensure_ascii=False)
        answer = Jev().ask({"requested_window": target}, {"which": choice(
            "Which listed window is `requested_window`? Choose none if no window clearly fits.", criteria)},
            timeout=4, retries=1)["which"]
    except (JevError, ValueError) as exc:
        log.info("Jev window resolution unavailable: %s", str(exc)[:120])
        return []
    # Real probe: "my shell in the tello folder" -> the right window at 0.68
    # (none 0.28); "the terminal with the drone project" split 0.39/0.26
    # between two Tello windows. A clear lead is required, not a high bar.
    runner_up = sorted(answer["probabilities"].values(), reverse=True)[1]
    if answer["choice"] == "none" or answer["p"] < 0.6 or answer["p"] < 2 * runner_up:
        return []
    return [mapped[int(answer["choice"])]]


def focus_window(args: dict) -> ActionResult:
    target = (args.get("target") or "").strip()
    if not target:
        return ActionResult(False, "no window specified")
    address = target
    note = ""
    if not target.startswith("0x"):
        r = _run(["hyprctl", "clients", "-j"])
        if not r.ok:
            return r
        try:
            clients = json.loads(r.message)
        except json.JSONDecodeError:
            return ActionResult(False, "could not parse window list")
        active_address = active_workspace = None
        active = _run(["hyprctl", "activewindow", "-j"])
        workspace = _run(["hyprctl", "activeworkspace", "-j"])
        try:
            active_address = json.loads(active.message).get("address") if active.ok else None
            active_workspace = json.loads(workspace.message).get("id") if workspace.ok else None
        except (ValueError, AttributeError):
            pass
        needle = target.lower()
        if needle in _THIS_WINDOW and active_address:
            address = active_address
            match = next((c for c in clients if c.get("address") == address), {})
        else:
            clients = _with_programs(clients)
            ranked = _rank_windows(clients, needle, active_address, active_workspace)
            if not ranked:
                # An assistant terminal's label ("Omarchy AI 0d592f0a", from
                # its log) outlives its title once the shell renames it:
                # journal 2026-09-27 13:36 and 09-28 15:43, both escalated.
                from .. import tile_logs
                labelled = tile_logs.address_for_label(target)
                ranked = [c for c in clients if labelled and c.get("address") == labelled]
            if not ranked:
                ranked = _jev_window(clients, target, active_workspace)
            if not ranked:
                return ActionResult(False, f"no window matching '{target}' found")
            match = ranked[0]
            address = match["address"]
        workspace_id = (match.get("workspace") or {}).get("id")
        if active_workspace is not None and workspace_id not in (None, active_workspace):
            note = (f" -- NOTE: it is on workspace {workspace_id}, so the view moved there from workspace "
                    f"{active_workspace}")
    result = _hyprctl_dispatch(
        f'hl.dsp.focus({{ window = "address:{address}" }})',
        ["focuswindow", f"address:{address}"],
    )
    if not result.ok:
        return result
    for _ in range(5):
        active = _run(["hyprctl", "activewindow", "-j"])
        try:
            if active.ok and json.loads(active.message).get("address") == address:
                return ActionResult(True, f"verified focus on {address}{note}")
        except (ValueError, AttributeError):
            pass
        time.sleep(0.04)
    # Journal 2026-09-27 08:17 and 15:03: every focus of an assistant
    # terminal "failed" while the screen was locked (the lock screen keeps
    # focus), she retried with other names, and it escalated twice.
    if _screen_locked():
        return ActionResult(False, SCREEN_LOCKED)
    return ActionResult(False, f"focus dispatch returned but {address} is not verified active")
