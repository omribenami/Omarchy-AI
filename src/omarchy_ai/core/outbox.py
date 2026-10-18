"""The one way anything reaches the user (harness item 4.1).

Every user-visible message goes through here: a result, a question, an
approval notice, an alert, an acknowledgement, a progress note. A message is
said or shown on the surface the user is on now, once:

- a conversation is open (desktop voice, a phone call, the text chat, the
  speaker): it is said there, and nowhere else;
- the phone page is open, or nobody has touched the desktop for 30 s: a
  phone push (with a chat it belongs to, a tap opens that chat);
- the user is at the desk: a desktop notification;
- presence unknown (no shell to ask): desktop notification and phone push,
  as before the outbox existed.

An urgent message outside a conversation goes to both the desktop and the
phone. A message may name its channels itself (an approval that must reach
the phone); nothing fans out to every channel by default.

A result an agenda job or a task produced is also kept in the agenda inbox
(core/agenda.py) until a conversation says it; with no conversation open, an
agenda result may wake the assistant to say it (core/daemon.py registers that
as its voice hook). The inbox is the store, not another notification.

Dedupe: a message with a dedupe_key (every result has one, origin + kind) is
delivered at most once per user, across restarts. The key is claimed in
STATE_DIR/outbox.json before any channel delivers it, so a crash during
delivery never repeats it; what was cut off is still in the inbox for the
next conversation.

Acknowledge-first (DoD-7): a voice conversation asks ack() for each request
whose call is expected to take more than SLOW_SECONDS (execution/tools.py
expected()) or goes to a task, a browser action or MyApi. One ack per
request: none when the model already spoke for it. Progress is coalesced and
rate-limited per origin (progress()). A background result the model has not
spoken within a few seconds is delivered once through once().

Every delivery is logged ("outbox: ...") and kept in a short audit ring.
"""

from __future__ import annotations

import collections
import hashlib
import logging
import re
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from . import statefile
from .crashpoints import crashpoint

log = logging.getLogger(__name__)

ACK, PROGRESS, QUESTION, APPROVAL, RESULT, ALERT = (
    "ack",
    "progress",
    "question",
    "approval",
    "result",
    "alert",
)
KINDS = (ACK, PROGRESS, QUESTION, APPROVAL, RESULT, ALERT)
VOICE, CHAT, PUSH, DESKTOP, HUD, SPEAKER, INBOX = (
    "voice",
    "chat",
    "push",
    "desktop",
    "hud",
    "speaker",
    "inbox",
)
NORMAL, URGENT = "normal", "urgent"
# Where the user is (presence()).
AT_VOICE, AT_PHONE, AT_DESK, AWAY, UNKNOWN = "voice", "phone", "desk", "away", "unknown"

SLOW_SECONDS = 5.0  # a call expected to take longer than this is acknowledged at once
ACK_WITHIN_SECONDS = 1.5  # DoD-7: the acknowledgement is heard within this
PROGRESS_EVERY_SECONDS = 12.0
KEEP_SECONDS = 30 * 86400  # a delivered key is remembered this long
MAX_KEYS = 4000
# Requests that always get an acknowledgement when the model is silent: a
# task, a browser action, MyApi (the user's list, 2026-10-18).
ACK_TOOLS = frozenset(
    {
        "start_task",
        "browser_task",
        "browser_control",
        "inspect_browser",
        "run_skill",
        "run_mission",
        "desktop_task",
    }
)

# The ledger's path; None: STATE_DIR/outbox.json, resolved at each use.
LEDGER_PATH: Path | None = None


@dataclass
class Message:
    kind: str
    title: str = ""
    body: str = ""
    origin_id: str = ""  # the turn, task, agenda run or call that caused it
    dedupe_key: str = (
        ""  # delivered at most once; a result defaults to origin_id:result
    )
    channels: tuple[str, ...] = ()  # explicit; empty: route() picks
    priority: str = NORMAL
    ttl: float = 0.0  # seconds; 0: no expiry
    msg_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    created: float = field(default_factory=time.time)
    entry: dict | None = None  # what a conversation says (an agenda inbox entry)
    wake: bool = (
        False  # with no conversation open, the voice hook may start one to say it
    )
    person: str | None = None  # only that person's phones (core/identities.py)
    conversation: str = ""  # a tap on the phone's notice opens this chat
    push_kind: str = "other"  # Flux's marker: "approval"
    desktop_title: str = ""  # when the desktop shows another title than the phone
    actions: dict[str, str] | None = None  # desktop notification buttons {id: label}
    on_action: Callable[[str], Any] | None = None

    def key(self) -> str:
        if self.dedupe_key:
            return self.dedupe_key
        return (
            f"{self.origin_id}:{RESULT}"
            if self.kind == RESULT and self.origin_id
            else ""
        )


_lock = threading.RLock()
_audit: collections.deque = collections.deque(maxlen=200)


def _record(
    kind: str, origin: str, key: str, channels, msg_id: str = "", note: str = ""
) -> None:
    item = {
        "at": time.monotonic(),
        "ts": time.time(),
        "msg_id": msg_id,
        "kind": kind,
        "origin": origin,
        "key": key,
        "channels": list(channels),
        "note": note,
    }
    _audit.append(item)
    log.info(
        "outbox: %s msg=%s origin=%s key=%s -> %s%s",
        kind,
        msg_id or "-",
        origin or "-",
        key or "-",
        ",".join(channels) or "nothing",
        f" ({note})" if note else "",
    )


def audit() -> list[dict]:
    """The latest deliveries, oldest first (tests and doctor)."""
    return list(_audit)


# ---------------------------------------------------------------- ledger


def ledger_path() -> Path:
    if LEDGER_PATH is not None:
        return LEDGER_PATH
    from .. import config

    return config.STATE_DIR / "outbox.json"


def claim(key: str, kind: str = RESULT, msg_id: str = "") -> bool:
    """Record `key` as delivered; False if it already was (here or before a
    restart). Called before delivery: a crash after it never repeats."""
    now = time.time()
    with _lock, statefile.locked_json(ledger_path(), {}) as doc:
        if not isinstance(doc.data, dict):
            doc.data = {}
        keys = doc.data.setdefault("keys", {})
        if key in keys:
            return False
        keys[key] = {"at": round(now, 3), "kind": kind, "msg": msg_id}
        stale = [
            k
            for k, v in keys.items()
            if now - float((v or {}).get("at", 0)) > KEEP_SECONDS
        ]
        for k in stale:
            keys.pop(k, None)
        if len(keys) > MAX_KEYS:
            for k in sorted(keys, key=lambda k: keys[k].get("at", 0))[
                : len(keys) - MAX_KEYS
            ]:
                keys.pop(k, None)
    return True


def delivered(key: str) -> bool:
    data = statefile.read_json(ledger_path(), {})
    return isinstance(data, dict) and key in (data.get("keys") or {})


# ---------------------------------------------------------------- surfaces

# Conversations running on their own event loop (phone calls, the text
# chat, the speaker; one thread each): session -> that conversation's loop.
_calls: dict = {}


def attach_call(session, loop) -> None:
    with _lock:
        _calls[session] = loop


def detach_call(session) -> None:
    with _lock:
        _calls.pop(session, None)


def attached(session) -> bool:
    with _lock:
        return session in _calls


def calls_open() -> bool:
    with _lock:
        return any(not loop.is_closed() for loop in _calls.values())


def announce_to_calls(entry: dict) -> bool:
    """Hand an entry to every open call. True if one will say it."""
    with _lock:
        calls = list(_calls.items())
    heard = False
    for session, loop in calls:
        if not loop.is_closed():
            loop.call_soon_threadsafe(session.announce, dict(entry))
            heard = True
    return heard


@dataclass
class VoiceHook:
    is_open: Callable[[], bool]
    say: Callable[
        [dict], Any
    ]  # says it in an open conversation, else may start one (wake)


_voice_hooks: list[VoiceHook] = []


def register_voice(
    is_open: Callable[[], bool], say: Callable[[dict], Any]
) -> Callable[[], None]:
    """The daemon's desktop conversation. Returns the unregister function."""
    hook = VoiceHook(is_open, say)
    with _lock:
        _voice_hooks.append(hook)

    def unregister() -> None:
        with _lock:
            if hook in _voice_hooks:
                _voice_hooks.remove(hook)

    return unregister


def _inbox_listeners() -> list:
    """core/agenda.py `listeners`: called with an entry the inbox keeps when no
    conversation is open, no voice hook is registered and it may wake her."""
    from . import agenda

    return list(agenda.listeners)


def voice_open() -> bool:
    with _lock:
        hooks = list(_voice_hooks)
    for hook in hooks:
        try:
            if hook.is_open():
                return True
        except Exception:  # noqa: BLE001 -- a broken hook is not an open conversation
            log.debug("outbox: voice hook failed", exc_info=True)
    return calls_open()


def _phone_connected() -> bool:
    from ..phone import presence

    return presence.connected()


def _desk_state() -> str | None:
    """ "active", "recent", "idle" or None (execution/operator.py)."""
    from ..execution import operator

    if operator.user_active() is None:
        return None
    return "idle" if operator.user_idle() else "active"


def presence() -> str:
    if voice_open():
        return AT_VOICE
    try:
        if _phone_connected():
            return AT_PHONE
        state = _desk_state()
    except Exception:  # noqa: BLE001 -- unknown is a valid answer
        log.debug("outbox: presence unavailable", exc_info=True)
        return UNKNOWN
    if state is None:
        return UNKNOWN
    return AWAY if state == "idle" else AT_DESK


def route(msg: Message) -> tuple[str, ...]:
    """The channels for a message (its own, when it names them)."""
    if msg.channels:
        return tuple(msg.channels)
    if msg.kind in (ACK, PROGRESS) or not msg.title:
        return (VOICE,) if voice_open() else ()
    where = presence()
    if where == AT_VOICE:
        return (VOICE,)
    if msg.priority == URGENT:
        return (DESKTOP, PUSH) + ((CHAT,) if msg.conversation else ())
    if where in (AT_PHONE, AWAY):
        return (PUSH,) + ((CHAT,) if msg.conversation else ())
    if where == AT_DESK:
        return (DESKTOP,)
    return (DESKTOP, PUSH) + ((CHAT,) if msg.conversation else ())


# ---------------------------------------------------------------- transports


def _desktop_env():
    try:
        from ..execution.desktop_env import desktop_env

        return desktop_env()
    except Exception:  # noqa: BLE001
        return None


def _desktop(
    title: str,
    body: str,
    urgent: bool = False,
    actions: dict[str, str] | None = None,
    on_action: Callable[[str], Any] | None = None,
) -> None:
    """A desktop notification; with actions, a click calls on_action(id) (in a thread)."""
    argv = ["notify-send", "-a", "Omarchy AI", "-u", "critical" if urgent else "normal"]
    if actions:
        for action_id, label in actions.items():
            argv += ["-A", f"{action_id}={label}"]

        def wait() -> None:
            try:
                proc = subprocess.run(
                    argv + [title, body],
                    capture_output=True,
                    text=True,
                    timeout=3600,
                    env=_desktop_env(),
                )
            except (OSError, subprocess.SubprocessError):
                return
            choice = proc.stdout.strip()
            if choice and on_action is not None:
                on_action(choice)

        threading.Thread(target=wait, daemon=True, name="outbox-desktop").start()
        return
    try:
        subprocess.run(
            argv + [title, body],
            timeout=5,
            check=False,
            env=_desktop_env(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("outbox: notify-send unavailable")


def _push(
    title: str, body: str = "", kind: str = "other", devices: list[str] | None = None
) -> None:
    """On every paired, connected phone through Flux (only `devices`, when given);
    off the caller's thread. Without Flux nothing happens."""
    from ..phone import flux_notify

    if not title or not flux_notify._enabled():
        return
    threading.Thread(
        target=flux_notify.deliver,
        args=(title, body, kind, devices),
        daemon=True,
        name="flux-notify",
    ).start()


def _chat(title: str, kind: str, conversation: str) -> None:
    """A tap on the phone's notice opens the chat it belongs to."""
    from ..phone import flux_notify
    from . import conversations

    conversations.notified(
        flux_notify._titled(flux_notify._masked(title), kind)[:120], conversation
    )


def _say(entry: dict, wake: bool = False) -> bool:
    """In the open conversation; with `wake` and none open, the voice hook may start one."""
    with _lock:
        hooks = list(_voice_hooks)
    if hooks:
        for hook in hooks:
            hook.say(dict(entry))
        return True
    heard = announce_to_calls(entry)
    if not heard and wake:
        for listener in _inbox_listeners():
            try:
                listener(dict(entry))
            except Exception:  # noqa: BLE001
                log.warning("outbox: inbox listener failed", exc_info=True)
    return heard


def _entry(msg: Message) -> dict:
    return msg.entry or {
        "id": msg.msg_id,
        "title": msg.title,
        "detail": msg.body,
        "outcome": msg.kind,
        "urgent": msg.priority == URGENT,
    }


def _devices(person: str | None) -> list[str] | None:
    if not person:
        return None
    from . import identities

    return identities.phones(person) or None


# ---------------------------------------------------------------- send


def send(msg: Message) -> tuple[str, ...]:
    """Deliver once to the surface the user is on. Returns the channels used
    (empty: a duplicate, expired, or nowhere to say it)."""
    key = msg.key()
    if msg.ttl and time.time() - msg.created > msg.ttl:
        _record(msg.kind, msg.origin_id, key, (), msg.msg_id, "expired")
        return ()
    channels = route(msg)
    if key and not claim(key, msg.kind, msg.msg_id):
        _record(msg.kind, msg.origin_id, key, (), msg.msg_id, "duplicate")
        return ()
    crashpoint("outbox.after_claim")
    used: list[str] = []
    urgent = msg.priority == URGENT
    if VOICE in channels and (voice_open() or msg.wake):
        if _say(_entry(msg), wake=msg.wake):
            used.append(VOICE)
    elif msg.entry is not None and msg.wake:
        # Kept in the inbox; with no conversation open it may wake her to say it.
        _wake(msg.entry)
        used.append(INBOX)
    if msg.title:
        if DESKTOP in channels:
            try:
                title = msg.desktop_title or msg.title
                if msg.actions:
                    _desktop(
                        title,
                        msg.body,
                        urgent,
                        actions=msg.actions,
                        on_action=msg.on_action,
                    )
                else:
                    _desktop(title, msg.body, urgent)
                used.append(DESKTOP)
            except Exception:  # noqa: BLE001 -- the other channels still go out
                log.warning("outbox: desktop notification failed", exc_info=True)
        if CHAT in channels and msg.conversation:
            try:
                _chat(msg.title, msg.push_kind, msg.conversation)
                used.append(CHAT)
            except Exception:  # noqa: BLE001
                log.debug("outbox: chat link unavailable", exc_info=True)
        if PUSH in channels:
            try:
                _push(msg.title, msg.body, msg.push_kind, _devices(msg.person))
                used.append(PUSH)
            except Exception:  # noqa: BLE001
                log.debug("outbox: phone push unavailable", exc_info=True)
    _record(msg.kind, msg.origin_id, key, used, msg.msg_id)
    return tuple(used)


def _wake(entry: dict) -> None:
    with _lock:
        hooks = list(_voice_hooks)
    for hook in hooks:
        hook.say(dict(entry))
    if not hooks:
        for listener in _inbox_listeners():
            try:
                listener(dict(entry))
            except Exception:  # noqa: BLE001
                log.warning("outbox: inbox listener failed", exc_info=True)


def alert(
    title: str,
    body: str = "",
    urgent: bool = False,
    person: str | None = None,
    conversation: str = "",
    *,
    origin_id: str = "",
    kind: str = ALERT,
    dedupe_key: str = "",
) -> tuple[str, ...]:
    """A plain notice (what agenda.notify was): routed like any message."""
    return send(
        Message(
            kind,
            title[:120],
            body[:400],
            origin_id=origin_id,
            dedupe_key=dedupe_key,
            priority=URGENT if urgent else NORMAL,
            person=person,
            conversation=conversation,
        )
    )


# ---------------------------------------------------------------- acknowledge-first

_acks: collections.OrderedDict[str, float] = collections.OrderedDict()


def needs_ack(tool: str) -> bool:
    """Whether a call starting now should be acknowledged at once."""
    from ..execution import tools

    return (
        tool in ACK_TOOLS
        or tool.startswith("myapi")
        or tools.expected(tool) > SLOW_SECONDS
    )


def acked(origin_id: str) -> bool:
    with _lock:
        return origin_id in _acks


def model_acked(origin_id: str) -> None:
    """The model spoke for this request itself: no automatic ack for it."""
    with _lock:
        if origin_id not in _acks:
            _acks[origin_id] = time.monotonic()
            _record(ACK, origin_id, "", ("model",), note="the model spoke first")
        while len(_acks) > 500:
            _acks.popitem(last=False)


def ack(origin_id: str, deliver: Callable[[], Any], channel: str = VOICE) -> bool:
    """One acknowledgement per request: False when it was already acknowledged
    (by the outbox or by the model). `deliver` says it (a clip, a chat line)."""
    with _lock:
        if origin_id in _acks:
            return False
        _acks[origin_id] = time.monotonic()
        while len(_acks) > 500:
            _acks.popitem(last=False)
    deliver()
    _record(ACK, origin_id, "", (channel,))
    return True


def once(
    key: str,
    deliver: Callable[[], Any],
    *,
    kind: str = RESULT,
    origin_id: str = "",
    channel: str = VOICE,
) -> bool:
    """Deliver through a conversation's own `deliver`, at most once per key (durable)."""
    if not claim(key, kind):
        _record(kind, origin_id, key, (), note="duplicate")
        return False
    deliver()
    _record(kind, origin_id, key, (channel,))
    return True


_progress: dict[str, list] = {}


def progress(
    origin_id: str,
    text: str,
    deliver: Callable[[str], Any],
    *,
    every: float = PROGRESS_EVERY_SECONDS,
    now: float | None = None,
) -> bool:
    """Rate-limited and coalesced: at most one progress note per origin per
    `every` seconds; notes offered in between are replaced by the newest,
    which is what the next allowed call delivers. True when delivered."""
    now = time.monotonic() if now is None else now
    with _lock:
        state = _progress.setdefault(origin_id, [-1e9, ""])
        if now - state[0] < every:
            state[1] = text
            return False
        state[0], state[1] = now, ""
        if len(_progress) > 500:
            _progress.pop(next(iter(_progress)))
    deliver(text)
    _record(PROGRESS, origin_id, "", (VOICE,))
    return True


# The user's standing rule for acknowledging, if one quotes the words: a
# learned preference such as "acknowledge with an assertive 'On it!'".
_ACK_RULE = re.compile(
    r"acknowledg|\backs?\b|\bon it\b|אישור|לאשר|תאשר|כשאת מתחילה|כשאתה מתחיל", re.I
)
_QUOTE = "\"'“”„‘’`"
_QUOTED = re.compile(f"[{_QUOTE}]([^{_QUOTE}\\n]{{1,24}})[{_QUOTE}]")


def ack_phrase(preferences: list[str] | None = None) -> str | None:
    """The quoted words of the newest learned rule about acknowledging, or None."""
    if preferences is None:
        try:
            from . import memory

            preferences = memory.load_preferences()
        except Exception:  # noqa: BLE001
            return None
    for text in reversed(preferences or []):
        if _ACK_RULE.search(text):
            found = _QUOTED.search(text)
            if found and found.group(1).strip():
                return found.group(1).strip()
    return None


def phrase_language(text: str) -> str | None:
    """A language the script alone gives away (Hebrew, Arabic, Cyrillic, Greek), else None."""
    for low, high, lang in (
        (0x0590, 0x05FF, "he"),
        (0x0600, 0x06FF, "ar"),
        (0x0400, 0x04FF, "ru"),
        (0x0370, 0x03FF, "el"),
    ):
        if any(low <= ord(c) <= high for c in text):
            return lang
    return None


def ack_stem(phrase: str) -> str:
    return "ack-" + hashlib.sha1(phrase.encode()).hexdigest()[:8]


def reset_for_tests() -> None:
    """Forget in-memory state (acks, progress, surfaces, audit)."""
    with _lock:
        _acks.clear()
        _progress.clear()
        _calls.clear()
        _voice_hooks.clear()
        _audit.clear()
