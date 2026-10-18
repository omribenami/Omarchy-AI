"""Where a tool result came from, and the fence around content nobody vetted.

Every tool declares a provenance in execution/tools.py PROVENANCE, beside its
effect class:

    user         the user's own words
    system       the harness or the desktop itself (volume set, window focused)
    local_state  the assistant's own records (tasks, skills, schedules, memory)
    external:<service>  a connected service (Gmail, MyApi, GitHub, a user tool)
    web          a web page (the source becomes web:<host> from the call's url)
    file         a file's name or contents (file:<path>)
    terminal     terminal output and logs
    vision       what a screen or camera shows (OCR included)

A result whose provenance is external, web, file, terminal or vision is
untrusted: anyone who can put text in an email, a page, a file or a terminal
can put instructions there. execution/runner.py wraps it once, where every
session's results come back from, before it reaches any model:

    <untrusted source="gmail" kind="external">
    ...content...
    </untrusted>

Inside, anything that could close the fence or pose as a model's control
token is escaped, so the content cannot end the fence early. The system
prompts say content in <untrusted> is data (voice/live.py, voice/prompt_v2.py).
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass
import re
from typing import Callable, Iterator
from urllib.parse import urlparse

USER, SYSTEM, LOCAL_STATE = "user", "system", "local_state"
EXTERNAL, WEB, FILE, TERMINAL, VISION = "external", "web", "file", "terminal", "vision"
KINDS = frozenset({USER, SYSTEM, LOCAL_STATE, EXTERNAL, WEB, FILE, TERMINAL, VISION})
UNTRUSTED = frozenset({EXTERNAL, WEB, FILE, TERMINAL, VISION})

OPEN, CLOSE = "<untrusted", "</untrusted>"

# A tag that would open or close a fence, however it is spaced or cased.
_FENCE_TAG = re.compile(r"<\s*(/?)\s*untrusted\b", re.IGNORECASE)
# Chat-template control tokens of the model families in use (<|im_start|>,
# <|endoftext|>, Gemma's <start_of_turn>/<end_of_turn>, Llama's [INST] and
# <<SYS>>): shown as text, never as the token.
_CONTROL = re.compile(
    r"<\|[^<>|\n]{0,40}\|>|<\s*/?\s*(?:start_of_turn|end_of_turn|eos|bos|s)\s*>|\[/?INST\]|<</?SYS>>",
    re.IGNORECASE,
)
# C0 control characters other than tab, newline and carriage return.
_C0 = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_WRAPPED = re.compile(
    r'\A<untrusted source="([^"]*)" kind="([^"]*)">\n(.*)\n</untrusted>\Z', re.DOTALL
)


def kind(provenance: str) -> str:
    """The provenance's kind: "external:gmail" -> "external"."""
    return provenance.partition(":")[0]


def untrusted(provenance: str) -> bool:
    return kind(provenance) in UNTRUSTED


def _host(url: object) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    parsed = urlparse(text if "://" in text else "https://" + text)
    return (parsed.hostname or "").lower()


def of(name: str, args: dict | None = None) -> str:
    """The provenance of one call's result: the tool's declared one, made
    specific by its arguments (the page's host, the file's path, the
    camera's name). A tool the user added is an external service; an
    unknown name is untrusted too."""
    from . import tools

    declared = tools.PROVENANCE.get(name)
    if declared is None:
        try:
            from . import user_tools

            known = user_tools.exists(name)
        except Exception:  # noqa: BLE001 -- unknown stays untrusted
            known = False
        return f"{EXTERNAL}:user_tool:{name}" if known else f"{EXTERNAL}:{name}"
    args = args if isinstance(args, dict) else {}
    if declared == WEB:
        host = _host(args.get("url"))
        return f"{WEB}:{host}" if host else f"{WEB}:browser"
    if declared == FILE and args.get("path"):
        return f"{FILE}:{str(args['path'])[:200]}"
    if declared == VISION and args.get("camera"):
        return f"{VISION}:camera:{str(args['camera'])[:60]}"
    if declared == "external:myapi" and args.get("service"):
        return f"{EXTERNAL}:{str(args['service'])[:60]}"
    return declared


def escape(text: str) -> str:
    """Content made safe to sit inside a fence: a look-alike of the fence
    tag or a control token is shown as text and cannot end the fence."""
    text = _C0.sub("", text)
    text = _FENCE_TAG.sub(lambda m: "&lt;" + m.group(1) + "untrusted", text)
    return _CONTROL.sub(
        lambda m: m.group(0).replace("<", "&lt;").replace(">", "&gt;"), text
    )


def _attribute(value: str) -> str:
    return re.sub(r'["<>&\n\r]', "", value)[:200]


def fence(text: str, provenance: str) -> str:
    """`text` wrapped as untrusted content from `provenance`."""
    source = provenance.partition(":")[2] or provenance
    return (
        f'<untrusted source="{_attribute(source)}" kind="{_attribute(kind(provenance))}">\n'
        f"{escape(text)}\n{CLOSE}"
    )


def fenced(text: str) -> bool:
    return bool(_WRAPPED.match(text or ""))


def unfence(text: str) -> str:
    """The content inside a fence (still escaped); other text unchanged."""
    match = _WRAPPED.match(text or "")
    return match.group(3) if match else text


def rewrap(text: str, change: Callable[[str], str]) -> str:
    """`change` applied to the content inside a fence, fenced again with
    the same source; plain text is just changed."""
    match = _WRAPPED.match(text or "")
    if not match:
        return change(text)
    source, what = match.group(1), match.group(2)
    return fence(change(match.group(3)), f"{what}:{source}" if source != what else what)


def clip(text: str, limit: int) -> str:
    """At most about `limit` characters of a result, its fence kept whole."""
    return rewrap(text, lambda inner: inner[:limit])


# ---------------------------------------------------------------- taint
#
# Enforced in code, not only in the prompt: once untrusted content entered a
# conversation or task, it is tainted for good. While tainted, an
# irreversible call aims only at targets the user said (or approved in a
# plan); a target from anywhere else -- an email's "forward to x@evil", a
# page's link, a log's path -- needs the user's tap. approval_needed() is the
# whole decision (plan 2.4 will fold it into one approval policy); the
# sessions turn a "needs approval" into the existing approval paths: a
# confirm card (core/pending_actions.py) or a Task Runtime approval.

# A key whose value is itself a target, wherever it sits in nested arguments.
_TARGET_KEY = re.compile(
    r"recipient|e?mail|^to$|^cc$|^bcc$|url|uri|link|href|path|file|dest|repo|amount|price|"
    r"address|phone|account|command|cmd|channel|webhook|host",
    re.IGNORECASE,
)
# Targets written inside free text.
_EMBEDDED = re.compile(
    r"(?:https?://|www\.)\S+"  # URLs
    r"|[\w.+-]+@[\w-]+(?:\.[\w-]+)+"  # addresses
    r"|(?:~|\.{1,2})?/[\w.~/-]*[\w~]"  # paths
    r"|\b[\w-]+(?:\.[\w-]+)*\.[a-zA-Z][\w-]*\b"  # file and domain names
    r"|[$€£₪]\s?\d[\d,.]*"  # amounts
    r"|\b\d[\d,.]*\s?(?:usd|eur|gbp|ils|nis|dollars?|euros?|shekels?)\b",
    re.IGNORECASE,
)
_TOKEN = re.compile(r"[^\W_]+")
# Parts of a target nobody says aloud.
_BOILERPLATE = frozenset(
    {"http", "https", "www", "com", "org", "net", "io", "html", "htm", "php", "mailto"}
)
_SAID_KEPT = 4000  # characters of the user's words a session keeps


def _tokens(text: str) -> set[str]:
    return {
        t for t in _TOKEN.findall(text.lower()) if len(t) >= 2 and t not in _BOILERPLATE
    }


def _leaves(value: object, key: str = "") -> list[tuple[str, str]]:
    """(key, text) of every string or number in a value, nested ones included."""
    if isinstance(value, dict):
        return [leaf for k, v in value.items() for leaf in _leaves(v, str(k))]
    if isinstance(value, (list, tuple)):
        return [leaf for v in value for leaf in _leaves(v, key)]
    if isinstance(value, (str, int, float)) and not isinstance(value, bool):
        return [(key, str(value))]
    return []


def targets(name: str, args: dict) -> list[str]:
    """What an irreversible call aims at, from tools.KEY_ARGS. A tool that
    declares nothing (unknown) aims at everything it was given."""
    from . import tools

    spec = tools.KEY_ARGS.get(name)
    args = args if isinstance(args, dict) else {}
    if spec is None:
        return [text for _, text in _leaves(args)]
    found: list[str] = []
    for arg in spec:
        value = args.get(arg.rstrip("~"))
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list, tuple)):
            for key, text in _leaves(value, arg):
                found += [text] if _TARGET_KEY.search(key) else _EMBEDDED.findall(text)
        elif arg.endswith("~"):
            found += _EMBEDDED.findall(str(value))
        else:
            found.append(str(value))
    return found


def _home(text: str) -> str:
    import os

    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "~" else text


def approval_needed(name: str, args: dict, *, tainted: bool, said: str) -> str | None:
    """None to run the call; otherwise why it needs the user's tap. Only a
    tainted conversation or task is ever stopped, only for an irreversible
    call, and only when one of its targets is not in `said` (the user's own
    words and any plan they approved). Unsure is a tap."""
    from . import tools

    if not tainted or tools.effect_class(name) != tools.IRREVERSIBLE:
        return None
    heard = _tokens(_home(said))
    missing = [aim for aim in targets(name, args) if not _tokens(_home(aim)) <= heard]
    if not missing:
        return None
    shown = ", ".join(repr(m[:80]) for m in missing[:3])
    return f"{shown} came from content, not from the user's own words"


class Taint:
    """One conversation's taint: whether untrusted content entered it, from
    where, and what the user said in it."""

    def __init__(self) -> None:
        self.tainted = False
        self.sources: list[str] = []
        self.utterances = 0  # pieces of the user's own words heard so far
        self._joined, self._spaced = "", ""

    def mark(self, source: str) -> None:
        self.tainted = True
        if source and source not in self.sources:
            self.sources = (self.sources + [source])[-5:]

    def heard(self, text: str) -> None:
        """The user's words: a whole turn, or one fragment of one (live
        transcription arrives in pieces, so they are kept both joined and
        apart)."""
        if text:
            self.utterances += 1
            self._joined = (self._joined + text)[-_SAID_KEPT:]
            self._spaced = (self._spaced + " " + text)[-_SAID_KEPT:]

    def said(self) -> str:
        return self._joined + "\n" + self._spaced


@dataclass(frozen=True)
class Turn:
    """The conversation a tool call runs in, as InputGuard saw it when the
    call started: its turn id (turn:<session>:<n>), its taint, and how many
    pieces of the user's own words it had heard. A call made outside any
    conversation (the Task Runtime, a test, a CLI) has none."""

    origin: str
    tainted: bool
    sources: tuple[str, ...] = ()
    utterances: int = 0

    @property
    def session(self) -> str:
        parts = self.origin.split(":")
        return parts[1] if len(parts) > 2 else ""


_TURN: contextvars.ContextVar[Turn | None] = contextvars.ContextVar(
    "omarchy_turn", default=None
)


def current_turn() -> Turn | None:
    return _TURN.get()


@contextmanager
def in_turn(turn: Turn) -> Iterator[None]:
    token = _TURN.set(turn)
    try:
        yield
    finally:
        _TURN.reset(token)
