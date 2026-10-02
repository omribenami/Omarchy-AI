"""Every talk with Omarchy, kept so the phone can list it, reopen it and go on.

The user, 2026-09-30: "keep talks in a sub menu of the text screen, like the
AI chats of ChatGPT/Claude/Grok ... approvals pop inside the relevant chat ...
the voice talks should also appear there ... the chat name set by the AI."

A conversation is one file under STATE_DIR/conversations: its lines (what the
user said or typed, what she answered) and cards for the tasks started in it
(an approval, a question, a result). Calls write into it as they happen: the
session's transcript list is a Transcript, whose appends land here. A call
that continues a conversation gives the model its lines first (context()).
The title comes from the Gateway model once there is something to name, and
again as the talk grows.

A task knows its conversation (Task.conversation, from CURRENT while a call's
tool runs), so its approvals, questions and results come back to the chat it
started in, and so do the phone notifications about it (for_notification).
"""
from __future__ import annotations

from collections import OrderedDict
import contextvars
import json
import logging
import os
import re
from pathlib import Path
import secrets
import shutil
import threading
import time

from ..config import STATE_DIR

log = logging.getLogger(__name__)

DIR = STATE_DIR / "conversations"
ATTACHMENTS_DIR = STATE_DIR / "conversation_attachments"
MAX_LINES = 2000
MAX_LIST = 100
CONTEXT_CHARS = 8000
DESKTOP_GAP = 15 * 60      # a desktop voice session this soon after the last one continues it
TITLE_AFTER = (2, 12, 40)  # (re)name at these line counts
_SAVE_EVERY = 1.0
MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_ATTACHMENTS_PER_TURN = 6

# The conversation of the call whose tool is running (asyncio.to_thread copies it).
CURRENT: contextvars.ContextVar[str] = contextvars.ContextVar("omarchy_conversation", default="")

_lock = threading.RLock()
_cache: dict[str, dict] = {}
_dirty: dict[str, float] = {}
_notified: OrderedDict[str, str] = OrderedDict()  # notification title -> conversation id


def _path(cid: str) -> Path:
    return DIR / f"{cid}.json"


def _valid(cid) -> bool:
    return isinstance(cid, str) and 8 <= len(cid) <= 40 and cid.replace("-", "").isalnum()


def _load(cid: str) -> dict | None:
    if not _valid(cid):
        return None
    with _lock:
        if cid in _cache:
            return _cache[cid]
        try:
            data = json.loads(_path(cid).read_text())
        except (OSError, ValueError):
            return None
        _cache[cid] = data
        return data


def _save(conv: dict, force: bool = False) -> None:
    with _lock:
        now = time.time()
        if not force and now - _dirty.get(conv["id"], 0) < _SAVE_EVERY:
            conv["_unsaved"] = True  # written by the next save or flush()
            return
        conv.pop("_unsaved", None)
        _dirty[conv["id"]] = now
        DIR.mkdir(parents=True, exist_ok=True)
        tmp = _path(conv["id"]).with_suffix(".tmp")
        tmp.write_text(json.dumps(conv, ensure_ascii=False))
        os.replace(tmp, _path(conv["id"]))


def flush(cid: str | None = None) -> None:
    with _lock:
        for conv in list(_cache.values()):
            if (cid is None or conv["id"] == cid) and conv.get("_unsaved"):
                _save(conv, force=True)


def create(source: str) -> str:
    cid = time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)
    now = time.time()
    conv = {"id": cid, "title": "", "source": source, "created": now, "updated": now, "lines": [], "titled_at": 0}
    with _lock:
        _cache[cid] = conv
        _save(conv, force=True)
    return cid


def exists(cid) -> bool:
    return _load(cid) is not None


def continue_or_create(cid, source: str) -> str:
    """The call's conversation: the one the phone named, or a new one."""
    return cid if exists(cid) else create(source)


def desktop(source: str = "desktop-voice") -> str:
    """Desktop voice sessions come one wake word at a time: one soon after the
    last goes on in its conversation instead of starting a new chat. The same
    for a voice satellite (source "satellite")."""
    latest = next((c for c in _summaries(empty=True) if c["source"] == source), None)
    if latest and time.time() - latest["updated"] < DESKTOP_GAP:
        return latest["id"]
    return create(source)


def add_line(cid: str, role: str, text: str, attachments: list[dict] | None = None) -> None:
    """A piece of what was said. Pieces of one side's turn join one line."""
    text = str(text or "")
    attachments = [dict(item) for item in (attachments or []) if isinstance(item, dict)]
    if not text and not attachments:
        return
    role = "user" if role == "user" else "assistant"
    with _lock:
        conv = _load(cid)
        if conv is None:
            return
        lines = conv["lines"]
        now = time.time()
        if lines and lines[-1].get("role") == role and not attachments:
            lines[-1]["text"] += text
        else:
            line = {"role": role, "text": text, "at": now}
            if attachments:
                line["attachments"] = attachments[:MAX_ATTACHMENTS_PER_TURN]
            lines.append(line)
            del lines[:-MAX_LINES]
        # Passwords never land here, as in the session history: the whole
        # line, since a spoken one comes split across fragments.
        lines[-1]["text"] = _redact(lines[-1]["text"], role == "user")
        conv["updated"] = now
        _save(conv)
    if role == "assistant":
        _maybe_title(cid)


def _redact(text: str, speech: bool) -> str:
    try:
        from ..execution.passwords import redact
        return redact(text, speech=speech)
    except Exception:  # noqa: BLE001 -- keyring trouble must not lose the line
        return text


def add_card(cid: str, card: dict) -> None:
    with _lock:
        conv = _load(cid)
        if conv is None:
            return
        conv["lines"].append({"role": "card", "at": time.time(), **card})
        del conv["lines"][:-MAX_LINES]
        conv["updated"] = time.time()
        _save(conv, force=True)


def _safe_name(name: str) -> str:
    name = Path(str(name or "file").replace("\0", "")).name.strip()
    return name[:180] or "file"


def store_attachment(cid: str, name: str, mime: str, body: bytes, *, role: str | None = None,
                     text: str = "") -> dict:
    """Keep one chat attachment under its conversation and return public metadata."""
    if not exists(cid):
        raise KeyError("no such conversation")
    if not body:
        raise ValueError("file is empty")
    if len(body) > MAX_ATTACHMENT_BYTES:
        raise ValueError("file is larger than 20 MB")
    clean = _safe_name(name)
    aid = secrets.token_hex(12)
    folder = ATTACHMENTS_DIR / cid
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = folder / f"{aid}-{clean}"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(body)
    mime = str(mime or "application/octet-stream")[:120]
    if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", mime):
        mime = "application/octet-stream"
    meta = {"id": aid, "name": clean, "mime": mime,
            "size": len(body), "url": f"/api/conversations/{cid}/attachments/{aid}", "path": str(target)}
    with _lock:
        conv = _load(cid)
        if conv is None:
            target.unlink(missing_ok=True)
            raise KeyError("no such conversation")
        conv.setdefault("attachments", []).append(meta)
        _save(conv, force=True)
    if role:
        add_line(cid, role, text, [meta])
    return meta


def share_file(cid: str, path: str, label: str = "") -> dict:
    """Copy a file explicitly selected by the assistant into a phone chat."""
    from ..config import load_config
    from ..execution.files import resolve_path
    source = resolve_path(path, load_config(), must_exist=True)
    if not source.is_file():
        raise ValueError("file does not exist")
    if source.stat().st_size > MAX_ATTACHMENT_BYTES:
        raise ValueError("file is larger than 20 MB")
    import mimetypes
    meta = store_attachment(cid, source.name, mimetypes.guess_type(source.name)[0] or "application/octet-stream",
                            source.read_bytes(), role="assistant", text=label.strip() or source.name)
    return meta


def attachment(cid: str, aid: str) -> tuple[Path, dict] | None:
    conv = _load(cid)
    if conv is None or not isinstance(aid, str):
        return None
    known = list(conv.get("attachments", []))
    known.extend(meta for line in conv.get("lines", []) for meta in line.get("attachments", []))
    for meta in known:
        if meta.get("id") == aid:
            path = ATTACHMENTS_DIR / cid / f"{aid}-{_safe_name(meta.get('name', 'file'))}"
            return (path, dict(meta)) if path.is_file() else None
    return None


def delete(cid) -> bool:
    with _lock:
        if _load(cid) is None:
            return False
        _cache.pop(cid, None)
        try:
            _path(cid).unlink()
        except OSError:
            return False
        shutil.rmtree(ATTACHMENTS_DIR / cid, ignore_errors=True)
        return True


def context(cid: str) -> str:
    """The conversation so far, for the model at the start of a call that continues it."""
    conv = _load(cid)
    if not conv:
        return ""
    out = []
    for line in conv["lines"]:
        attached = ", ".join(
            f"{item.get('name')} ({item.get('mime')}; desktop path {item.get('path')})"
            for item in line.get("attachments", []) if isinstance(item, dict)
        )
        if line.get("role") == "card":
            out.append(f"[Task {line.get('task_id')} {line.get('kind')}] {line.get('text', '')}")
        else:
            text = line["text"].strip()
            if attached:
                text += f" [Attachments: {attached}]"
            out.append(f"{'User' if line['role'] == 'user' else 'Omarchy'}: {text}")
    text = "\n".join(out)
    return text[-CONTEXT_CHARS:]


class Transcript(list):
    """A session's transcript that also writes into its conversation."""

    def __init__(self, cid: str, items=()):
        super().__init__(items)
        self.conversation = cid

    def append(self, item) -> None:
        super().append(item)
        if isinstance(item, dict):
            add_line(self.conversation, item.get("role", ""), item.get("text", ""), item.get("attachments"))

    def close(self) -> None:
        flush(self.conversation)


# -- tasks -----------------------------------------------------------------

_CARD_EVENTS = {"waiting_approval": "approval", "waiting_user": "question",
                "certified": "result", "unverified": "result", "failed": "result", "progress": "progress"}


def task_progress(task) -> str:
    """One compact, shared status sentence for chat cards and the task HUD."""
    step = (getattr(task, "steps", None) or [])[-1] if getattr(task, "steps", None) else None
    if step and step.get("outcome") == "running":
        return f"Still working: {step.get('executor', 'Omarchy')} is {step.get('role', 'working')} (step {step.get('n')})."
    if step and step.get("claim"):
        claim = " ".join(str(step["claim"]).split())
        return f"Progress: {claim[:360]}"
    return f"Still working: {str(getattr(task, 'phase', 'working')).replace('_', ' ')}."


def task_event(task, event: str) -> None:
    """A task started in a conversation reports back into it as a card."""
    cid = getattr(task, "conversation", "")
    kind = _CARD_EVENTS.get(event)
    if not cid or not kind:
        return
    card = {"kind": kind, "task_id": task.id, "event": event}
    if kind == "approval":
        request = task.pending_approval or {}
        sudo = request.get("sudo_action")
        text = (f"Sudo needed for: {request.get('task_summary', '')}\nAction: {sudo}" if sudo else
                f"Needs your approval: {request.get('subject', '')}")
        card.update(text=text, subject=str(request.get("subject", "")),
                    risk=request.get("risk", ""), reasons=list(request.get("reasons", []))[:5],
                    fingerprint=request.get("fingerprint", ""), task_summary=request.get("task_summary", ""),
                    sudo_action=sudo or "")
    elif kind == "question":
        card.update(text=str(task.question or ""), question=str(task.question or ""))
    elif kind == "progress":
        card["text"] = task_progress(task)
    else:
        card["text"] = str(task.result or event)
    add_card(cid, card)


TOOL_INSTALL = "tool-install:"


def system() -> str:
    """The conversation for requests no talk started (a tool install run by hand)."""
    latest = next((c for c in _summaries(empty=True) if c["source"] == "system"), None)
    if latest:
        return latest["id"]
    cid = create("system")
    with _lock:
        _cache[cid]["title"] = "System requests"
        _save(_cache[cid], force=True)
    return cid


_carded: set[str] = set()


def sync_installs() -> None:
    """Each tool install waiting for the user becomes an approval card in the
    System requests chat, once, with one phone notification that opens it.
    2026-09-30: the only way to approve one was the desktop notification, and
    the phone's notice promised a fingerprint prompt that never came."""
    from ..execution import user_tools
    for request in user_tools.pending_install_requests():
        if request["id"] in _carded:
            continue
        _carded.add(request["id"])
        cid = system()
        add_card(cid, {"kind": "approval", "task_id": TOOL_INSTALL + request["id"], "event": "waiting_approval",
                       "text": f"Install the assistant tool {request['name']}?",
                       "subject": f"install tool {request['name']}", "risk": "ELEVATED",
                       "reasons": [request["summary"][:300]], "fingerprint": request["id"]})
        try:
            from ..execution import flux_approve
            from ..phone import flux_notify
            how = ("Open the chat and tap Approve for your fingerprint prompt, or use the desktop notification."
                   if flux_approve.available() else "Approve it on the desktop notification.")
            flux_notify.send(f"Install assistant tool: {request['name']}?", how, kind="approval", conversation=cid)
        except Exception:  # noqa: BLE001 -- the card is there either way
            log.debug("tool install notice not sent", exc_info=True)


ACTION = "action:"


def _card_status(card: dict, tasks) -> str:
    if card.get("kind") == "confirm":
        from . import pending_actions
        action = pending_actions.get((card.get("action") or {}).get("id"))
        return "waiting" if action and action.get("status") == "waiting" else "done"
    if str(card.get("task_id", "")).startswith(ACTION):
        return "done"
    if str(card.get("task_id", "")).startswith(TOOL_INSTALL):
        from ..execution import user_tools
        waiting = {r["id"] for r in user_tools.pending_install_requests()}
        return "waiting" if card.get("fingerprint") in waiting else "done"
    task = tasks(card.get("task_id"))
    if task is None or card.get("kind") == "result":
        return "done"
    if card["kind"] == "approval":
        waiting = task.status == "waiting_approval" and (task.pending_approval or {}).get(
            "fingerprint") == card.get("fingerprint")
    else:
        waiting = task.status == "waiting_user" and str(task.question or "") == card.get("question")
    return "waiting" if waiting else "done"


def _with_status(conv: dict, tasks) -> list[dict]:
    lines = [dict(line) for line in conv["lines"]]
    latest = {}
    for i, line in enumerate(lines):
        if line.get("role") == "card":
            latest[line.get("task_id")] = i
    for i, line in enumerate(lines):
        if line.get("role") == "card":
            line["status"] = _card_status(line, tasks) if latest.get(line.get("task_id")) == i else "done"
            if line.get("kind") == "confirm":
                from . import pending_actions
                line["outcome"] = (pending_actions.get((line.get("action") or {}).get("id")) or {}).get("outcome", "")
    return lines


def _task_lookup():
    from ..runtime.service import get_runtime
    store = get_runtime().store
    seen: dict = {}

    def get(task_id):
        if task_id not in seen:
            seen[task_id] = store.load(task_id) if task_id else None
        return seen[task_id]
    return get


# -- reading ----------------------------------------------------------------

def _summaries(empty: bool = False) -> list[dict]:
    """Newest first. A talk where nothing was said is left out, and removed
    once a day old (a wake word with no words, a call that never connected)."""
    flush()
    out = []
    if DIR.is_dir():
        for path in DIR.glob("*.json"):
            conv = _load(path.stem)
            if not conv:
                continue
            if conv["lines"] or empty:
                out.append(conv)
            elif time.time() - conv["updated"] > 86400:
                delete(conv["id"])
    out.sort(key=lambda c: c["updated"], reverse=True)
    return out[:MAX_LIST]


def summaries(tasks=None) -> dict:
    sync_installs()
    tasks = tasks or _task_lookup()
    rows, total = [], 0
    for conv in _summaries():
        lines = _with_status(conv, tasks)
        waiting = sum(1 for line in lines if line.get("status") == "waiting")
        total += waiting
        last = next((line for line in reversed(lines) if line.get("text")), {})
        rows.append({"id": conv["id"], "title": conv.get("title", ""), "source": conv["source"],
                     "created": conv["created"], "updated": conv["updated"],
                     "preview": " ".join(str(last.get("text", "")).split())[:140], "waiting": waiting})
    return {"conversations": rows, "waiting": total}


def get(cid, tasks=None) -> dict | None:
    sync_installs()
    flush(cid if isinstance(cid, str) else None)
    conv = _load(cid)
    if conv is None:
        return None
    return {"id": conv["id"], "title": conv.get("title", ""), "source": conv["source"],
            "created": conv["created"], "updated": conv["updated"],
            "lines": _with_status(conv, tasks or _task_lookup())}


# -- notifications ------------------------------------------------------------

def notified(title: str, cid: str) -> None:
    if not cid or not title:
        return
    with _lock:
        _notified[title] = cid
        _notified.move_to_end(title)
        while len(_notified) > 200:
            _notified.popitem(last=False)


def for_notification(title: str) -> str | None:
    with _lock:
        return _notified.get(title)


# -- titles -------------------------------------------------------------------

_TITLE_SYSTEM = (
    "Name this conversation between a user and their desktop assistant, the way chat apps title chats: "
    "3 to 6 words, the specific subject (names, things), no quotes, no trailing period, in the language the "
    "user mostly speaks. Answer as JSON: {\"title\": \"...\"}.")


def _maybe_title(cid: str) -> None:
    conv = _load(cid)
    if not conv:
        return
    count = sum(1 for line in conv["lines"] if line.get("role") in ("user", "assistant"))
    due = next((n for n in TITLE_AFTER if count >= n > conv.get("titled_at", 0)), None)
    if due is None:
        return
    conv["titled_at"] = max(n for n in TITLE_AFTER if count >= n)
    threading.Thread(target=_title, args=(cid,), daemon=True, name="conversation-title").start()


def _title(cid: str) -> None:
    conv = _load(cid)
    if not conv:
        return
    try:
        from ..config import load_config
        from ..voice.omarchy import GatewayClient
        answer = GatewayClient(load_config()).complete_json(_TITLE_SYSTEM, {"conversation": context(cid)[-4000:]},
                                                           timeout=15)
        title = " ".join(str(answer.get("title") or "").split()).strip(" .\"'")[:60]
    except Exception as exc:  # noqa: BLE001 -- a chat without a title still works
        log.info("conversation title unavailable: %s", str(exc)[:160])
        return
    if title:
        with _lock:
            conv["title"] = title
            _save(conv, force=True)
