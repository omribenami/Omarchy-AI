"""One tool registry and one dispatch path for every conversation (plan 4.6).

Every surface that lets a model call tools -- the desktop OpenAI live session
(voice/live.py through voice/tool_dispatch.py), the Gemini voice session and
everything built on it (voice/gemini_live.py: desktop voice, the Gateway
realtime transport voice/gateway_live.py, the typed chat voice/text_chat.py,
the phone's Gemini call phone/gemini.py, the speaker satellite
voice/satellite.py), the Gateway STT/TTS pipeline (voice/omarchy.py) and the
phone bridge's /api/tool (phone/server.py, the phone's OpenAI call) -- runs a
call here:

    result = await dispatch.arun(owner, guard, name, args, tag=call_id)
    result = dispatch.run(owner, guard, name, args)      # a server thread

That is the ToolRunner (execution/runner.py: the tool's deadline, cancel,
worker routing, fencing of untrusted results through execution/provenance.py)
wrapped in the conversation's InputGuard (execution/verified_input.py: the
approval policy of core/approvals.py, taint, verified keyboard focus, the
session-ending and schedule-that-sends confirmations, same-turn duplicate
suppression). What stays in the sessions is their own I/O: audio,
transcripts, how a result is handed back to their model, and when they tell
the user (core/outbox.py). tests/test_parity.py holds them to it, with the
differences that are on purpose listed there, each with its reason.

registry_tools() is the one tool list: every session declares it (the
Gemini sessions may declare the core of it plus use_tool, the rest reached
through execution/catalog.py), adding only end_conversation and its
surface's own tools.
"""

from __future__ import annotations

import json
from typing import Any, Callable, Iterable

from . import runner
from .actions import run_action
from .tools import MYAPI_TOOLS, TOOLS
from .verified_input import InputGuard

Execute = Callable[[str, dict], Any]

# Gemini's function calling (and a Gateway model imitating it) sometimes
# names a tool "default_api.<name>": only that observed namespace is removed.
_NAMESPACE = "default_api."

# Answered by the session from its own action log, never by run_action.
RECENT_ACTIONS = "get_recent_actions"


def myapi_on(config: Any) -> bool:
    """Whether the MyApi tools are declared: enabled and connected."""
    if not getattr(config, "myapi_enabled", False):
        return False
    from .. import myapi

    return bool(myapi.is_connected())


def registry_tools(myapi_connected: bool) -> list[dict]:
    """The tools every session offers (Responses API shape): the built-in
    ones, MyApi's when connected, and the tools the user added
    (execution/user_tools.py) -- the same set execution/catalog.py offers
    through use_tool."""
    from . import user_tools

    return [*TOOLS, *(MYAPI_TOOLS if myapi_connected else []), *user_tools.schemas()]


def tool_name(name: str) -> str:
    return name[len(_NAMESPACE) :] if name.startswith(_NAMESPACE) else name


def known(name: str, myapi_connected: bool, extra: Iterable[str] = ()) -> bool:
    """A tool this conversation declared (the registry, plus its own)."""
    names = {t["name"] for t in registry_tools(myapi_connected)}
    return name in names or name in set(extra)


def _wrap(guard: InputGuard, guarded: bool) -> runner.Wrap | None:
    return guard.run if guarded else None


def _settled(guard: InputGuard, result: runner.ToolResult) -> runner.ToolResult:
    # Untrusted content taints the conversation however the call ran.
    if result.untrusted:
        guard.taint.mark(result.provenance)
    return result


async def arun(
    owner: object,
    guard: InputGuard,
    name: str,
    args: dict,
    *,
    execute: Execute | None = None,
    tag: object = None,
    guarded: bool = True,
) -> runner.ToolResult:
    """Run one of the conversation's calls (cancelled with `owner`, or by `tag`)."""
    result = await runner.default_runner().arun(
        name,
        args,
        execute=execute or run_action,
        wrap=_wrap(guard, guarded),
        owner=owner,
        tag=tag,
    )
    return _settled(guard, result)


def run(
    owner: object,
    guard: InputGuard,
    name: str,
    args: dict,
    *,
    execute: Execute | None = None,
    tag: object = None,
    guarded: bool = True,
) -> runner.ToolResult:
    """arun() for a thread that may block (the phone bridge's request)."""
    result = runner.default_runner().run(
        name,
        args,
        execute=execute or run_action,
        wrap=_wrap(guard, guarded),
        owner=owner,
        tag=tag,
    )
    return _settled(guard, result)


def recent_actions(log: list[dict], target: str | None) -> str:
    """get_recent_actions over a conversation's action log: the last 20,
    or those on windows whose app or title has `target` in it."""
    entries = log
    if target:
        needle = target.lower()
        entries = [
            e
            for e in entries
            if needle in ((e.get("window") or {}).get("app") or "").lower()
            or needle in ((e.get("window") or {}).get("title") or "").lower()
        ]
    return json.dumps(entries[-20:])
