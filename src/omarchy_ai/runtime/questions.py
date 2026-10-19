"""What a task's waiting text really is: a question, a finished result, or a cancellation.

Real task 2026-10-17 20:38 ("turn on the dining-room light"): the light was
on after one step, Jev picked ASK_USER, and the worker's finished report
became the task's "question". It waited a day. The user typed "cancel it"
into the phone's answer box; that went to the worker as an answer, the
worker said "cancelled", and that became the next "question". Each later
"ok" or "what's the question here??" resumed it, and it turned the lights on
three more times. These helpers keep that apart: an answer that says cancel
cancels, a report with no question in it ends the task, and a question is
something the user can actually answer.
"""

from __future__ import annotations

import re

ASK_PREFIX = "I need your input to continue:"
# A waiting task whose "question" asks nothing is closed after this long
# (runtime.expire_stale_questions on the heartbeat; doctor's tasks check).
STALE_QUESTION_HOURS = 4.0

# A worker report's structured tail (runtime/executors/coding_agents.py
# asks for these lines): its prose is what comes before them.
_TAIL = re.compile(
    r"^\s*(SUMMARY|FILES CHANGED|TESTS RUN|RISKS|COMMANDS THAT DID IT|TEST_COMMAND)\s*:",
    re.MULTILINE,
)
_CODE = re.compile(r"`[^`]*`")

# The whole answer is a request to stop the task: short, and nothing else.
_CANCEL_WORDS = {
    "cancel",
    "cancelled",
    "canceled",
    "stop",
    "abort",
    "nevermind",
    "never mind",
    "forget it",
    "forget about it",
    "drop it",
    "בטל",
    "תבטל",
    "תבטלי",
    "לבטל",
    "ביטול",
    "עצור",
    "תעצור",
    "תעצרי",
    "עזוב",
    "עזבי",
    "תעזוב",
    "תעזבי",
    "די",
    "לא משנה",
}
_FILLER = {
    "it",
    "this",
    "that",
    "the",
    "task",
    "job",
    "please",
    "pls",
    "now",
    "just",
    "ok",
    "okay",
    "את",
    "זה",
    "המשימה",
    "בבקשה",
    "עכשיו",
    "כבר",
    "אותה",
    "אותו",
}
_WORDS = re.compile(r"[^\w\s']+", re.UNICODE)

# A worker saying the task itself was cancelled, in its prose or SUMMARY line.
_CANCEL_ACK = re.compile(
    r"\b(task|request|job|work)\s+(was\s+|has\s+been\s+|is\s+)?(cancel+ed|aborted|stopped)\b"
    r"|\bcancel+ed\s+(by|at|per|as)\s+(the\s+)?user"
    r"|\bcancel+ed\s+(at|per)\s+your\s+request"
    r"|בוטל(ה)?\s+לפי\s+בקשתך|המשימה\s+בוטלה",
    re.IGNORECASE,
)


def strip_prefix(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith(ASK_PREFIX):
        text = text[len(ASK_PREFIX) :].strip()
    return text


def prose(text: str) -> str:
    """A report without its structured tail or inline code."""
    text = strip_prefix(text)
    match = _TAIL.search(text)
    if match:
        text = text[: match.start()]
    return _CODE.sub("", text).strip()


def is_question(text: str) -> bool:
    """The text asks the user something: a question mark in its prose."""
    words = prose(text)
    return "?" in words or "؟" in words


def cancel_intent(answer: str) -> bool:
    """The user's whole answer says to cancel the task ("cancel it", "בטל").

    Every other word must be filler, so "don't cancel", "no, cancel the
    meeting instead" or a longer instruction is an ordinary answer."""
    text = f" {' '.join(_WORDS.sub(' ', str(answer or '').lower()).split())} "
    if len(text.split()) > 5:
        return False
    found = False
    for phrase in sorted(_CANCEL_WORDS, key=len, reverse=True):
        if f" {phrase} " in text:
            found = True
            text = text.replace(f" {phrase} ", " ")
    return found and all(word in _FILLER for word in text.split())


def cancel_acknowledged(text: str) -> bool:
    """A worker's report says the task itself was cancelled."""
    return bool(_CANCEL_ACK.search(strip_prefix(text)))
