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


# -- what reaches the phone ---------------------------------------------------
# Real task 2026-10-26 15:08 ("mirror the screen to the living-room TV"): the
# phone's chat got "I need your input to continue: <Hebrew question>
# SUMMARY: ... FILES CHANGED: None. TESTS RUN: ... RISKS: ...", the same text
# again as "Progress: ...", **markdown**, and two "Cancelled by the user."
# cards. Everything a person reads about a task (chat cards, notices) goes
# through these: the words a person would say, nothing of the report format.

_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+", re.MULTILINE)
_EMPHASIS = re.compile(r"\*\*|__|(?<![\w*])\*(?=\S)|(?<=\S)\*(?![\w*])")
_SENTENCE = re.compile(r"(?<=[.!?؟])\s+")
_JEV = re.compile(r"\bJev\b")
_HEBREW = re.compile(r"[֐-׿]")
_LATIN = re.compile(r"[A-Za-z]")
_NOT_VERIFIED = re.compile(r"\n*\s*Not verified:", re.IGNORECASE)


def clean(text: str) -> str:
    """A worker's text as a person reads it: no "I need your input" prefix,
    no SUMMARY/FILES CHANGED/... tail, no markdown marks, one line."""
    text = strip_prefix(text)
    match = _TAIL.search(text)
    if match:
        text = text[: match.start()]
    text = _HEADING.sub("", text)
    text = _EMPHASIS.sub("", text.replace("`", ""))
    return " ".join(text.split())


def brief(text: str, limit: int) -> str:
    """Whole sentences up to `limit` characters (a cut word gets an ellipsis)."""
    text = " ".join(str(text or "").split())
    if len(text) <= limit:
        return text
    out = ""
    for sentence in _SENTENCE.split(text):
        if len(out) + len(sentence) + 1 > limit:
            break
        out = f"{out} {sentence}".strip()
    if out:
        return out
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return cut.rstrip(" ,;:—-") + "…"


def ask_text(question: str, limit: int = 320) -> str:
    """A question card: the question in the worker's (the user's) words, with
    at most the sentence that leads into it, and no English preamble."""
    text = clean(question)
    if len(text) > limit and ("?" in text or "؟" in text):
        sentences = _SENTENCE.split(text)
        asked = [i for i, s in enumerate(sentences) if "?" in s or "؟" in s]
        keep = sorted({i for i in asked} | ({asked[0] - 1} if asked[0] > 0 else set()))
        text = " ".join(sentences[i] for i in keep)
        if len(text) > limit:
            text = " ".join(sentences[i] for i in asked)
    return brief(text, limit)


def result_text(result: str, limit: int = 280) -> str:
    """A result card: a sentence or two, with "not verified" said plainly and
    nothing about the deciding model or the workers' report format."""
    text = str(result or "")
    parts = _NOT_VERIFIED.split(text, maxsplit=1)
    unverified = len(parts) > 1
    sentences = []
    for sentence in _SENTENCE.split(clean(parts[0])):
        if _JEV.search(sentence):
            # "I could not complete this: Jev judged that ..." keeps its first half.
            head = sentence.split(":", 1)[0]
            sentence = f"{head}." if ":" in sentence and not _JEV.search(head) else ""
        if sentence:
            sentences.append(sentence)
    body = brief(" ".join(sentences), limit)
    if unverified:
        return f"{body} (Not verified.)" if body else "Finished, but I could not verify the result."
    return body or brief(clean(text), limit)


def hebrew(text: str) -> bool:
    """The text is mostly Hebrew (more Hebrew letters than Latin ones)."""
    text = str(text or "")
    return len(_HEBREW.findall(text)) > len(_LATIN.findall(text))


# -- questions already answered ----------------------------------------------
# The same task: the worker asked "which screen should I mirror?" four times.
# The answers ("living room", "there is only one TV", "mirror the COMPUTER!!!")
# reached it only inside a JSON context that its prompt truncated, and nothing
# stopped the same question from going back to the user.

_TOKEN = re.compile(r"\w+", re.UNICODE)
SAME_QUESTION_OVERLAP = 0.5


def _question_words(text: str) -> set[str]:
    sentences = [s for s in _SENTENCE.split(clean(text)) if "?" in s or "؟" in s] or [clean(text)]
    words = set()
    for word in _TOKEN.findall(" ".join(sentences).lower()):
        # Hebrew attaches "the/and/to/in/that" to the word: הטלפון = טלפון.
        if _HEBREW.match(word) and len(word) > 3 and word[0] in "הובלש":
            word = word[1:]
        if len(word) > 1:
            words.add(word)
    return words


def similar(a: str, b: str) -> bool:
    """Two questions ask for the same thing, judged by their words (the
    fallback when Jev is unavailable: it does not understand paraphrase)."""
    x, y = _question_words(a), _question_words(b)
    return bool(x and y) and len(x & y) / len(x | y) >= SAME_QUESTION_OVERLAP


def answered_block(asked: list[str], answers: list[str]) -> str:
    """What the user already said, put in front of the worker's assignment."""
    answers = [str(a).strip() for a in answers if str(a).strip()]
    if not answers:
        return ""
    lines = ["THE USER ALREADY ANSWERED (read this before anything else; it is part of the request):"]
    if asked:
        lines.append("Questions already put to the user:")
        lines += [f"- {brief(clean(q), 300)}" for q in asked[-4:]]
    lines.append("The user's answers, oldest first (the last one is the most recent):")
    lines += [f"{i}. {brief(a, 600)}" for i, a in enumerate(answers[-6:], 1)]
    lines.append("Act on these answers now. Do not ask any of these questions again, or one that asks for the "
                 "same thing in other words: when something still seems open, take the most reasonable reading "
                 "of the request and these answers, do it, and say in your report what you assumed.")
    return "\n".join(lines)
