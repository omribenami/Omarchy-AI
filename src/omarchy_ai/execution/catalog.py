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
    "list_files", "read_file", "terminal_task", "terminal_read", "workspace_switch",
    "browser_task", "desktop_task", "start_task", "task_status", "task_respond",
    "show_window_labels", "run_mission",
})
# In the catalog since 2026-10-11, to keep their declarations out of every
# session's prompt: close_window, hide_window_labels and get_recent_actions
# take no argument or one Jev can pick (the session still answers
# get_recent_actions itself: voice/gemini_live.py _run_call). workspace_switch
# stays declared: without it the model sent "switch to workspace 3" to
# desktop_task, the slow background loop (probes that day, 2 of 2).

# Declared directly when MyApi is connected (and still reachable through
# use_tool). 2026-09-30 13:35: sending one email through use_tool failed four
# ways in a row -- args nested twice, args left out, then refused as a loop --
# and a body of text is never filled from a request, by design (_NO_FILL).
MYAPI_CORE = frozenset({"myapi"})  # Jev runs MyApi (execution/myapi_agent.py)

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
    model to fill args in one call when it already knows the tool.

    Tools the user added also carry their description: the instructions cover
    the built-in ones, not these. Journal 2026-09-29 18:58: seeing only a
    smart-home user tool's bare signature, the model asked it to "list all
    devices to find" the one the user named, twice (each ran as a command and
    switched on the wrong device), and needed four calls for one command."""
    def sig(t):
        schema = t.get("parameters") or {}
        required = set(schema.get("required") or [])
        return f"{t['name']}({', '.join(k + ('*' if k in required else '') for k in schema.get('properties') or {})})"
    try:
        from . import user_tools
        added = {t["name"] for t in user_tools.schemas()}
    except ImportError:
        added = set()
    tools = catalog(myapi_on).values()
    return ", ".join(sig(t) for t in tools if t["name"] not in added) + "".join(
        f"\n- {sig(t)}: {t['description']}" for t in tools if t["name"] in added)


def declared(myapi_on: bool = False) -> list[dict]:
    """The live model's function list when the picker is on."""
    return ([t for t in TOOLS if t["name"] in CORE] + [t for t in MYAPI_TOOLS if myapi_on and t["name"] in MYAPI_CORE]
            + [USE_TOOL])


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
    "description's own examples for the exact values). `request` decides what the call does (its action or "
    "operation); `user_said` may only supply details the request leaves out, never a different action. Use only what "
    "the request, the user's words and the description support; leave out any parameter you would have to guess."
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
    # 2026-09-30 13:35: {"args": {"name": "myapi_gmail_send", "args": {...}}} -- the whole call nested once more.
    if set(given) == {"args"} and isinstance(given["args"], dict):
        given = dict(given["args"])
    props = ((tools.get(name) or {}).get("parameters") or {}).get("properties") or {}
    # A tool with its own `args` parameter (schedule_task: the call to make
    # later) sent flat: use_tool(name=schedule_task, kind=..., tool=..., args={the
    # later call's arguments}). Those `args` belong to the tool, not to use_tool.
    if "args" in props and given and not set(given) & set(props) and any(
            k in props for k in args if k not in ("name", "args", "request")):
        given = {"args": given}
    for key, value in args.items():
        if key not in ("name", "args", "request") and key in props:
            given.setdefault(key, value)
    if name in tools:
        given, moved = conform(tools[name].get("parameters") or {}, given)
        if moved:
            log.info("Catalog call to %s fitted to its schema: %s", name, ", ".join(moved))
    return name, given


# Argument names the live model uses for the same thing. Journal
# 2026-10-26: a smart-home user tool failed "ask needs the request in
# 'text'" three times running; the request was sent as `request`.
_SYNONYMS = (
    frozenset({"text", "request", "query", "q", "prompt", "message", "msg", "question", "input", "utterance",
               "sentence", "phrase", "instruction", "instructions", "command", "say", "words", "search",
               "search_query", "content", "body"}),
    frozenset({"entity_id", "entity_ids", "entity", "entities", "device", "device_id", "device_name", "target",
               "targets"}),
    frozenset({"camera", "camera_name", "cam", "camera_id"}),
    frozenset({"area", "room", "location", "zone", "place"}),
    frozenset({"operation", "op", "action", "mode"}),
    frozenset({"url", "link", "href", "uri", "address", "website"}),
    frozenset({"path", "file", "file_path", "filepath", "filename"}),
    frozenset({"service", "service_name"}),
    frozenset({"service_data", "data", "params", "parameters", "attributes"}),
)


def _type_fits(prop: dict, value) -> bool:
    kinds = {prop.get("type")} | {(o or {}).get("type") for o in (prop.get("oneOf") or prop.get("anyOf") or [])}
    kinds.discard(None)
    if not kinds:
        return True
    by_type = {"string": str, "array": list, "object": dict, "boolean": bool, "integer": int, "number": (int, float)}
    return any(isinstance(value, by_type[k]) for k in kinds if k in by_type)


def _mentions(text: str, value: str) -> bool:
    return bool(re.search(rf"(?<![\w]){re.escape(value.casefold())}(?![\w])", (text or "").casefold()))


def conform(schema: dict, args: dict) -> tuple[dict, list[str]]:
    """`args` fitted to the tool's declared schema where the intent is plain:
    a value under a synonym of a missing parameter (`request` for `text`),
    the one unexpected string where one required string is missing, an enum
    value in the wrong case, and a missing required enum (`operation`) that
    the other arguments' descriptions all point to ("For call" on domain and
    service -> operation "call"; live 2026-10-24 a garage call was sent with
    domain/service/entity_id and no operation). Returns (args, what moved)."""
    props = (schema or {}).get("properties") or {}
    if not props or not isinstance(args, dict):
        return args, []
    args, notes = dict(args), []
    required = [k for k in (schema.get("required") or []) if k in props]

    def missing(key):
        return key not in args or args[key] in (None, "")

    for key in [k for k in args if k not in props]:
        group = next((g for g in _SYNONYMS if key.casefold() in g), None)
        if group is None:
            continue
        targets = [p for p in props if p.casefold() in group and missing(p) and _type_fits(props[p], args[key])]
        if len(targets) > 1:
            targets = [p for p in targets if p in required] or targets
        if len(targets) == 1:
            args[targets[0]] = args.pop(key)
            notes.append(f"{key}->{targets[0]}")
    unknown = [k for k in args if k not in props and isinstance(args[k], str) and args[k].strip()]
    wanted = [k for k in required if missing(k) and (props[k] or {}).get("type") == "string"
              and not (props[k] or {}).get("enum")]
    if len(unknown) == 1 and len(wanted) == 1:
        args[wanted[0]] = args.pop(unknown[0])
        notes.append(f"{unknown[0]}->{wanted[0]}")
    for key, prop in props.items():
        allowed = (prop or {}).get("enum")
        if not allowed:
            continue
        value = args.get(key)
        if isinstance(value, str) and value not in allowed:
            folded = re.sub(r"[\s-]+", "_", value.strip().casefold())
            same = [a for a in allowed if isinstance(a, str) and re.sub(r"[\s-]+", "_", a.casefold()) == folded]
            if not same and folded:
                # A shortened value ("play" for play_music / play_movie),
                # settled by the other arguments: a value naming the rest
                # (media_type "music"), or a description naming the option
                # ("media type for play_music"). Live 2026-10-26: a song
                # request failed on operation "play" and was escalated.
                same = [a for a in allowed if isinstance(a, str)
                        and folded in re.sub(r"[\s-]+", "_", a.casefold()).split("_")]
                if len(same) > 1:
                    said = [str(v).casefold() for k, v in args.items() if k != key and isinstance(v, str)]
                    same = [a for a in same
                            if any(part in said for part in a.casefold().split("_") if part != folded)
                            or any(_mentions((props.get(k) or {}).get("description"), a)
                                   for k in args if k != key and k in props)] or same
            if len(same) == 1:
                args[key] = same[0]
                notes.append(f"{key}={same[0]}")
        elif missing(key) and key in required:
            pointed = None
            for other, value in args.items():
                if other == key or other not in props:
                    continue
                said = {a for a in allowed if isinstance(a, str) and _mentions((props[other] or {}).get("description"), a)}
                if said:
                    pointed = said if pointed is None else pointed & said
            if pointed is not None and len(pointed) == 1:
                args[key] = next(iter(pointed))
                notes.append(f"{key}={args[key]} (from the other arguments)")
    return args, notes


def usage(tool: dict, keys, given: dict | None = None) -> str:
    """How to pass `keys` to this tool: their names, what they hold, and a
    call to copy (with the values already `given`), so a retry gets the
    argument names right."""
    schema = tool.get("parameters") or {}
    props = schema.get("properties") or {}
    given = {k: v for k, v in (given or {}).items() if k in props and v not in (None, "")}
    example: dict = {}
    for key in dict.fromkeys([*(schema.get("required") or []), *given, *keys]):
        prop = props.get(key) or {}
        if key in given:
            example[key] = given[key]
        elif prop.get("enum"):
            example[key] = prop["enum"][0]
        else:
            example[key] = f"<{(prop.get('description') or key).split('.')[0][:60]}>"
    described = "; ".join(f"`{k}`: {(props.get(k) or {}).get('description', '')[:120]}" for k in keys)
    return (f"Use exactly these argument names ({described}). For example: use_tool(name={tool['name']!r}, "
            f"args={json.dumps(example, ensure_ascii=False)}).")


_CONTENT_ARGS = frozenset({"text", "message", "body", "content"})
# A parameter that wants the user's request itself (a smart-home tool's
# `text`: "the user's request, in their words") is filled with use_tool's
# `request` when the filler paraphrased it.
_WANTS_REQUEST = re.compile(r"user'?s (own )?(request|words)|in (their|the user's) (own )?words", re.I)


def _request_into(tool: dict, given: dict, request: str) -> dict:
    """`given` plus use_tool's `request` in the parameter that wants the
    user's request, when it is missing and applies to the chosen operation
    (its description names none of the tool's enum values, or names the one
    given)."""
    props = (tool.get("parameters") or {}).get("properties") or {}
    if not request:
        return given
    for key, prop in props.items():
        if key in given or (prop or {}).get("type") != "string" or not _WANTS_REQUEST.search(
                str((prop or {}).get("description") or "")):
            continue
        description = str(prop.get("description") or "")
        named = {(k, v) for k, p in props.items() for v in ((p or {}).get("enum") or [])
                 if isinstance(v, str) and _mentions(description, v)}
        if not named or any(given.get(k) == v for k, v in named):
            return {**given, key: request}
    return given


def _same_words(part: str, whole: str) -> bool:
    """`part` appears in `whole`, ignoring case, punctuation and spacing."""
    def norm(text):
        return " ".join(re.findall(r"\w+", text.casefold()))
    return bool(norm(part)) and f" {norm(part)} " in f" {norm(whole)} "


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
    # Text to type or send is content: it must be in her request, never made
    # up by the filler. 2026-10-20 01:29: "press Enter" was filled as typing
    # the user's own instruction ("another 3 messages") and sent to ChatGPT.
    props = schema.get("properties") or {}
    for key in _CONTENT_ARGS & set(filled) - set(given):
        if not _same_words(str(filled[key]), request):
            if request and _WANTS_REQUEST.search(str((props.get(key) or {}).get("description") or "")):
                log.info("Catalog fill for %s: %s is the request itself", tool["name"], key)
                filled[key] = request
                continue
            log.info("Catalog fill for %s dropped %s: not in the request", tool["name"], key)
            filled.pop(key)
    _named_choices(props, filled, given, request, tool["name"])
    args = {**filled, **given}
    return None if _problems(tool, args) else args


def _named_choices(props: dict, filled: dict, given: dict, request: str, name: str) -> None:
    """A choice the filler made that the request never names gives way to
    the one the request names first. 2026-10-29 21:30: use_tool(name=
    browser_control, request='click "SUBSCRIBE"') was filled as a scroll
    from the user's earlier "scroll to the middle", three times."""
    words = re.findall(r"\w+", request.casefold())
    # A tool that takes the user's own words ("list cameras in the house" as
    # a smart-home question) reads a verb in them as words, not as a choice.
    if not words or any(_WANTS_REQUEST.search(str((p or {}).get("description") or "")) for p in props.values()):
        return
    for key, prop in props.items():
        options = {v.casefold(): v for v in (prop or {}).get("enum") or [] if isinstance(v, str)}
        if key in given or key not in filled or str(filled[key]).casefold() in words or words[0] not in options:
            continue
        log.info("Catalog fill for %s: %s %r -> %r, as the request says", name, key, filled[key], options[words[0]])
        filled[key] = options[words[0]]


def _keyword_rank(request: str, tools: dict[str, dict], matched_only: bool = False) -> list[dict]:
    """Tools by how many of the request's (English) words they contain;
    `matched_only` leaves out the ones that share none."""
    words = {w for w in re.findall(r"[a-z]{3,}", request.lower())}
    def score(t):
        hay = (t["name"].replace("_", " ") + " " + t["description"]).lower()
        return sum(w in hay for w in words) + 2 * sum(w in t["name"] for w in words)
    ranked = sorted(tools.values(), key=score, reverse=True)
    return [t for t in ranked if score(t) > 0] if matched_only else ranked


def _origin(given: dict | None) -> dict | None:
    """Where the request came from (core/origin.py), None fields dropped;
    read from the conversation and person in context when not given."""
    if given is None:
        try:
            from ..core import origin
            given = origin.current()
        except Exception:  # noqa: BLE001 -- origin only informs the pick
            return None
    info = {k: v for k, v in (given or {}).items() if v is not None}
    return info if info.get("channel") else None


def resolve(args: dict, heard: str = "", *, myapi_on: bool = False, jev=None, fill=None,
            origin: dict | None = None) -> Resolution:
    """What a use_tool call means: a tool to run now, or what to tell the model.

    A call by name runs without a Jev pick here; the session's switchboard
    still reviews it (voice/gemini_live.py _run_call: only `picked` skips
    that), and the review offers Jev the other tools that could fit.
    `origin` (core/origin.py) tells Jev where the request came from.

    `fill(tool, request, heard) -> dict` fills missing arguments from the
    request (fill_args with the Gateway model); a filled call is not `picked`,
    so the switchboard still reviews it."""
    tools = catalog(myapi_on)
    name, given = _normalize(args, tools)
    request = str(args.get("request") or "").strip()
    # MyApi method discovery returns exact operations such as
    # GMAIL_CREATE_EMAIL_DRAFT. Gemini naturally calls that name directly
    # with `parameters`, even though only our stable catalog tools are
    # declared. Preserve the documented method and arguments instead of
    # asking a text model to guess a raw provider REST path.
    parameters = args.get("parameters")
    if (myapi_on and name not in tools and isinstance(parameters, dict)
            and re.fullmatch(r"[A-Z][A-Z0-9_]+", name)
            and "myapi_execute" in tools):
        service = name.split("_", 1)[0].lower()
        return Resolution(True, "myapi_execute", {
            "service": service,
            "operation": name,
            "arguments": parameters,
            "description": request or f"Execute {name} on {service}",
        }, evidence={"by": "myapi_operation", "operation": name})
    if name in CORE:
        return Resolution(False, message=f"{name} is one of your own tools: call it directly, not through use_tool.")
    if name in tools:
        tool = tools[name]
        given = _request_into(tool, given, request)
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
    state = {"request": request[:600], "user_said": heard[-300:]}
    if (where := _origin(origin)) is not None:
        state["origin"] = where
    # 2026-10-15 19:52: "tell the kids they have 2 minutes to leave the
    # computer" (said to the speaker) was picked as set_reminder, a desktop
    # notification on this computer; Jev did not know who was where.
    try:
        answer = (jev or Jev()).ask(
            state,
            {"tool": choice("Which one tool carries out `request` (the user's words are `user_said`, speech "
                            "recognition, possibly garbled)? Pick the tool whose job this is. `origin` says where "
                            "the request came from and whether the user is at this computer: when they are not "
                            "(the voice speaker in a room, the phone), 'here', 'this room', 'the lights' mean the "
                            "place where they are, and a tool that only shows or changes something on this "
                            "computer's screen (night light, a desktop notification or reminder) reaches only "
                            "someone at this computer.", criteria)},
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
    given, _moved = conform(tool.get("parameters") or {}, given)
    given = _request_into(tool, given, request)
    if not _problems(tool, given):
        return Resolution(True, tool["name"], given, picked=True, evidence=evidence)
    if (filled := _complete(tool, given, request, heard, fill)) is not None:
        return Resolution(True, tool["name"], filled, evidence={**evidence, "filled": True})
    return Resolution(False, message=_offer([tool], f"Jev picked {tool['name']}."), evidence=evidence)


def suggest(need: str, *, myapi_on: bool = False, jev=None, count: int = 4) -> list[dict]:
    """The tools, declared or in the catalog, likeliest to do `need`, best
    first ({name, summary, p}). For when the model says it cannot do
    something without having tried (voice/gemini_live.py _judge_claim):
    2026-10-20 01:06 she told the user she could not read the open ChatGPT
    conversation, never having looked at inspect_browser in the catalog."""
    tools = {t["name"]: t for t in TOOLS if t["name"] in CORE} | catalog(myapi_on)
    from ..core.jev import Jev, choice
    answer = (jev or Jev()).ask(
        {"need": need[:600]},
        {"tool": choice("Which tool does `need`, or the first step toward it?",
                        {n: _summary(t) for n, t in tools.items()})},
        timeout=TIMEOUT, retries=0)["tool"]
    ranked = sorted(answer["probabilities"].items(), key=lambda kv: kv[1], reverse=True)
    return [{"name": n, "summary": _summary(tools[n]), "p": round(p, 2)} for n, p in ranked if n in tools][:count]

