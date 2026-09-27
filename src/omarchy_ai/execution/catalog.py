"""The tool catalog: Jev picks the tool, the live model fills its arguments.

Journals 2026-09-24..26: ~800 live tool calls from 89 declared tools (50k
characters of schema on every connect). Some 20 tools made over 95% of the
calls, and start_task -- the one route that could have filed the Omarchy
issue -- was called once. The live model is fast at words and poor at
choosing among a hundred tools; Jev (TypeSafe) is a fast, calibrated
chooser that never generates text. So:

- The live model is declared only CORE (what it uses constantly) plus
  `use_tool`. Everything else, and every tool the user approves later
  (execution/user_tools.py), is in the catalog.
- use_tool(request) -> one Jev choice over the whole catalog. A pick that
  needs no arguments runs at once; otherwise its parameters go back and the
  model calls use_tool again with name and args (a known name skips Jev).
- The catalog is read fresh on every call, so a tool approved mid-conversation
  is usable in that same conversation, with no restart.

Code owns thresholds, validation and the fallback: if Jev is unreachable the
best keyword matches are returned for the model to choose from.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import re
import time

from .tools import MYAPI_TOOLS, TOOLS, _tool

log = logging.getLogger(__name__)

# Declared to the live model directly: the tools it actually uses turn to turn
# (journal counts above), the ones _run_call handles itself, and the routes
# the prompt tells it to take by default.
CORE = frozenset({
    "type_text", "press_key", "read_tile_log", "list_windows", "focus_window", "describe_screen",
    "list_files", "read_file", "terminal_task", "terminal_read", "workspace_switch", "close_window",
    "browser_task", "desktop_task", "start_task", "task_status", "task_respond",
    "show_window_labels", "hide_window_labels", "get_recent_actions", "run_mission",
})

PICK_P = 0.6          # below this Jev's pick is offered, not run
CANDIDATES = 3
TIMEOUT = 3.0
_DESCRIPTION_CHARS = 180
_NONE = "none"

USE_TOOL = _tool(
    "use_tool",
    "Your other tools (casting, reminders, bar panels, brightness, media, Bluetooth, night light, themes and "
    "Omarchy commands, skills, schedules, updates, GitHub issues, screenshots, browser tabs, files, sudo, MyApi "
    "and tools the user added) are in a catalog. Say what to do in `request` and Jev picks the right tool at "
    "once: one that needs nothing more runs immediately; otherwise you get its parameters and call use_tool "
    "again with `name` and `args`. If you already know the tool's name (e.g. one named in your instructions), "
    "pass `name` and `args` directly.",
    {"type": "object", "properties": {
        "request": {"type": "string", "description": "What to do, resolved and specific (English is fine)."},
        "name": {"type": "string", "description": "Exact catalog tool name, when known."},
        "args": {"type": "object", "description": "That tool's arguments."},
    }})


def catalog(myapi_on: bool = False) -> dict[str, dict]:
    tools = [t for t in TOOLS if t["name"] not in CORE] + (MYAPI_TOOLS if myapi_on else [])
    try:
        from . import user_tools
        tools += user_tools.schemas()
    except ImportError:
        pass
    return {t["name"]: t for t in tools}


def signatures(myapi_on: bool = False) -> str:
    """name(param*, param) per catalog tool, * = required: enough for the live
    model to fill args in one call when it already knows the tool."""
    def sig(t):
        schema = t.get("parameters") or {}
        required = set(schema.get("required") or [])
        return f"{t['name']}({', '.join(k + ('*' if k in required else '') for k in schema.get('properties') or {})})"
    return ", ".join(sig(t) for t in catalog(myapi_on).values())


def declared() -> list[dict]:
    """The live model's function list when the picker is on."""
    return [t for t in TOOLS if t["name"] in CORE] + [USE_TOOL]


@dataclass
class Resolution:
    run: bool                    # True: run `tool` with `args` now
    tool: str = ""
    args: dict = field(default_factory=dict)
    message: str = ""            # when not run: what the model gets back
    picked: bool = False         # Jev chose it (the switchboard would ask the same question again)
    evidence: dict = field(default_factory=dict)


def _summary(tool: dict) -> str:
    text = " ".join(tool["description"].split())
    first = re.split(r"(?<=[.!?])\s", text, maxsplit=1)[0]
    return (first if len(first) >= 40 else text)[:_DESCRIPTION_CHARS]


def _problems(tool: dict, args: dict) -> list[str]:
    schema = tool.get("parameters") or {}
    props = schema.get("properties") or {}
    missing = [k for k in schema.get("required") or [] if k not in args]
    unknown = [k for k in args if props and k not in props]
    return ([f"missing {', '.join(missing)}"] if missing else []) + ([f"unknown {', '.join(unknown)}"] if unknown else [])


def _offer(tools: list[dict], lead: str) -> str:
    return json.dumps({"note": lead + " Call use_tool again with `name` and `args`.",
                       "tools": [{"name": t["name"], "description": t["description"][:400],
                                  "parameters": (t.get("parameters") or {}).get("properties", {}),
                                  "required": (t.get("parameters") or {}).get("required", [])} for t in tools]},
                      ensure_ascii=False)


# Parameters that hold a body of text the user composes (a file's content, an
# edit): never filled from a one-line `request`, or a vague "add that to the
# file" would overwrite the file with an invented body.
_NO_FILL = frozenset({"content", "old_text", "new_text", "body", "text"})

_FILL_SYSTEM = (
    "Fill the arguments of one tool call. Input: the tool (name, description, JSON-schema parameters), `request` "
    "(what the voice assistant asked for) and `user_said` (speech recognition, possibly garbled). Return a JSON "
    "object {\"call_arguments\": {...}} holding the tool's parameters, exactly as its schema and description say (follow the "
    "description's own examples for the exact values). Use only what the request, the user's words and the "
    "description support; leave out any parameter you would have to guess."
)


def fill_args(tool: dict, request: str, heard: str, complete_json) -> dict:
    """The tool's arguments from the request, by the Gateway text model."""
    answer = complete_json(_FILL_SYSTEM, {
        "tool": {"name": tool["name"], "description": tool["description"], "parameters": tool.get("parameters") or {}},
        "request": request[:600], "user_said": heard[-300:]})
    # Not "args": run_omarchy_command's own parameter is called args.
    args = answer.get("call_arguments")
    return args if isinstance(args, dict) else {}


def _normalize(args: dict, tools: dict[str, dict]) -> tuple[str, dict]:
    """(name, args) from the shapes the live model actually sends.

    Journal 2026-09-27: {"name": "list_commands", "query": "screen recording"}
    (the tool's parameter beside `name`, not in `args`) and {"args": {"name":
    "run_omarchy_command", "args": [...]}} (the name inside `args`)."""
    name = str(args.get("name") or "").strip()
    given = dict(args["args"]) if isinstance(args.get("args"), dict) else {}
    if not name and str(given.get("name") or "") in tools:
        name = given.pop("name")
    props = ((tools.get(name) or {}).get("parameters") or {}).get("properties") or {}
    for key, value in args.items():
        if key not in ("name", "args", "request") and key in props:
            given.setdefault(key, value)
    return name, given


def _complete(tool: dict, given: dict, request: str, heard: str, fill) -> dict | None:
    """`given` plus arguments filled from the request, when that makes a valid
    call. Journal 2026-09-27 01:33: use_tool(name=run_omarchy_command,
    request="stop screen recording") with no args got the schema back ~60
    times in 50s; the model never moved the request into args."""
    schema = tool.get("parameters") or {}
    missing = [k for k in schema.get("required") or [] if k not in given]
    if not fill or not missing or not (request or heard) or _NO_FILL & set(missing):
        return None
    try:
        filled = fill(tool, request, heard)
    except Exception as exc:  # the offer below still works without it
        log.warning("Catalog argument fill failed for %s: %s", tool["name"], str(exc)[:160])
        return None
    args = {**filled, **given}
    return None if _problems(tool, args) else args


def _keyword_rank(request: str, tools: dict[str, dict]) -> list[dict]:
    words = {w for w in re.findall(r"[a-z]{3,}", request.lower())}
    def score(t):
        hay = (t["name"].replace("_", " ") + " " + t["description"]).lower()
        return sum(w in hay for w in words) + 2 * sum(w in t["name"] for w in words)
    return sorted(tools.values(), key=score, reverse=True)


def resolve(args: dict, heard: str = "", *, myapi_on: bool = False, jev=None, fill=None) -> Resolution:
    """What a use_tool call means: a tool to run now, or what to tell the model.

    `fill(tool, request, heard) -> dict` fills missing arguments from the
    request (fill_args with the Gateway model); a filled call is not `picked`,
    so the switchboard still reviews it."""
    tools = catalog(myapi_on)
    name, given = _normalize(args, tools)
    request = str(args.get("request") or "").strip()
    if name in CORE:
        return Resolution(False, message=f"{name} is one of your own tools: call it directly, not through use_tool.")
    if name in tools:
        tool = tools[name]
        problems = _problems(tool, given)
        if problems and (filled := _complete(tool, given, request, heard, fill)) is not None:
            return Resolution(True, name, filled, evidence={"by": "name", "filled": True})
        if problems:
            return Resolution(False, message=_offer([tool], f"{name}: {'; '.join(problems)}."))
        return Resolution(True, name, given, evidence={"by": "name"})
    if name:
        request = f"{request} ({name})".strip()
    if not request:
        return Resolution(False, message="use_tool needs `request` (what to do) or an exact `name`.")
    started = time.monotonic()
    from ..core.jev import Jev, JevError, choice
    criteria = {n: _summary(t) for n, t in tools.items()}
    criteria[_NONE] = "No tool here does this (it is conversation, or needs a core tool or start_task)."
    try:
        answer = (jev or Jev()).ask(
            {"request": request[:600], "user_said": heard[-300:]},
            {"tool": choice("Which one tool carries out `request` (the user's words are `user_said`, speech "
                            "recognition, possibly garbled)? Pick the tool whose job this is.", criteria)},
            timeout=TIMEOUT, retries=0)["tool"]
    except (JevError, KeyError, ValueError) as exc:
        log.warning("Catalog pick unavailable, offering keyword matches: %s", str(exc)[:160])
        return Resolution(False, message=_offer(_keyword_rank(request, tools)[:CANDIDATES],
                                                "Jev was unavailable; these match best."),
                          evidence={"unavailable": str(exc)[:120]})
    ms = round((time.monotonic() - started) * 1000)
    ranked = sorted(answer["probabilities"].items(), key=lambda kv: kv[1], reverse=True)
    evidence = {"pick": answer["choice"], "p": round(answer["p"], 2), "ms": ms}
    if answer["choice"] == _NONE and answer["p"] >= PICK_P:
        return Resolution(False, message="No catalog tool does this. Use your own tools, start_task for a job "
                                         "with several steps, or just answer.", evidence=evidence)
    top = [tools[n] for n, _ in ranked if n in tools][:CANDIDATES]
    if answer["choice"] == _NONE or answer["p"] < PICK_P:
        return Resolution(False, message=_offer(top, "Jev is not sure which tool fits; the likeliest are below."),
                          evidence=evidence)
    tool = tools[answer["choice"]]
    if not _problems(tool, given):
        return Resolution(True, tool["name"], given, picked=True, evidence=evidence)
    if (filled := _complete(tool, given, request, heard, fill)) is not None:
        return Resolution(True, tool["name"], filled, evidence={**evidence, "filled": True})
    return Resolution(False, message=_offer([tool], f"Jev picked {tool['name']}."), evidence=evidence)
