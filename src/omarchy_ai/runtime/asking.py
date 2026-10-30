"""Where a task's question reaches the user, and how they answer it by voice.

The user, 2026-10-28: questions that background tasks ask were mostly lost.
A task started from a voice speaker posted its "question" card into a chat
the user could not see while standing at the speaker, and nothing said it.

- Speaker first: a question of a task that was asked for on a voice
  satellite is said on THAT speaker (voice/satellite.py ask()), in its open
  conversation or in a short one she starts; the reply goes to the task
  (task_respond). Not answered within SPEAKER_ANSWER_SECONDS, it goes to the
  phone as well.
- Otherwise (no speaker, or that speaker is not streaming its microphone)
  the runtime's own notice goes out (runtime._notify): the phone push, whose
  tap opens the chat on that question.
- A display elsewhere (a wall tablet) is told about every open question
  through the satellite's state command (satellite_state_command): one JSON
  line {"question": "open" | "answered" | "withdrawn", "id", "text",
  "speaker"} per change, the open ones again every QUESTION_REFRESH_SECONDS
  so a display that missed one, or restarted, catches up. A tap there may ask
  for the question to be put on a speaker (ask_from_display): only a
  question that is open right now, and only its own words from the task.

A question is said only where it was put: put_to() records the conversations
that heard it, so the user's spoken reply there may answer it
(service._in_this_conversation).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid

log = logging.getLogger(__name__)

# Said on the speaker and not answered this long: the phone gets it too.
SPEAKER_ANSWER_SECONDS = 150.0
# Open questions are sent to the display again this often.
QUESTION_REFRESH_SECONDS = 60.0
# A display tap puts the same question on a speaker at most this often.
DISPLAY_ASK_SECONDS = 30.0
DISPLAY_TEXT_CHARS = 160

_lock = threading.Lock()
_open: dict[str, dict] = {}            # question id -> the "open" line last sent
_heard_in: dict[str, set[str]] = {}    # task id -> conversations that heard its question
_display_asked: dict[str, float] = {}  # question id -> when a display tap last asked it


def question_id(task) -> str:
    """One id per question a task asked (its count in budget["asked"])."""
    return f"{task.id}.{len(task.budget.get('asked') or [])}"


def shown(task) -> str:
    """The question as a person hears or reads it; "" when it says nothing."""
    from . import questions
    return questions.ask_text(str(getattr(task, "question", "") or ""))


def speaker_of(task) -> str | None:
    """The speaker (its host) the task was asked for on; "" for a speaker
    conversation from before speakers were told apart (the default one);
    None when it was not asked on a speaker."""
    from ..core import conversations
    cid = getattr(task, "conversation", "") or ""
    if not cid or conversations.source(cid) != "satellite":
        return None
    return conversations.device(cid)


def put_to(task_id: str, cid: str) -> None:
    """A conversation said this task's question (voice/gemini_live.py)."""
    if not task_id or not cid:
        return
    with _lock:
        _heard_in.setdefault(task_id, set()).add(cid)
        while len(_heard_in) > 200:
            _heard_in.pop(next(iter(_heard_in)))


def was_put_to(task_id: str, cid: str) -> bool:
    with _lock:
        return cid in _heard_in.get(task_id or "", set())


def speaker_entry(task, host: str, entry: dict | None = None) -> dict:
    """What the speaker's conversation says: the agenda entry for this
    question when there is one (so a conversation that says it marks it
    heard), with the question, the task and where it was asked."""
    from ..core import conversations
    from .task import request_text
    question = shown(task)
    entry = dict(entry or {"id": uuid.uuid4().hex[:10], "job_id": f"task-{task.id}", "kind": "task",
                           "urgent": False, "at": time.time(), "delivered": False})
    cid = getattr(task, "conversation", "") or ""
    entry.update(task_id=task.id, outcome="waiting_user", title=request_text(task.goal)[:160],
                 question=question, question_id=question_id(task), speaker=host,
                 detail=f"Task {task.id} is paused with a question for you: {question}",
                 # Her conversation goes on in the task's own chat when it is
                 # this speaker's: the spoken answer lands next to the card.
                 conversation=cid if cid and conversations.device(cid) == host else "")
    return entry


def deliver(task, entry: dict | None = None) -> bool:
    """A task now waits on its question (the daemon's task listener). True
    when it was handled here: said on the task's speaker, or nothing to say.
    False: the usual notice goes out (runtime._notify: the phone)."""
    if not shown(task):
        log.warning("task %s waits on a question with no words in it; not said or sent: %r", task.id,
                    str(task.question or "")[:200])
        return True
    host = speaker_of(task)
    if host is None:
        return False
    from ..core import outbox
    from ..voice import satellite
    from . import runtime as runtime_module
    _, _, fingerprint = runtime_module.notice(task, "waiting_user")
    key = runtime_module.outbox_key(task, fingerprint)
    if outbox.delivered(key):
        return True
    if not satellite.reachable(host):
        log.info("task %s: its speaker %s is not listening; the question goes to the phone", task.id,
                 host or "(default)")
        return False
    if not satellite.ask(speaker_entry(task, host, entry)):
        return False
    outbox.claim(key, outbox.QUESTION)  # said there: the runtime's notice is not sent as well
    log.info("task %s: question said on the speaker %s", task.id, host or "(default)")
    timer = threading.Timer(SPEAKER_ANSWER_SECONDS, _phone_if_unanswered, (task.id, question_id(task), key))
    timer.daemon = True
    timer.start()
    return True


def _phone_if_unanswered(task_id: str, asked: str, key: str) -> None:
    from . import runtime as runtime_module
    from .service import get_runtime
    from .task import WAITING_USER
    try:
        task = get_runtime().store.load(task_id)
        if task is None or task.status != WAITING_USER or question_id(task) != asked:
            return  # answered, cancelled or asked anew meanwhile
        log.info("task %s: the question said on the speaker was not answered; sent to the phone", task_id)
        runtime_module.send_question(task, f"{key}:phone", away=True)
    except Exception:  # noqa: BLE001 -- a timer thread must not die loudly
        log.warning("could not send task %s's question to the phone", task_id, exc_info=True)


# -- the display's lines ---------------------------------------------------

def _line(task, host: str | None) -> dict:
    from . import questions
    return {"question": "open", "id": question_id(task),
            "text": questions.brief(shown(task), DISPLAY_TEXT_CHARS), "speaker": host or ""}


def current() -> dict[str, tuple]:
    """The open questions now: {question id: (task, its display line)}."""
    from .service import get_runtime
    from .task import WAITING_USER
    out = {}
    for task in get_runtime().store.list(50):
        if task.status == WAITING_USER and task.source != "cli" and shown(task):
            out[question_id(task)] = (task, _line(task, speaker_of(task)))
    return out


def _tell(line: dict) -> None:
    from ..voice import satellite
    satellite.tell_question(line)


def _closed(qid: str, task) -> dict:
    """The line that ends an open question: answered when the task got an
    answer after it was asked, else withdrawn (cancelled, finished, dropped)."""
    asked = (task.budget.get("asked") or []) if task is not None else []
    index = int(qid.rsplit(".", 1)[-1]) - 1
    before = int(asked[index].get("answers", 0)) if 0 <= index < len(asked) else None
    answered = before is not None and len(task.answers or []) > before
    return {"question": "answered" if answered else "withdrawn", "id": qid}


def on_event(task, event: str) -> None:
    """Task listener: an open question's line when a task starts waiting on
    it, its end when the task moves on."""
    if task.source == "cli":
        return
    if event == "waiting_user" and shown(task):
        line = _line(task, speaker_of(task))
        with _lock:
            stale = [q for q in _open if q.rsplit(".", 1)[0] == task.id and q != line["id"]]
            for q in stale:
                _open.pop(q)
            new = _open.get(line["id"]) != line
            _open[line["id"]] = line
        for q in stale:
            _tell({**_closed(q, task), "speaker": line["speaker"]})
        if new:
            _tell(line)
        return
    with _lock:
        gone = [(q, _open.pop(q)) for q in list(_open) if q.rsplit(".", 1)[0] == task.id]
    for q, line in gone:
        _tell({**_closed(q, task), "speaker": line.get("speaker", "")})


def sync(refresh: bool = True) -> None:
    """Bring the display up to date from the task store (another process may
    have answered one): ends the questions no longer open, and sends the open
    ones (all of them again with `refresh`)."""
    now = current()
    with _lock:
        gone = [(q, _open.pop(q)) for q in list(_open) if q not in now]
        send = [line for q, (_, line) in now.items() if refresh or _open.get(q) != line]
        for q, (_, line) in now.items():
            _open[q] = line
    from .service import get_runtime
    store = get_runtime().store
    for q, line in gone:
        _tell({**_closed(q, store.load(q.rsplit(".", 1)[0])), "speaker": line.get("speaker", "")})
    for line in send:
        _tell(line)


def ask_from_display(qid: str, host: str | None = None) -> tuple[int, str]:
    """A display tapped an open question: put it on a speaker so the user
    answers by voice. (HTTP status, message). Nothing but the task's own
    question can be said this way: the request names an id, not words."""
    from ..voice import satellite
    found = current().get(str(qid or ""))
    if found is None:
        return 404, "no such question is waiting"
    task, line = found
    now = time.monotonic()
    with _lock:
        if now - _display_asked.get(line["id"], -DISPLAY_ASK_SECONDS) < DISPLAY_ASK_SECONDS:
            return 429, "already asked on the speaker just now"
        _display_asked[line["id"]] = now
    for choice in (host, line["speaker"], ""):
        if choice is not None and satellite.known(choice):
            host = choice
            break
    else:
        return 503, "no speaker is running"
    if not satellite.reachable(host) or not satellite.ask(speaker_entry(task, host)):
        with _lock:
            _display_asked.pop(line["id"], None)
        return 503, "that speaker is not listening right now"
    log.info("task %s: a display asked for its question on the speaker %s", task.id, host or "(default)")
    return 202, "asking on the speaker"


def reset_for_tests() -> None:
    with _lock:
        _open.clear()
        _heard_in.clear()
        _display_asked.clear()
