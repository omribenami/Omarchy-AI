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
import hashlib
import html
import ipaddress
import re
from typing import Callable, Collection, Iterable, Iterator, NamedTuple
from urllib.parse import unquote, unquote_plus, urlparse

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
    if name == "task_status" and str(args.get("task_id") or "").startswith("w-"):
        # A background worker's report (runtime/workers.py): what it read is untrusted.
        return f"{EXTERNAL}:worker"
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


# The brackets a control token is built from, shown as HTML entities so the
# token is visible as text but no longer the token (angle brackets for
# <|im_start|> and Gemma/Llama <<SYS>>; square brackets for Llama [INST]).
_BRACKETS = {ord("<"): "&lt;", ord(">"): "&gt;", ord("["): "&#91;", ord("]"): "&#93;"}


def escape(text: str) -> str:
    """Content made safe to sit inside a fence: a look-alike of the fence
    tag or a control token is shown as text and cannot end the fence or pose
    as a model's control token."""
    text = _C0.sub("", text)
    text = _FENCE_TAG.sub(lambda m: "&lt;" + m.group(1) + "untrusted", text)
    return _CONTROL.sub(lambda m: m.group(0).translate(_BRACKETS), text)


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
# plan), or that the assistant itself used in a call it made before the
# taint arose (Taint.chose: the retry of a page it picked, not a page's
# link); a target from anywhere else -- an email's "forward to x@evil", a
# page's link, a log's path -- needs the user's tap. approval_needed() is the
# whole decision (plan 2.4 will fold it into one approval policy); the
# sessions turn a "needs approval" into the existing approval paths: a
# confirm card (core/pending_actions.py) or a Task Runtime approval.
#
# One relaxation, for where the browser goes (_BROWSE: browser_task's url
# and the web addresses written in its goal and steps, browser_control's
# url). When the conversation kept a whole record of the untrusted content
# it saw (Taint.seen), a web address the assistant picked itself runs
# unless the content put it there: its host, or a distinctive label of the
# host, appears in that content, or its path, query or fragment carries a
# word from the content or an opaque token (an id, a key) the user never
# said. A search engine's bare front page (_SEARCH_HOSTS) carries nothing
# and always runs. Everything else -- recipients of a send, paths, commands,
# amounts, typed text, repos -- keeps the strict rule above, and so does a
# web address once the record is not whole (content seen without its text,
# or more of it than the record keeps).

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


def _aimed(name: str, args: dict) -> list[tuple[str, str]]:
    """(argument, target) for each target of an irreversible call, from
    tools.KEY_ARGS. A tool that declares nothing (unknown) aims at
    everything it was given, under no argument name."""
    from . import tools

    spec = tools.KEY_ARGS.get(name)
    args = args if isinstance(args, dict) else {}
    if spec is None:
        return [("", text) for _, text in _leaves(args)]
    found: list[tuple[str, str]] = []
    for arg in spec:
        key = arg.rstrip("~")
        value = args.get(key)
        if value is None or value == "":
            continue
        if isinstance(value, (dict, list, tuple)):
            for inner, text in _leaves(value, key):
                if _TARGET_KEY.search(inner):
                    found.append((key, text))
                else:
                    found += [(key, t) for t in _EMBEDDED.findall(text)]
        elif arg.endswith("~"):
            found += [(key, t) for t in _EMBEDDED.findall(str(value))]
        else:
            found.append((key, str(value)))
    return found


def targets(name: str, args: dict) -> list[str]:
    """What an irreversible call aims at, from tools.KEY_ARGS. A tool that
    declares nothing (unknown) aims at everything it was given."""
    return [text for _, text in _aimed(name, args)]


def _home(text: str) -> str:
    import os

    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "~" else text


# Irreversible calls whose own effect is the risk, with no argument to point
# at: entering the saved password, posting an issue publicly. The target
# check below finds nothing to flag in them, so a tainted conversation would
# let them through. Once untrusted content is in the conversation, each needs
# the user's tap whatever its arguments.
_SELF_ACTING = {
    "submit_sudo_password": "the saved password would be entered after outside content entered this conversation",
    "terminal_sudo": "the saved password would be entered after outside content entered this conversation",
    "report_issue": "it would post publicly after outside content entered this conversation",
}


def _unsaid(aims: list[str], said: str) -> list[str]:
    heard = _tokens(_home(said))
    return [aim for aim in aims if aim and not _tokens(_home(aim)) <= heard]


def own(aim: str) -> str:
    """A target as Taint.chose keeps it: the same target, written the same
    way (a trailing slash or surrounding space aside), and nothing looser."""
    return _home(str(aim).strip()).rstrip("/")


# The arguments whose web addresses get the relaxed rule (the comment above
# _TARGET_KEY): where the assistant's own browser goes. browser_control's
# typed text, a send's recipients, delegate's url (a worker acts on it
# unseen) and every other target stay strict.
_BROWSE = {
    "browser_task": frozenset({"url", "goal", "steps"}),
    "browser_control": frozenset({"url"}),
}
# Search engines whose bare front page carries no data anywhere.
_SEARCH_HOSTS = frozenset(
    {"google.com", "duckduckgo.com", "html.duckduckgo.com", "bing.com"}
)
# Labels of a host that name no one (www, example.co.uk's "co").
_GENERIC_LABELS = frozenset({"co", "com", "org", "net", "gov", "edu", "ac", "www"})
_SCHEME = re.compile(r"^[a-z][a-z0-9+.-]*://", re.IGNORECASE)
_LOCAL_SUFFIXES = (".local", ".localhost", ".internal", ".lan", ".home.arpa")


class _Web(NamedTuple):
    host: str  # ASCII (punycode) form, without "www."
    names: frozenset[str]  # the host's ASCII and Unicode forms
    labels: frozenset[str]  # its distinctive labels, both forms
    rest: str  # path, parameters, query and fragment, decoded
    bare: bool  # nothing past the origin


def _idna(host: str) -> tuple[str, str]:
    """(ASCII, Unicode) forms of a host; the input itself where one fails."""
    try:
        uni = host.encode("ascii").decode("idna") if "xn--" in host else host
    except (UnicodeError, ValueError):
        uni = host
    try:
        asc = uni.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        asc = host
    return asc.lower(), uni.lower()


def _web(aim: str) -> _Web | None:
    """A public http(s) web address, normalized (case, scheme, "www.",
    trailing slash, punycode), or None: another scheme, credentials, an
    unusual port, an IP address or a local name stay strict."""
    text = aim.strip()
    if _SCHEME.match(text):
        if not text.lower().startswith(("http://", "https://")):
            return None
    else:
        text = "https://" + text
    try:
        parsed = urlparse(text)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").rstrip(".")
    if not host or parsed.username or parsed.password or port not in (None, 80, 443):
        return None
    try:
        ipaddress.ip_address(host.strip("[]"))
        return None
    except ValueError:
        pass
    asc, uni = _idna(host)
    asc, uni = asc.removeprefix("www."), uni.removeprefix("www.")
    if "." not in asc or asc.endswith(_LOCAL_SUFFIXES):
        return None
    labels = {
        label
        for form in (asc, uni)
        for label in form.split(".")[:-1]
        if len(label) >= 3 and label not in _GENERIC_LABELS
    }
    rest = ";".join(filter(None, (parsed.path.strip("/"), parsed.params)))
    rest = "?".join(filter(None, (rest, parsed.query)))
    if parsed.fragment:
        rest += "#" + parsed.fragment
    return _Web(
        asc, frozenset({asc, uni}), frozenset(labels), unquote_plus(rest), not rest
    )


def _opaque(token: str) -> bool:
    """A token nobody types by accident: a key, an id, an encoded blob."""
    return len(token) >= 20 or (
        len(token) >= 10
        and any(c.isdigit() for c in token)
        and any(c.isalpha() for c in token)
    )


class _Seen(NamedTuple):
    text: str  # decoded and lowercased
    tokens: frozenset[str]


# JSON's escapes of a character and of a slash, as a tool's JSON output has them.
_JSON_ESCAPE = re.compile(r"\\u([0-9a-fA-F]{4})|\\/")


def _plain(content: str) -> str:
    """Text to compare targets against: unfenced, entities, JSON escapes and
    percent escapes decoded, lowercased."""
    text = html.unescape(unfence(content))
    text = _JSON_ESCAPE.sub(
        lambda m: chr(int(m.group(1), 16)) if m.group(1) else "/", text
    )
    return unquote(_C0.sub("", text)).lower()


def _record(seen: str | None) -> _Seen | None:
    if seen is None:
        return None
    text = _plain(seen)
    return _Seen(text, frozenset(_tokens(text)))


def _in_content(aim: str, heard: set[str], seen: _Seen) -> bool:
    """Whether a target, or every word of it the user did not say, appears
    in the untrusted content seen."""
    written = _plain(_home(aim)).strip().rstrip("/")
    if len(written) >= 4 and written in seen.text:
        return True
    words = _tokens(_home(aim)) - heard
    return bool(words) and words <= seen.tokens


# Words too common to carry anything: frequent English and Hebrew words, and
# the vocabulary of browser goals and steps. A word in content counts as data
# only when it is distinctive (_distinctive).
_COMMON = frozenset(
    """
    about above after again against along already also although always among
    another answer answers around because before being below between both
    called could does doing during each either every first found from further
    great having here how into itself just known large later least less like
    little long made make many more most much must never next number often
    only other others over people perhaps place point possible quite rather
    really right same several should show since small something sometimes
    still such than that their them then there these they thing things think
    this those though three through today together under until upon used
    using very want well were what when where whether which while whole whose
    will with within without would year years your yours best better time
    times today tomorrow yesterday morning evening night early late during
    information details detail guide guides about overview summary general
    search searches searched searching result results page pages first
    content contents read reading open opening click clicking find finding
    look looking check checking verify verified visit website websites site
    sites link links article articles recommended recommendation
    recommendations source sources official answer question questions
    following follow scroll scrolling select selecting enter type button
    field form submit browse browser google bing duckduckgo query queries
    image images video videos news main home section sections list lists
    item items title titles text find identify identifying top related
    tab tabs window close back forward reload new another few several
    whether please thanks hello good tips tip ways steps step
    של את על עם זה זאת הוא היא הם הן אני אתה את אנחנו לא כן גם אבל או כי
    אם מה מי איך למה מתי איפה כל יותר פחות הרבה מאוד רק עוד כבר עכשיו היום
    מחר אתמול בוקר ערב לילה זמן טוב הכי חיפוש תוצאות תוצאה עמוד דף אתר
    קישור לחפש חפש תחפש לקרוא קרא תקרא לפתוח פתח תפתח מידע ראשון ראשונה
    ראשונים
    """.split()
)


def _stem(token: str) -> str:
    """A word without its common inflection, so "searched", "searching" and
    "searches" are one word, and so are Hebrew's prefixed forms."""
    if token and "\u0590" <= token[0] <= "\u05ff":
        if len(token) > 3 and token[0] in "הובלמשכ":
            token = token[1:]
        for suffix in ("ים", "ות"):
            if len(token) > 4 and token.endswith(suffix):
                return token[: -len(suffix)]
        return token
    for suffix in ("ings", "ing", "edly", "ed", "es", "ly", "s"):
        if len(token) - len(suffix) >= 3 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _distinctive(token: str) -> bool:
    """A word that could identify something: five letters or more, or with a
    digit, and not a common word (_COMMON) in any inflection."""
    if not (len(token) >= 5 or any(c.isdigit() for c in token)):
        return False
    return token not in _COMMON and _stem(token) not in _COMMON_STEMS


_COMMON_STEMS = frozenset(_stem(word) for word in _COMMON)


def _carried(
    text: str, heard: set[str], seen: _Seen | None, own_words: Collection[str] = ()
) -> list[str]:
    """Words of `text` that would carry data out: ones the user never said
    that are opaque (a key, an id), or that appear in the untrusted content
    and are distinctive (_distinctive), unless the user said them in any
    form or the assistant wrote them before content had them (`own_words`,
    Taint.own_words: stems)."""
    heard_stems = {_stem(h) for h in heard}
    return sorted(
        t
        for t in _tokens(text) - heard
        if _opaque(t)
        or (
            seen is not None
            and t in seen.tokens
            and _distinctive(t)
            and _stem(t) not in heard_stems
            and _stem(t) not in own_words
        )
    )


def _browse_problem(
    web: _Web, heard: set[str], seen: _Seen, own_words: Collection[str] = ()
) -> tuple[bool, list[str]] | None:
    """None to go to this web address; otherwise (whether the content put
    it there, the words it would carry out): its host or a distinctive label
    of it appears in the content, or its path, query or fragment carries
    data (_carried)."""
    if web.host not in _SEARCH_HOSTS:
        if any(name in seen.text for name in web.names):
            return True, []
        if any(label in seen.tokens and label not in heard for label in web.labels):
            return True, []
    words = _carried(web.rest, heard, seen, own_words)
    return (False, words) if words else None


def _free_text(name: str, args: dict) -> str:
    """The free text a browser call carries besides its address: what
    browser_task types and searches for is written in its goal and steps."""
    if name != "browser_task" or not isinstance(args, dict):
        return ""
    return "\n".join(text for _, text in _leaves([args.get("goal"), args.get("steps")]))


def _shown(aims: list[str]) -> str:
    return ", ".join(repr(m[:80]) for m in aims[:3])


def approval_needed(
    name: str,
    args: dict,
    *,
    tainted: bool,
    said: str,
    chosen: Collection[str] = (),
    seen: str | None = None,
    own_words: Collection[str] = (),
) -> str | None:
    """None to run the call; otherwise why it needs the user's tap. Only a
    tainted conversation or task is ever stopped, only for an irreversible
    call, and only when one of its targets is not in `said` (the user's own
    words and any plan they approved) and is not one the assistant itself
    used before the taint arose (`chosen`, Taint.chose: exactly that target,
    not a look-alike), or the call is self-acting. A web address the
    browser goes to (_BROWSE) is held back only when the untrusted content
    `seen` put it there or it would carry data out (_browse_problem); `seen`
    is Taint.seen, None when that record is not whole, and then the address
    is as strict as any target. `own_words` (Taint.own_words) are words the
    user or the assistant itself wrote first, never data. Unsure is a tap.

    The reason says a target came from content only when it appears in
    `seen`; otherwise it says the user did not state it."""
    from . import tools

    if not tainted or tools.effect_class(name) != tools.IRREVERSIBLE:
        return None
    if name in _SELF_ACTING:
        return _SELF_ACTING[name]
    heard = _tokens(_home(said))
    record = _record(seen)
    from_content: list[str] = []
    carrying: list[tuple[str, list[str]]] = []
    unstated: list[str] = []
    relaxed = False  # a web address ran on the relaxed rule
    for arg, aim in _aimed(name, args):
        if not aim or _tokens(_home(aim)) <= heard or own(aim) in chosen:
            continue
        web = _web(aim) if arg in _BROWSE.get(name, ()) else None
        if web is not None and web.bare and web.host in _SEARCH_HOSTS:
            relaxed = True  # a search engine's front page carries nothing
            continue
        if web is not None and record is not None:
            problem = _browse_problem(web, heard, record, own_words)
            if problem is None:
                relaxed = True
                continue
            if problem[0]:
                from_content.append(aim)
            else:
                carrying.append((aim, problem[1]))
        elif record is not None and _in_content(aim, heard, record):
            from_content.append(aim)
        else:
            unstated.append(aim)
    if relaxed and (
        words := _carried(_free_text(name, args), heard, record, own_words)
    ):
        # What it would search for or type on the page (the goal, the steps).
        carrying.append(("the goal and steps", words))
    reasons = []
    if from_content:
        reasons.append(
            f"{_shown(from_content)} came from content, not from the user's own words"
        )
    for aim, words in carrying[:2]:
        derived = [w for w in words if record is not None and w in record.tokens]
        what = ", ".join(repr(w[:40]) for w in (derived or words)[:3])
        source = "from outside content" if derived else "the user never said"
        shown = aim if aim == "the goal and steps" else repr(aim[:80])
        reasons.append(f"{shown} would carry data {source} ({what}) to the web")
    if unstated:
        reasons.append(f"{_shown(unstated)} wasn't stated by the user")
    return "; ".join(reasons) or None


def read_query_needed(args: dict, *, said: str) -> str | None:
    """Why a read whose arguments leave the machine (myapi_call's query)
    needs the user's tap in a tainted conversation, or None. A query value
    the user never said could carry data -- a canary, an id, an address --
    out to the service as a URL parameter, so it falls to a tap."""
    query = args.get("query") if isinstance(args, dict) else None
    if not query:
        return None
    missing = _unsaid([text for _key, text in _leaves(query)], said)
    if not missing:
        return None
    shown = ", ".join(repr(m[:80]) for m in missing[:3])
    return f"a myapi_call query built from content ({shown}) would leave the machine"


_CHOSEN_KEPT = 200  # targets of the assistant's own pre-taint calls a session keeps
_SEEN_KEPT = (
    200_000  # characters of untrusted content a session keeps to compare against
)
_SEEN_KEYS = 64  # recent pieces of content remembered, so one seen twice is kept once
_OWN_WORDS_KEPT = 5000  # stems of words the user or the assistant wrote first


class Taint:
    """One conversation's taint: whether untrusted content entered it, from
    where, what that content said, what the user said in it, and the targets
    the assistant itself aimed at before it was tainted."""

    def __init__(self) -> None:
        self.tainted = False
        self.sources: list[str] = []
        self.utterances = 0  # pieces of the user's own words heard so far
        self._joined, self._spaced = "", ""
        self.chosen: set[str] = set()
        self._seen = ""
        self._seen_whole = True  # every piece of untrusted content is in _seen
        self._seen_keys: list[str] = []
        self._seen_tokens: set[str] = set()
        # Stems of words the user said, and of words the assistant wrote in
        # its own calls before any content held them: never data from content.
        self.own_words: set[str] = set()

    def _own(self, tokens: Iterable[str]) -> None:
        for token in tokens:
            if len(self.own_words) >= _OWN_WORDS_KEPT:
                return
            self.own_words.add(_stem(token))

    def wrote(self, args: object) -> None:
        """The arguments of a call the assistant made (run or refused): its
        words that no content seen so far holds are its own. Opaque tokens
        are never counted, and once the record of content is not whole
        nothing more is."""
        if self.tainted and not self._seen_whole:
            return
        words = _tokens(" ".join(text for _, text in _leaves(args)))
        self._own(t for t in words if not _opaque(t) and t not in self._seen_tokens)

    def chose(self, aims: list[str]) -> None:
        """The targets of a call the assistant is dispatching. Kept only
        while nothing untrusted has entered the conversation (the call whose
        own result taints it included, since it is dispatched first): no
        content can have suggested them, so retrying the same call later is
        not acting on content. Once tainted, nothing more is added."""
        if self.tainted:
            return
        for aim in aims:
            if aim and len(self.chosen) < _CHOSEN_KEPT:
                self.chosen.add(own(aim))

    def mark(self, source: str, content: str | None = None) -> None:
        """Untrusted content from `source` entered the conversation.
        `content` is its text (fenced or not); without it the record of what
        was seen is no longer whole (Taint.seen)."""
        self.tainted = True
        if source and source not in self.sources:
            self.sources = (self.sources + [source])[-5:]
        if content is None:
            self._seen_whole = False
            return
        text = _C0.sub("", html.unescape(unfence(content)))
        key = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        if key in self._seen_keys:
            return  # the same result, marked by the guard and its dispatcher
        self._seen_keys = (self._seen_keys + [key])[-_SEEN_KEYS:]
        self._seen_tokens |= _tokens(_plain(text))
        joined = self._seen + "\n" + text
        if len(joined) > _SEEN_KEPT:
            self._seen_whole = False  # the oldest content no longer fits
            joined = joined[-_SEEN_KEPT:]
        self._seen = joined

    def seen(self) -> str | None:
        """All the untrusted content this conversation saw, or None once
        that record is not whole: then every target is checked strictly."""
        return self._seen if self._seen_whole else None

    def heard(self, text: str) -> None:
        """The user's words: a whole turn, or one fragment of one (live
        transcription arrives in pieces, so they are kept both joined and
        apart)."""
        if text:
            self.utterances += 1
            self._joined = (self._joined + text)[-_SAID_KEPT:]
            self._spaced = (self._spaced + " " + text)[-_SAID_KEPT:]
            self._own(_tokens(text))

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
