"""When a failed call is tried again, and how long to wait first.

One policy for every automatic retry (agenda steps, worker-model and Jev
calls, MyApi reads), instead of a loop and a sleep at each site:

    result = retry.run(lambda: client.get(...), effect_class=retry.READ,
                       policy=retry.Policy(attempts=3, base=1.0), name="myapi read")

A failure is retried only when both hold:
- it is retriable: the connection failed, the service was rate-limited
  (429) or failed on its side (5xx), the call timed out, or a tool said it
  failed without saying why (`failed`); never a client error (4xx, bad
  credentials), a cancel, or a call core/effects.py did not dispatch
  because it may already have happened;
- a repeat is safe: the tool's effect class (execution/tools.py) is `read`
  or `reversible` (a repeat leaves things as one call does). An
  `irreversible` call is retried only with an idempotency key, and only for
  failures where core/effects.py would not send it again blindly: it never
  went out (connection refused, 429), or it may have and the journal now
  answers `uncertain` instead of sending it again (transport, timeout). A
  5xx or a tool's own failure of an irreversible call is never retried: the
  journal records `failed`, and a repeat would send it again.

    kind          read  reversible  irreversible  irreversible+idem key
    not_sent       yes     yes          no              yes
    rate_limited   yes     yes          no              yes
    transport      yes     yes          no              yes
    timeout        yes     yes          no              yes
    server (5xx)   yes     yes          no              no
    failed         yes     yes          no              no
    permanent      no      no           no              no
    uncertain      no      no           no              no
    cancelled      no      no           no              no

The wait before retry n (0-based) is base * factor**n, at most `cap`, with
+-`jitter` of itself added at random; a Retry-After the service sent is
waited instead when it is longer (and the retry is given up when it is
longer than `max_retry_after`). Waits share the caller's deadline: a retry
that could not start before it is not made. The current CancelToken
(core/cancel.py) ends a wait at once and no retry follows.
"""

from __future__ import annotations

from dataclasses import dataclass
import email.utils
import logging
import random
import re
import socket
import subprocess
import time
from typing import Any, Callable, TypeVar

from . import cancel

log = logging.getLogger("omarchy_ai.core.retry")

T = TypeVar("T")

# execution/tools.py's effect classes (kept as text here: core/ does not
# import the tool registry).
READ, REVERSIBLE, IRREVERSIBLE = "read", "reversible", "irreversible"

# Failure kinds.
NOT_SENT = "not_sent"  # the connection failed before the request left
RATE_LIMITED = "rate_limited"  # 429: refused, not processed
TRANSPORT = "transport"  # the connection broke; it may have reached the service
TIMEOUT = "timeout"
SERVER = "server"  # 5xx: the service failed on its side
FAILED = "failed"  # the tool said it failed, without a classifiable reason
PERMANENT = "permanent"  # 4xx, bad credentials: the same call fails again
UNCERTAIN = "uncertain"  # core/effects.py did not dispatch it (may have happened)
CANCELLED = "cancelled"

_REPEAT_SAFE = frozenset({NOT_SENT, RATE_LIMITED, TRANSPORT, TIMEOUT, SERVER, FAILED})
# Kinds after which core/effects.py does not re-send a keyed call that may
# have happened: it never went out, or the journal says `uncertain`.
_IDEMPOTENT_SAFE = frozenset({NOT_SENT, RATE_LIMITED, TRANSPORT, TIMEOUT})


@dataclass(frozen=True)
class Failure:
    kind: str
    retry_after: float | None = None  # seconds the service asked us to wait
    detail: str = ""


def should_retry(kind: str, effect_class: str, *, idempotent: bool = False) -> bool:
    """The policy table (module docstring). An unknown effect class counts as
    irreversible."""
    if effect_class in (READ, REVERSIBLE):
        return kind in _REPEAT_SAFE
    return idempotent and kind in _IDEMPOTENT_SAFE


# ------------------------------------------------------------- classify

_RATE = re.compile(r"\b429\b|too many requests|rate.?limit", re.I)
_SERVER = re.compile(
    r"HTTP \(?5\d\d\b|\bbad gateway\b|unavailable|overloaded|internal server error",
    re.I,
)
_TIMEOUT = re.compile(r"timed out|\btimeout\b|did not answer within", re.I)
_TRANSPORT = re.compile(
    r"could not reach|connection (?:refused|reset|aborted|failed|error)|network is unreachable|"
    r"name or service not known|temporary failure in name resolution|broken pipe|"
    r"remote end closed|connection to \S+ failed",
    re.I,
)
_PERMANENT = re.compile(r"HTTP \(?4\d\d\b|\bunauthori[sz]ed\b|\bforbidden\b", re.I)


def classify_text(text: str) -> Failure:
    """A failure known only by its message."""
    detail = str(text)[:200]
    for pattern, kind in (
        (_RATE, RATE_LIMITED),
        (_SERVER, SERVER),
        (_TIMEOUT, TIMEOUT),
        (_TRANSPORT, TRANSPORT),
        (_PERMANENT, PERMANENT),
    ):
        if pattern.search(detail):
            return Failure(kind, detail=detail)
    return Failure(FAILED, detail=detail)


def _status_kind(status: int) -> str:
    if status == 429:
        return RATE_LIMITED
    if 500 <= status <= 599:
        return SERVER
    if status == 408:
        return TIMEOUT
    return PERMANENT


def retry_after_seconds(value: Any, now: float | None = None) -> float | None:
    """A Retry-After header value (seconds or an HTTP date) in seconds."""
    if value is None:
        return None
    text = str(value).strip()
    try:
        return max(0.0, float(text))
    except ValueError:
        pass
    try:
        when = email.utils.parsedate_to_datetime(text)
    except (TypeError, ValueError, IndexError):
        return None
    if when is None:
        return None
    return max(0.0, when.timestamp() - (time.time() if now is None else now))


def classify(error: Any) -> Failure:
    """What kind of failure `error` is: an exception, a message, or a result
    with `ok`/`message` (an ActionResult, a runner ToolResult)."""
    if isinstance(error, str):
        return classify_text(error)
    if isinstance(error, cancel.Cancelled):
        return Failure(CANCELLED, detail=error.reason)
    if isinstance(error, BaseException):
        return _classify_exception(error)
    error_class = getattr(error, "error_class", None)
    if error_class in ("timeout", "worker_lost", "cancelled"):
        return Failure(
            {"timeout": TIMEOUT, "worker_lost": TRANSPORT, "cancelled": CANCELLED}[
                error_class
            ]
        )
    if error_class in ("uncertain", "in_progress"):
        return Failure(UNCERTAIN)
    if error_class == "crashed":
        return Failure(PERMANENT)
    return classify_text(str(getattr(error, "message", error)))


def _classify_exception(error: BaseException) -> Failure:
    detail = str(error)[:200]
    if type(error).__name__ == "Uncertain":  # core/effects.py, not imported here
        return Failure(UNCERTAIN, detail=detail)
    if getattr(error, "reconnect_needed", False):
        return Failure(PERMANENT, detail=detail)
    status = getattr(error, "status", None)
    if not isinstance(status, int):
        status = getattr(error, "code", None)  # urllib.error.HTTPError
    if isinstance(status, int) and 100 <= status <= 599:
        after = getattr(error, "retry_after", None)
        headers = getattr(error, "headers", None)
        if after is None and headers is not None:
            after = retry_after_seconds(headers.get("Retry-After"))
        return Failure(_status_kind(status), after, detail)
    if isinstance(error, (TimeoutError, socket.timeout, subprocess.TimeoutExpired)):
        return Failure(TIMEOUT, detail=detail)
    if isinstance(error, (ConnectionRefusedError, socket.gaierror)):
        return Failure(NOT_SENT, detail=detail)
    reason = getattr(error, "reason", None)  # urllib.error.URLError
    if isinstance(reason, (ConnectionRefusedError, socket.gaierror)):
        return Failure(NOT_SENT, detail=detail)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return Failure(TIMEOUT, detail=detail)
    if getattr(error, "transport", False):  # myapi.MyApiError
        if not getattr(error, "maybe_sent", True):
            return Failure(NOT_SENT, detail=detail)
        return Failure(TIMEOUT if _TIMEOUT.search(detail) else TRANSPORT, detail=detail)
    if isinstance(error, OSError):
        return Failure(TRANSPORT, detail=detail)
    return classify_text(detail)


# --------------------------------------------------------------- policy


@dataclass(frozen=True)
class Policy:
    attempts: int = 3  # tries in all, the first included
    base: float = 0.5
    factor: float = 2.0
    cap: float = 30.0
    jitter: float = 0.2  # +- this fraction of each wait, at random
    max_retry_after: float = 60.0

    def delay(self, retry: int, failure: Failure | None = None) -> float:
        """The wait before retry number `retry` (0 = the first retry)."""
        wait = min(self.cap, self.base * self.factor**retry)
        if self.jitter:
            wait *= 1 + random.uniform(-self.jitter, self.jitter)
        wait = max(0.0, wait)
        if failure is not None and failure.retry_after is not None:
            wait = max(wait, failure.retry_after)
        return wait

    def plan(
        self,
        retry: int,
        failure: Failure | None = None,
        *,
        deadline: float | None = None,
        token: cancel.CancelToken | None = None,
    ) -> float | None:
        """The wait before retry number `retry`, or None when the retry must
        not be made: the call was cancelled, the service asked for a longer
        wait than max_retry_after, or it could not start before `deadline`
        (a time.monotonic() value). `token` defaults to the current call's."""
        token = token if token is not None else cancel.current()
        if token is not None and token.cancelled:
            return None
        if (
            failure is not None
            and failure.retry_after is not None
            and failure.retry_after > self.max_retry_after
        ):
            return None
        pause = self.delay(retry, failure)
        if deadline is not None and time.monotonic() + pause >= deadline:
            return None
        return pause

    def wait(
        self,
        retry: int,
        failure: Failure | None = None,
        *,
        deadline: float | None = None,
        token: cancel.CancelToken | None = None,
    ) -> bool:
        """Wait before retry number `retry`; False (at once) when `plan` says
        no, or when the call is cancelled while waiting."""
        token = token if token is not None else cancel.current()
        pause = self.plan(retry, failure, deadline=deadline, token=token)
        return pause is not None and _sleep(pause, token)


def _sleep(pause: float, token: cancel.CancelToken | None) -> bool:
    """Sleep `pause` seconds; False when cancelled meanwhile."""
    if token is not None:
        return not token.wait(pause)
    time.sleep(pause)
    return True


def run(
    call: Callable[[], T],
    *,
    effect_class: str,
    idempotent: bool = False,
    policy: Policy = Policy(),
    deadline: float | None = None,
    token: cancel.CancelToken | None = None,
    check: Callable[[T], Failure | None] | None = None,
    name: str = "call",
) -> T:
    """Call `call()` until it succeeds or the policy says stop.

    A raised exception is classified (`classify`); `check` turns a returned
    result into a Failure, or None when it succeeded. What the last attempt
    did is what the caller gets: its result, or its exception re-raised."""
    retry = 0
    while True:
        try:
            result = call()
        except Exception as exc:
            if not _again(
                policy,
                retry,
                classify(exc),
                effect_class,
                idempotent,
                deadline,
                token,
                name,
            ):
                raise
        else:
            outcome = check(result) if check is not None else None
            if outcome is None or not _again(
                policy, retry, outcome, effect_class, idempotent, deadline, token, name
            ):
                return result
        retry += 1


def _again(
    policy: Policy,
    retry: int,
    failure: Failure,
    effect_class: str,
    idempotent: bool,
    deadline: float | None,
    token: cancel.CancelToken | None,
    name: str,
) -> bool:
    if retry + 1 >= policy.attempts:
        return False
    if not should_retry(failure.kind, effect_class, idempotent=idempotent):
        return False
    token = token if token is not None else cancel.current()
    pause = policy.plan(retry, failure, deadline=deadline, token=token)
    if pause is None:
        return False
    log.warning(
        "%s failed (%s: %s); retrying in %.1fs",
        name,
        failure.kind,
        failure.detail[:160],
        pause,
    )
    return _sleep(pause, token)
