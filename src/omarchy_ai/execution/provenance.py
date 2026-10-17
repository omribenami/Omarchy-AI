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

import re
from typing import Callable
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
