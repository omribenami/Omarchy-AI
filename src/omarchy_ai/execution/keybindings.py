"""A data-driven command list instead of a hand-written function per
Omarchy keybinding.

Omarchy's own `omarchy-menu-keybindings` script already resolves every
bind (228 of them) to its real dispatcher + argument, including the tricky
cases a naive reimplementation would get wrong: Lua-dispatched binds only
expose an opaque internal id via `hyprctl binds -j` (dispatcher: "__lua",
arg: "6"), not the actual Lua expression — the menu script re-derives that
from the source .lua files itself. Rather than duplicate that resolution
logic (and risk it silently drifting out of sync with Omarchy's own), this
sources the script's bash functions directly and calls them.

`--print` is forced via `$1` before sourcing specifically so the script's
own bottom-of-file dispatch takes the safe, non-interactive branch
(`output_keybindings`) instead of popping the fzf/walker selection menu —
confirmed live that this does not trigger the interactive UI.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass

from rapidfuzz import fuzz

from .desktop_env import desktop_env

log = logging.getLogger("omarchy_ai.execution.keybindings")

_MENU_SCRIPT = "/usr/share/omarchy/bin/omarchy-menu-keybindings"
_TIMEOUT = 10

_EXTRACT_RECORDS = f"""
set -- --print
source {_MENU_SCRIPT} >/dev/null 2>&1
output_binding_records
"""

_DISPATCH_ONE = f"""
set -- --print
source {_MENU_SCRIPT} >/dev/null 2>&1
dispatch_binding "$DISPATCHER" "$ARG"
"""


@dataclass
class Binding:
    keys: str
    title: str
    dispatcher: str
    arg: str


def _load() -> list[Binding]:
    try:
        proc = subprocess.run(
            ["bash", "-c", _EXTRACT_RECORDS],
            capture_output=True, text=True, timeout=_TIMEOUT, check=False,
            env=desktop_env(),
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        log.warning("failed to load keybindings: %s", proc.stderr[:300])
        return []

    bindings = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        left = parts[0]
        dispatcher = parts[1] if len(parts) > 1 else ""
        arg = parts[2] if len(parts) > 2 else ""
        if "→" in left:
            keys, title = left.split("→", 1)
        else:
            keys, title = "", left
        bindings.append(Binding(keys.strip(), title.strip(), dispatcher, arg))
    return bindings


def _jev_rank(query: str, bindings: list[Binding], limit: int) -> list[dict] | None:
    """One Jev Choice over every binding (230 today, under Choice's 255).

    Real probe on this machine's bindings (2026-09-22): lexical WRatio put
    the right command first for 4/9 requests ("pick a color" -> "Expand
    window left a little", "lock the screen" -> "Swap window to the left",
    nothing at all for Hebrew "צלמי מסך"); Jev got 9/9 in ~0.4s. Returns
    None when Jev is unavailable, so the caller falls back to lexical."""
    if not 1 <= len(bindings) <= 255:
        return None
    try:
        from ..core.jev import Jev, JevError, choice
        criteria = {str(i): b.title + (f" ({b.keys})" if b.keys else "") for i, b in enumerate(bindings)}
        answer = Jev().ask({"request": query}, {"which": choice(
            "Which of these desktop commands does the user's `request` ask to run?", criteria)},
            timeout=4, retries=1)["which"]
    except (JevError, ValueError) as exc:
        log.info("Jev command ranking unavailable, using lexical: %s", str(exc)[:120])
        return None
    ranked = sorted(answer["probabilities"].items(), key=lambda kv: -kv[1])
    return [{"title": bindings[int(k)].title, "keys": bindings[int(k)].keys, "probability": round(p, 2)}
            for k, p in ranked[:limit] if p >= 0.01]


def _runnable(binding: Binding) -> bool:
    """Mouse gestures (drag to move/resize, wheel scrolling) cannot be run as
    a command. Offering them let "move window" resolve to the SUPER+LEFT
    MOUSE drag at p=0.99 in a real session, and nothing happened."""
    keys = binding.keys.upper()
    return "MOUSE" not in keys


def list_commands(query: str | None, limit: int = 15) -> list[dict]:
    bindings = [b for b in _load() if _runnable(b)]
    if query:
        ranked = _jev_rank(query, bindings, min(limit, 5))
        if ranked:
            return ranked
        scored = sorted(
            bindings,
            key=lambda b: fuzz.WRatio(query.lower(), b.title.lower()),
            reverse=True,
        )
        bindings = [b for b in scored if fuzz.WRatio(query.lower(), b.title.lower()) > 40]
    return [{"title": b.title, "keys": b.keys} for b in bindings[:limit]]


def execute_command(title: str) -> tuple[bool, str]:
    bindings = _load()
    if not bindings:
        return False, "could not load the command list"
    best = max(bindings, key=lambda b: fuzz.WRatio(title.lower(), b.title.lower()))
    score = fuzz.WRatio(title.lower(), best.title.lower())
    if score < 60:
        return False, f"no command matching '{title}' found"

    env = {**desktop_env(), "DISPATCHER": best.dispatcher, "ARG": best.arg}
    try:
        proc = subprocess.run(
            ["bash", "-c", _DISPATCH_ONE],
            capture_output=True, text=True, timeout=_TIMEOUT, check=False,
            env=env,
        )
    except subprocess.TimeoutExpired:
        return False, f"'{best.title}' timed out"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, err[0] if err else f"'{best.title}' failed"
    return True, f"ran '{best.title}'"
