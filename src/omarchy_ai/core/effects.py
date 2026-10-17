"""The side-effect journal: every external or irreversible change goes through here.

An email sent, a MyApi write, a file written, a scheduled action's step, a
Task Runtime command at ELEVATED or above: each is recorded *before* it is
dispatched and updated after, so a crash can never leave us not knowing
whether it may have happened, and a retry of the same logical action never
does it twice.

    result = effects.perform("myapi_execute", "GMAIL_SEND_EMAIL", arguments,
                             lambda key: send(arguments, idempotency_key=key),
                             target="gmail:GMAIL_SEND_EMAIL")

The idempotency key is sha256(origin_id, tool, canonical args). origin_id
names what caused the action -- the user turn (execution/verified_input.py's
InputGuard), the pending action (execution/myapi_agent.decide), the agenda
run and step (core/agenda.py), the task, step and occurrence of a command
(runtime/runtime.py) -- and is set with `with effects.origin(...)` or passed
in. With no origin there is nothing to be a retry of: the call gets a fresh
one and is journaled but never deduplicated.

What a second call with the same key gets:
- `confirmed`: the stored result, without dispatching again.
- `failed` (the provider answered with an error, or it failed locally): it
  runs again, as a retry did before.
- `dispatched` whose process is gone (it died mid-call), or `uncertain` (no
  answer came back): it is NOT dispatched again. The result says, in a
  structured way, that it may already have happened.
- `proposed` whose process is gone (it died before dispatching): it runs.
- in flight in a live process: it waits for that result (up to `wait`).

A call cancelled by its deadline or by the user (core/cancel.py, set by
execution/runner.py) never ends `confirmed`: what it dispatched becomes
`uncertain` (the caller was told nothing was confirmed), and what it had not
yet dispatched is not sent and ends `failed`.

States: proposed -> dispatched -> confirmed | failed | uncertain.

Storage, under STATE_DIR (core/statefile.py: flock + atomic writes):
- effects.jsonl -- append-only, one line per state change (no arguments,
  only their hash: email bodies and commands' secrets stay out of it).
- effects_index.json -- the latest state of each key, with its result.
Both keep 30 days.

Crashpoints (core/crashpoints.py): effects.before_dispatch,
effects.after_dispatch_before_record, effects.after_record.
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
import functools
from dataclasses import dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
import time
from typing import Any, Callable, Iterator, TypeVar
import uuid

from .. import config
from . import cancel, statefile
from .crashpoints import crashpoint

log = logging.getLogger("omarchy_ai.core.effects")

JOURNAL_NAME = "effects.jsonl"
INDEX_NAME = "effects_index.json"
INDEX_VERSION = 1
RETENTION_SECONDS = 30 * 24 * 3600
PRUNE_EVERY_SECONDS = 3600
WAIT_SECONDS = 120.0
RESULT_CHARS = 8000

PROPOSED, DISPATCHED, CONFIRMED, FAILED, UNCERTAIN = (
    "proposed",
    "dispatched",
    "confirmed",
    "failed",
    "uncertain",
)
FINAL = (CONFIRMED, FAILED, UNCERTAIN)
IN_PROGRESS = (
    "in_progress"  # not a stored state: what a caller is told while another runs it
)

ORIGIN: contextvars.ContextVar[str] = contextvars.ContextVar(
    "omarchy_effect_origin", default=""
)

T = TypeVar("T")

_lock = threading.Lock()
_inflight: dict[str, threading.Event] = {}  # keys this process is dispatching now
_last_prune = 0.0


class Uncertain(Exception):
    """Raised by a dispatch that cannot tell whether it happened (the request
    went out and no answer came back). `result` is what the caller returns."""

    def __init__(self, result: Any) -> None:
        super().__init__("outcome unknown")
        self.result = result


@dataclass
class Effect:
    """What a caller is told when a call is not dispatched (a replay, or one
    that may already have happened)."""

    effect_id: str
    idem_key: str
    kind: str
    tool: str
    target: str
    state: str
    ts: float
    result: dict | None = None


# ------------------------------------------------------------------ keys


def canonical(args: Any) -> str:
    return json.dumps(
        args, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def args_hash(args: Any) -> str:
    return hashlib.sha256(canonical(args).encode()).hexdigest()


def idem_key(origin_id: str, tool: str, args: Any) -> str:
    material = "\0".join((origin_id, tool, canonical(args)))
    return hashlib.sha256(material.encode()).hexdigest()


@contextmanager
def origin(origin_id: str) -> Iterator[None]:
    """What the effects inside this block are caused by (this thread/task)."""
    token = ORIGIN.set(origin_id)
    try:
        yield
    finally:
        ORIGIN.reset(token)


# --------------------------------------------------------------- storage


def journal_path() -> Path:
    return config.STATE_DIR / JOURNAL_NAME


def index_path() -> Path:
    return config.STATE_DIR / INDEX_NAME


def _me() -> dict:
    from ..runtime.task import this_process

    return this_process()


def _owner_alive(entry: dict, key: str) -> bool:
    owner = entry.get("owner") or {}
    if owner.get("pid") == os.getpid():
        with _lock:
            return key in _inflight  # this process: only while a thread runs it
    from ..runtime.task import owner_alive

    return owner_alive(owner)


def _journal(entry: dict, key: str, note: str = "") -> None:
    record = {
        "effect_id": entry["effect_id"],
        "idem_key": key,
        "kind": entry["kind"],
        "tool": entry["tool"],
        "target": entry["target"],
        "args_hash": entry["args_hash"],
        "state": entry["state"],
        "correlation_id": entry.get("correlation_id") or "",
        "origin": entry.get("origin") or "",
        "attempt": entry.get("attempt", 1),
        "ts": time.time(),
    }
    result = entry.get("result")
    if entry["state"] in FINAL and isinstance(result, dict):
        record["result"] = canonical(result)[:600]
    if note:
        record["note"] = note
    try:
        statefile.append_jsonl(journal_path(), json.dumps(record, ensure_ascii=False))
    except OSError:
        log.warning("effects: could not append to the journal", exc_info=True)


def _index():
    return statefile.locked_json(index_path(), {}, expect=dict, version=INDEX_VERSION)


def _entries(data: dict) -> dict:
    entries = data.get("effects")
    if not isinstance(entries, dict):
        entries = data["effects"] = {}
    return entries


def _view(key: str, entry: dict) -> Effect:
    return Effect(
        entry["effect_id"],
        key,
        entry["kind"],
        entry["tool"],
        entry["target"],
        entry["state"],
        float(entry.get("ts") or 0),
        entry.get("result"),
    )


def lookup(key: str) -> Effect | None:
    """The stored state of one key (read-only)."""
    data = statefile.read_json(index_path(), {}, expect=dict, version=INDEX_VERSION)
    entry = _entries(data).get(key)
    return _view(key, entry) if isinstance(entry, dict) else None


def records(effect_id: str | None = None) -> list[dict]:
    """The journal (one effect's lines when given), oldest first."""
    rows = statefile.read_jsonl(journal_path())
    return [
        r
        for r in rows
        if isinstance(r, dict)
        and (effect_id is None or r.get("effect_id") == effect_id)
    ]


def prune(now: float | None = None) -> None:
    """Drop index entries and journal lines older than 30 days."""
    now = time.time() if now is None else now
    cutoff = now - RETENTION_SECONDS
    with _index() as doc:
        entries = _entries(doc.data)
        for key in [
            k for k, e in entries.items() if float(e.get("updated") or 0) < cutoff
        ]:
            if entries[key].get("state") in FINAL or not _owner_alive(
                entries[key], key
            ):
                del entries[key]
    path = journal_path()
    if not path.exists():
        return
    with statefile.locked(path):
        bad: list[str] = []
        rows = statefile.read_jsonl(path, bad)
        kept = [
            r for r in rows if isinstance(r, dict) and float(r.get("ts") or 0) >= cutoff
        ]
        if len(kept) != len(rows) or bad:
            text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in kept)
            statefile.rewrite_jsonl(path, text, bad)


def _maybe_prune() -> None:
    global _last_prune
    now = time.time()
    if now - _last_prune < PRUNE_EVERY_SECONDS:
        return
    _last_prune = now
    try:
        prune(now)
    except (OSError, statefile.StateFileError):
        log.warning("effects: pruning failed", exc_info=True)


# --------------------------------------------------------------- results


def _store_action(result: Any) -> dict:
    return {"ok": bool(result.ok), "message": str(result.message)[:RESULT_CHARS]}


def _load_action(stored: dict) -> Any:
    from ..execution.actions.base import ActionResult

    return ActionResult(bool(stored.get("ok")), str(stored.get("message") or ""))


def describe(effect: Effect, state: str | None = None) -> dict:
    """The structured account of an effect that was not dispatched (again)."""
    state = state or effect.state
    when = time.strftime("%H:%M on %Y-%m-%d", time.localtime(effect.ts))
    if state == IN_PROGRESS:
        message = (
            f"NOT started again: this exact action ({effect.tool}) is already running since {when}. "
            "Wait for its result; do not repeat it."
        )
    else:
        message = (
            f"NOT done again: this exact action ({effect.tool}) was already started at {when}, but no "
            "confirmation came back, so it MAY ALREADY HAVE HAPPENED. Do not retry it. Tell the user it may "
            "have happened and let them check before they ask for it again."
        )
    return {
        "status": state,
        "effect_id": effect.effect_id,
        "action": effect.tool,
        "target": effect.target,
        "started_at": effect.ts,
        "message": message,
    }


def not_dispatched(result: Any) -> bool:
    """Whether an ActionResult is this module's account of a call it did not
    dispatch because it may already have happened or is running elsewhere."""
    try:
        payload = json.loads(getattr(result, "message", "") or "")
    except (TypeError, ValueError):
        return False
    return (
        isinstance(payload, dict)
        and payload.get("status") in (UNCERTAIN, IN_PROGRESS)
        and "effect_id" in payload
    )


def _unsure_action(effect: Effect, state: str) -> Any:
    from ..execution.actions.base import ActionResult

    return ActionResult(False, json.dumps(describe(effect, state), ensure_ascii=False))


# --------------------------------------------------------------- perform


def _claim(
    key: str,
    kind: str,
    tool: str,
    target: str,
    args: Any,
    origin_id: str,
    correlation_id: str,
) -> tuple[str, Effect]:
    """("go", effect) when this call should dispatch (now proposed and owned
    here); otherwise ("replay" | "uncertain" | "wait", effect)."""
    now = time.time()
    with _index() as doc:
        entries = _entries(doc.data)
        entry = entries.get(key)
        if isinstance(entry, dict):
            state = entry.get("state")
            if state == CONFIRMED:
                return "replay", _view(key, entry)
            if state == UNCERTAIN:
                return "uncertain", _view(key, entry)
            if state in (PROPOSED, DISPATCHED):
                if _owner_alive(entry, key):
                    return "wait", _view(key, entry)
                if state == DISPATCHED:
                    entry.update(state=UNCERTAIN, updated=now)
                    _journal(entry, key, "its process ended after dispatching it")
                    return "uncertain", _view(key, entry)
                # proposed and its process is gone: it never went out.
        attempt = int(entry.get("attempt", 0)) + 1 if isinstance(entry, dict) else 1
        entry = {
            "effect_id": entry["effect_id"]
            if isinstance(entry, dict)
            else uuid.uuid4().hex,
            "kind": kind,
            "tool": tool,
            "target": target,
            "args_hash": args_hash(args),
            "origin": origin_id,
            "correlation_id": correlation_id,
            "state": PROPOSED,
            "ts": now,
            "updated": now,
            "owner": _me(),
            "attempt": attempt,
            "result": None,
        }
        entries[key] = entry
        with _lock:
            _inflight[key] = threading.Event()
        _journal(entry, key)
        return "go", _view(key, entry)


def _set(key: str, state: str, result: dict | None = None, note: str = "") -> None:
    with _index() as doc:
        entry = _entries(doc.data).get(key)
        if not isinstance(entry, dict):
            return
        entry.update(state=state, updated=time.time())
        if result is not None:
            entry["result"] = result
        _journal(entry, key, note)


def _dispatching(key: str, token: cancel.CancelToken | None) -> bool:
    """proposed -> dispatched, unless the call was cancelled first (then
    failed: it never went out). Under the index lock, so a cancel is seen
    either before this or as `dispatched` by _abandon."""
    with _index() as doc:
        entry = _entries(doc.data).get(key)
        if not isinstance(entry, dict):
            return token is None or not token.cancelled
        if token is not None and token.cancelled:
            entry.update(state=FAILED, updated=time.time())
            _journal(entry, key, f"stopped before it was sent ({token.reason})")
            return False
        entry.update(state=DISPATCHED, updated=time.time())
        _journal(entry, key)
        return True


def _finish(
    key: str, state: str, result: dict | None, token: cancel.CancelToken | None
) -> None:
    """The final state, or uncertain when the call was cancelled meanwhile."""
    with _index() as doc:
        entry = _entries(doc.data).get(key)
        if not isinstance(entry, dict):
            return
        note = ""
        if token is not None and token.cancelled:
            state, note = UNCERTAIN, f"stopped while it ran ({token.reason})"
        entry.update(state=state, updated=time.time())
        if result is not None:
            entry["result"] = result
        _journal(entry, key, note)


def _abandon(key: str, token: cancel.CancelToken) -> None:
    """The call this effect belongs to was cancelled (deadline, stop): the
    caller was told nothing was confirmed, so neither may the journal be."""
    with _index() as doc:
        entry = _entries(doc.data).get(key)
        if isinstance(entry, dict) and entry.get("state") in (DISPATCHED, CONFIRMED):
            entry.update(state=UNCERTAIN, updated=time.time())
            _journal(entry, key, f"its call was stopped ({token.reason})")


def _done(key: str) -> None:
    with _lock:
        event = _inflight.pop(key, None)
    if event is not None:
        event.set()


def _wait(key: str, effect: Effect, until: float) -> None:
    with _lock:
        event = _inflight.get(key)
    if event is not None:
        event.wait(max(0.0, until - time.monotonic()))
        return
    time.sleep(
        min(0.2, max(0.0, until - time.monotonic()))
    )  # another process: poll the index


def perform(
    kind: str,
    tool: str,
    args: Any,
    dispatch: Callable[[str], T],
    *,
    target: str = "",
    origin_id: str | None = None,
    correlation_id: str | None = None,
    succeeded: Callable[[T], bool] | None = None,
    store: Callable[[T], dict] | None = None,
    load: Callable[[dict], T] | None = None,
    unsure: Callable[[Effect, str], T] | None = None,
    wait: float = WAIT_SECONDS,
) -> T:
    """Run `dispatch(idem_key)` at most once per (origin, tool, args).

    By default the result is an ActionResult and `ok` decides confirmed or
    failed. `store`/`load` turn other results into JSON and back, `unsure`
    builds the result for one that may already have happened (or is still
    running elsewhere)."""
    store = store or _store_action
    load = load or _load_action
    unsure = unsure or _unsure_action
    is_ok: Callable[[T], bool] = succeeded or (lambda r: bool(getattr(r, "ok", False)))
    origin_id = origin_id or ORIGIN.get() or f"once:{uuid.uuid4().hex}"
    if correlation_id is None:
        from . import conversations

        correlation_id = conversations.CURRENT.get()
    key = idem_key(origin_id, tool, args)
    target = target or tool
    _maybe_prune()
    deadline = time.monotonic() + wait
    while True:
        verdict, effect = _claim(
            key, kind, tool, target, args, origin_id, correlation_id
        )
        if verdict == "go":
            break
        if verdict == "replay":
            log.info(
                "effects: %s %s already done (%s); returning its result",
                kind,
                tool,
                effect.effect_id,
            )
            return load(effect.result or {})
        if verdict == "uncertain":
            log.warning(
                "effects: %s %s may already have happened (%s); not sent again",
                kind,
                tool,
                effect.effect_id,
            )
            return unsure(effect, UNCERTAIN)
        if time.monotonic() >= deadline:
            return unsure(effect, IN_PROGRESS)
        _wait(key, effect, deadline)
    token = cancel.current()
    if token is not None:
        token.effect_ids.append(effect.effect_id)
        token.on_cancel(functools.partial(_abandon, key, token))
    try:
        crashpoint("effects.before_dispatch")
        if not _dispatching(key, token):
            raise cancel.Cancelled(token.reason if token is not None else "cancelled")
        try:
            result = dispatch(key)
        except Uncertain as exc:
            crashpoint("effects.after_dispatch_before_record")
            _set(key, UNCERTAIN, store(exc.result), "no answer came back")
            crashpoint("effects.after_record")
            return exc.result
        except Exception as exc:
            _set(key, UNCERTAIN, None, f"{type(exc).__name__}: {str(exc)[:200]}")
            raise
        crashpoint("effects.after_dispatch_before_record")
        _finish(key, CONFIRMED if is_ok(result) else FAILED, store(result), token)
        crashpoint("effects.after_record")
        return result
    finally:
        _done(key)
