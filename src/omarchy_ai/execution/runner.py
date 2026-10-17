"""Run each tool call with a deadline, cancellable, and say how it ended.

    runner = default_runner()
    result = runner.run("list_windows", {})                        # any thread
    result = await runner.arun(name, args, wrap=guard.run, owner=self)

Every call gets the deadline, resource and effect class its tool declares in
execution/tools.py, and a CancelToken (core/cancel.py) that is current in
the thread running it. A call ends in one of four ways, all as a ToolResult:
- it returns (ok or not): its own result -- fenced as untrusted content
  when its tool's provenance says so (execution/provenance.py);
- its deadline passes: "<tool> timed out after N s; nothing was confirmed";
- it is cancelled (the user's "stop", a hangup, a task cancel): likewise;
- it raises: "<tool> crashed".
On a deadline or cancel the caller is answered at once. What the call
registered on its token then runs: its tool's own canceller (CANCELLERS:
browser_task -> browser_jev.cancel_browser_tasks, desktop_task ->
desktop_jev.cancel_desktop_tasks), the effect journal marks whatever it
dispatched `uncertain` (core/effects.py), and a worker process is killed.

Two modes:
- in-process (most tools): the call runs on its own daemon thread, where the
  daemon's state is (the browser session, the assistant's terminals, focus
  tracking, Jev's circuit breaker, calls in flight in the effect journal).
  Adapters may stop early through the token; one that ignores it is
  abandoned at the deadline -- its thread finishes on its own, and its late
  result is dropped.
- worker (tools.WORKER_TOOLS: network reads that keep no daemon state): the
  call runs in a long-lived worker process (execution/worker_main.py) in its
  own process group. A deadline or cancel sends the group SIGTERM, then
  SIGKILL after KILL_GRACE seconds, so nothing it started outlives it. Only
  the daemon starts workers (use_workers()); elsewhere, tests included,
  these tools run in-process too.

Resources (one per session) keep mutating calls on the same resource in the
order they were made, and let reads run alongside each other.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
from dataclasses import dataclass
import importlib
import json
import logging
import os
import queue
import select
import signal
import subprocess
import sys
import threading
import time
from typing import Any, AsyncIterator, Callable

from ..core import logs, retry
from ..core.cancel import CURRENT, Cancelled, CancelToken, checkpoint, current
from . import provenance, tools

__all__ = [
    "CANCELLERS",
    "Cancelled",
    "CancelToken",
    "Resources",
    "ToolResult",
    "ToolRunner",
    "ToolSpec",
    "WorkerPool",
    "checkpoint",
    "current",
    "default_runner",
    "spec",
    "use_workers",
]

log = logging.getLogger("omarchy_ai.execution.runner")

KILL_GRACE = 2.0

# error_class values
FAILED, TIMEOUT, CANCELLED, CRASHED, WORKER_LOST = (
    "failed",
    "timeout",
    "cancelled",
    "crashed",
    "worker_lost",
)

# A tool's own way to stop work in progress, run when its call is cancelled.
CANCELLERS = {
    "browser_task": "omarchy_ai.execution.browser_jev:cancel_browser_tasks",
    "desktop_task": "omarchy_ai.execution.desktop_jev:cancel_desktop_tasks",
}

Execute = Callable[[str, dict], Any]
Wrap = Callable[[Execute, str, dict], Any]


@dataclass(frozen=True)
class ToolResult:
    """How one call ended. `ok` and `message` are what the model is told,
    as the tool returned them when it returned; a result with untrusted
    provenance is fenced (execution/provenance.py), the one place every
    session's tool results pass before they reach a model."""

    ok: bool
    message: str
    error_class: str | None = None
    retriable: bool = False
    effect_id: str | None = None
    duration: float = 0.0
    provenance: str = ""
    untrusted: bool = False


@dataclass(frozen=True)
class ToolSpec:
    name: str
    effect_class: str
    deadline: float
    resource: str
    worker: bool

    @property
    def reads(self) -> bool:
        return self.effect_class == tools.READ


def spec(name: str) -> ToolSpec:
    return ToolSpec(
        name,
        tools.effect_class(name),
        tools.deadline(name),
        tools.resource(name),
        name in tools.WORKER_TOOLS,
    )


def _run_canceller(name: str) -> None:
    target = CANCELLERS.get(name)
    if not target:
        return
    module, _, attr = target.partition(":")
    getattr(importlib.import_module(module), attr)()


def _seconds(value: float) -> str:
    return f"{value:.0f}" if value >= 1 else f"{value:.1f}"


def _stopped_message(
    spec: ToolSpec, error_class: str, reason: str, deadline: float
) -> str:
    if error_class == TIMEOUT:
        text = f"{spec.name} timed out after {_seconds(deadline)} s; nothing was confirmed."
    elif error_class == WORKER_LOST:
        text = f"{spec.name} failed: its worker process ended unexpectedly; nothing was confirmed."
    else:
        text = f"{spec.name} was stopped ({reason}); nothing was confirmed."
    if not spec.reads:
        text += " It may have partly happened: check before doing it again."
    return text


def _action_result(ok: bool, message: str) -> Any:
    from .actions.base import ActionResult

    return ActionResult(ok, message)


class WorkerLost(Exception):
    """A worker process ended (or broke the protocol) during a call."""


class _Call:
    """One call in flight: whether it finished or was stopped, decided once."""

    def __init__(
        self,
        spec: ToolSpec,
        owner: object,
        deadline: float,
        tag: object = None,
        args: dict | None = None,
    ) -> None:
        self.spec = spec
        self.args = args if isinstance(args, dict) else {}
        self.owner = owner
        self.tag = tag
        # The model's call id when it has one: the log correlation ID (core/logs.py).
        self.tool_call_id = str(tag) if tag else logs.new_id()
        self.deadline = deadline
        self.token = CancelToken()
        self.started = time.monotonic()
        self._lock = threading.Lock()
        self._settled = threading.Event()
        self._listeners: list[Callable[[], None]] = []
        self.finished = False
        self.value: Any = None
        self.error: BaseException | None = None
        self.stop_class = ""

    def listen(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if not self._settled.is_set():
                self._listeners.append(callback)
                return
        callback()

    def _settle(self) -> None:
        self._settled.set()
        listeners, self._listeners = self._listeners, []
        for callback in listeners:
            callback()

    def complete(self, value: Any, error: BaseException | None) -> None:
        with self._lock:
            if self._settled.is_set():
                log.info(
                    "tool %s finished after it was stopped; result dropped",
                    self.spec.name,
                )
                return
            self.finished, self.value, self.error = True, value, error
            self._settle()

    def stop(self, error_class: str, reason: str) -> bool:
        """Stop the call unless it already finished; returns whether it did."""
        with self._lock:
            if self._settled.is_set():
                return False
            self.stop_class = error_class
            self._settle()
            # Inside the lock: complete() cannot slip in between, and the
            # effect journal sees the cancel before any later confirmation.
            self.token.cancel(reason)
        return True

    def wait(self, timeout: float) -> bool:
        return self._settled.wait(timeout)


class _Threads:
    """Daemon threads for calls, reused when idle. Daemon threads, not a
    ThreadPoolExecutor: an abandoned call's thread must never hold up the
    daemon's exit, nor take a slot a later call waits for (an idle thread is
    always free; a new one starts when none is)."""

    IDLE_SECONDS = 60.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._jobs: queue.SimpleQueue[tuple[contextvars.Context, Callable[[], None]]]
        self._jobs = queue.SimpleQueue()
        self._idle = 0

    def submit(self, context: contextvars.Context, job: Callable[[], None]) -> None:
        with self._lock:
            if self._idle:
                self._idle -= 1
                self._jobs.put((context, job))
                return
        threading.Thread(
            target=self._serve, args=(context, job), name="tool-call", daemon=True
        ).start()

    def _serve(self, context: contextvars.Context, job: Callable[[], None]) -> None:
        while True:
            context.run(job)
            with self._lock:
                self._idle += 1
            try:
                context, job = self._jobs.get(timeout=self.IDLE_SECONDS)
            except queue.Empty:
                with self._lock:
                    try:  # submitted while timing out: reserved for this thread
                        context, job = self._jobs.get_nowait()
                    except queue.Empty:
                        self._idle -= 1
                        return


_threads = _Threads()


class ToolRunner:
    def __init__(self, pool: WorkerPool | None = None) -> None:
        self.pool = pool
        self._lock = threading.Lock()
        self._running: set[_Call] = set()

    # ---------------------------------------------------------------- calls

    def run(
        self,
        name: str,
        args: dict,
        *,
        execute: Execute | None = None,
        wrap: Wrap | None = None,
        owner: object = None,
        deadline: float | None = None,
        tag: object = None,
    ) -> ToolResult:
        """Run one call and block until it ends (at most its deadline)."""
        call = self._start(name, args, execute, wrap, owner, deadline, tag)
        if not call.wait(call.deadline):
            call.stop(TIMEOUT, "timeout")
        return self._result(call)

    async def arun(
        self,
        name: str,
        args: dict,
        *,
        execute: Execute | None = None,
        wrap: Wrap | None = None,
        owner: object = None,
        deadline: float | None = None,
        tag: object = None,
    ) -> ToolResult:
        """Run one call off the event loop; cancelling the awaiting task
        cancels the call."""
        loop = asyncio.get_running_loop()
        settled: asyncio.Future[None] = loop.create_future()

        def set_settled() -> None:
            if not settled.done():
                settled.set_result(None)

        def wake() -> None:
            loop.call_soon_threadsafe(set_settled)

        call = self._start(name, args, execute, wrap, owner, deadline, tag)
        call.listen(wake)
        try:
            await asyncio.wait({settled}, timeout=call.deadline)
        except asyncio.CancelledError:
            call.stop(CANCELLED, "cancelled")
            raise
        if not call.finished:
            call.stop(TIMEOUT, "timeout")
        return self._result(call)

    def cancel(
        self,
        *,
        owner: object = None,
        names: set[str] | frozenset[str] | None = None,
        tags: set | frozenset | None = None,
        reason: str = "stop",
    ) -> int:
        """Stop the running calls of `owner` (all owners when None), only
        those of the tools in `names` / with the `tags` (e.g. call ids) when
        given. Returns how many."""
        with self._lock:
            calls = [
                c
                for c in self._running
                if (owner is None or c.owner == owner)
                and (names is None or c.spec.name in names)
                and (tags is None or c.tag in tags)
            ]
        return sum(call.stop(CANCELLED, reason) for call in calls)

    def running(self, owner: object = None) -> list[str]:
        with self._lock:
            return [
                c.spec.name for c in self._running if owner is None or c.owner == owner
            ]

    # ------------------------------------------------------------- internals

    def _start(
        self,
        name: str,
        args: dict,
        execute: Execute | None,
        wrap: Wrap | None,
        owner: object,
        deadline: float | None,
        tag: object = None,
    ) -> _Call:
        tool = spec(name)
        call = _Call(
            tool, owner, tool.deadline if deadline is None else deadline, tag, args
        )
        token = call.token
        token.on_cancel(lambda: _run_canceller(name))
        routed = self._route(execute, token, call)

        def body() -> None:
            CURRENT.set(token)
            try:
                value = wrap(routed, name, args) if wrap else routed(name, args)
            except BaseException as exc:  # noqa: BLE001 -- reported as the call's outcome
                call.complete(None, exc)
            else:
                call.complete(value, None)

        with self._lock:
            self._running.add(call)
        context = contextvars.copy_context()
        context.run(logs.set_ids, tool_call_id=call.tool_call_id)
        _threads.submit(context, body)
        return call

    def _route(
        self, execute: Execute | None, token: CancelToken, call: _Call
    ) -> Execute:
        def in_process(name: str, args: dict) -> Any:
            if execute is not None:
                return execute(name, args)
            from .actions import run_action

            return run_action(name, args)

        pool = self.pool
        if pool is None:
            return in_process

        def routed(name: str, args: dict) -> Any:
            if name not in tools.WORKER_TOOLS:
                return in_process(name, args)
            from .actions import myapi_refused

            refused = myapi_refused(name)
            if refused is not None:
                return refused
            try:
                reply = pool.call(
                    name, args, token=token, timeout=call.deadline + KILL_GRACE
                )
            except WorkerUnavailable:
                log.warning("no tool worker available; running %s in-process", name)
                return in_process(name, args)
            return _action_result(
                bool(reply.get("ok")), str(reply.get("message") or "")
            )

        return routed

    def _result(self, call: _Call) -> ToolResult:
        result = self._outcome(call)
        logs.tool_result(
            call.spec.name,
            ok=result.ok,
            error_class=result.error_class,
            duration=result.duration,
            tool_call_id=call.tool_call_id,
            effect_id=result.effect_id,
            effect_class=call.spec.effect_class,
            deadline_s=call.deadline,
            worker=call.spec.worker and self.pool is not None,
            retriable=result.retriable,
        )
        return result

    def _outcome(self, call: _Call) -> ToolResult:
        with self._lock:
            self._running.discard(call)
        tool, token = call.spec, call.token
        duration = time.monotonic() - call.started
        effect_id = token.effect_ids[-1] if token.effect_ids else None
        if not call.finished or isinstance(call.error, Cancelled):
            error_class = call.stop_class or CANCELLED
            reason = token.reason or (
                call.error.reason if isinstance(call.error, Cancelled) else "cancelled"
            )
            log.warning("tool %s stopped after %.1fs (%s)", tool.name, duration, reason)
            return ToolResult(
                False,
                _stopped_message(tool, error_class, reason, call.deadline),
                error_class,
                retry.should_retry(
                    retry.TIMEOUT if error_class == TIMEOUT else retry.CANCELLED,
                    tool.effect_class,
                ),
                effect_id,
                duration,
            )
        if isinstance(call.error, WorkerLost):
            return ToolResult(
                False,
                _stopped_message(tool, WORKER_LOST, "", call.deadline),
                WORKER_LOST,
                retry.should_retry(retry.TRANSPORT, tool.effect_class),
                effect_id,
                duration,
            )
        if call.error is not None:
            log.error(
                "tool %s raised",
                tool.name,
                exc_info=(type(call.error), call.error, call.error.__traceback__),
            )
            return ToolResult(
                False, f"{tool.name} crashed", CRASHED, False, effect_id, duration
            )
        value = call.value
        ok, message = (
            bool(getattr(value, "ok", False)),
            str(getattr(value, "message", "")),
        )
        outcome: str | None = None if ok else FAILED
        unsure = None if ok else _not_dispatched(message)
        if unsure is not None:
            outcome, effect_id = unsure
        source = provenance.of(tool.name, call.args)
        untrusted = (
            provenance.untrusted(source)
            and bool(message)
            and unsure is None
            and not getattr(value, "harness", False)
        )
        if untrusted:
            message = provenance.fence(message, source)
        return ToolResult(
            ok, message, outcome, False, effect_id, duration, source, untrusted
        )


def _not_dispatched(message: str) -> tuple[str, str] | None:
    """(state, effect_id) when core/effects.py did not dispatch the call."""
    if not message.startswith("{"):
        return None
    try:
        payload = json.loads(message)
    except ValueError:
        return None
    if isinstance(payload, dict) and payload.get("effect_id") and payload.get("status"):
        return str(payload["status"]), str(payload["effect_id"])
    return None


# ------------------------------------------------------------------ workers


class WorkerUnavailable(Exception):
    """No worker process could be started."""


class _Worker:
    """One worker process, in its own process group, serving one call at a time."""

    def __init__(self, command: list[str]) -> None:
        level = logging.getLevelName(logging.getLogger().getEffectiveLevel())
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            start_new_session=True,
            bufsize=0,
            env={
                **os.environ,
                "OMARCHY_WORKER_LOG_LEVEL": str(level),
                **logs.worker_env(),
            },
        )
        self._buffer = b""

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None

    def send(self, message: dict) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message, ensure_ascii=False).encode() + b"\n")

    def receive(self, timeout: float) -> dict | None:
        """The next reply, None at the timeout; raises WorkerLost on EOF."""
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        until = time.monotonic() + timeout
        while b"\n" not in self._buffer:
            left = until - time.monotonic()
            if left <= 0:
                return None
            ready, _, _ = select.select([fd], [], [], left)
            if not ready:
                continue
            chunk = os.read(fd, 1 << 16)
            if not chunk:
                raise WorkerLost("worker closed its output")
            self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        try:
            reply = json.loads(line)
        except ValueError as exc:
            raise WorkerLost("unreadable reply from worker") from exc
        if not isinstance(reply, dict):
            raise WorkerLost("unreadable reply from worker")
        return reply

    def kill(self, grace: float = KILL_GRACE, *, close: bool = True) -> None:
        """SIGTERM to the whole group, SIGKILL to what is left after `grace`.
        `close=False` leaves the pipes to the thread still reading them."""
        pgid = self.proc.pid  # start_new_session: the group is the worker's pid
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGTERM)
        try:
            self.proc.wait(grace)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            self.proc.wait(grace)
        if close:
            self.close()

    def close(self) -> None:
        for stream in (self.proc.stdin, self.proc.stdout):
            with contextlib.suppress(OSError):
                if stream is not None:
                    stream.close()


class WorkerPool:
    """Long-lived worker processes, started when first needed (or by
    prewarm()) so a call does not pay for Python start-up and imports."""

    def __init__(
        self,
        command: list[str] | None = None,
        *,
        idle: int = 1,
        limit: int = 4,
        grace: float = KILL_GRACE,
    ) -> None:
        self.command = command or [
            sys.executable,
            "-m",
            "omarchy_ai.execution.worker_main",
        ]
        self.idle = idle
        self.limit = limit
        self.grace = grace
        self._lock = threading.Lock()
        self._free: list[_Worker] = []
        self._busy: set[_Worker] = set()

    def prewarm(self) -> None:
        """Start idle workers in the background, up to `idle`."""
        threading.Thread(
            target=self._fill, name="tool-worker-prewarm", daemon=True
        ).start()

    def _fill(self) -> None:
        with self._lock:
            missing = self.idle - len(self._free)
        for _ in range(max(0, missing)):
            try:
                worker = _Worker(self.command)
            except OSError:
                log.warning("could not start a tool worker", exc_info=True)
                return
            with self._lock:
                self._free.append(worker)

    def _take(self) -> _Worker:
        with self._lock:
            while self._free:
                worker = self._free.pop()
                if worker.alive:
                    self._busy.add(worker)
                    return worker
            if len(self._busy) >= self.limit:
                raise WorkerUnavailable(f"all {self.limit} tool workers are busy")
        try:
            worker = _Worker(self.command)
        except OSError as exc:
            raise WorkerUnavailable(str(exc)) from exc
        with self._lock:
            self._busy.add(worker)
        return worker

    def _give_back(self, worker: _Worker) -> None:
        with self._lock:
            self._busy.discard(worker)
            if worker.alive and len(self._free) < self.idle:
                self._free.append(worker)
                return
        threading.Thread(target=worker.kill, args=(self.grace,), daemon=True).start()

    def _drop(self, worker: _Worker, close: bool = True) -> None:
        with self._lock:
            self._busy.discard(worker)
        worker.kill(self.grace, close=close)

    def call(
        self,
        name: str,
        args: dict,
        *,
        token: CancelToken | None = None,
        timeout: float,
    ) -> dict:
        """{"ok", "message"} from a worker. A cancel of `token` kills the
        worker's process group (raising Cancelled here); so does `timeout`."""
        worker = self._take()
        state = {"done": False}
        guard = threading.Lock()

        def kill() -> None:
            with guard:
                if state["done"]:
                    return
                state["done"] = True
            # The calling thread may be reading the pipe: it closes it.
            threading.Thread(
                target=self._drop,
                args=(worker, False),
                name="tool-worker-kill",
                daemon=True,
            ).start()

        if token is not None:
            token.on_cancel(kill)
        from ..core import conversations, effects

        request = {
            "name": name,
            "args": args,
            "origin": effects.ORIGIN.get(),
            "conversation": conversations.CURRENT.get(),
            "log": logs.current(),  # the call's IDs, for the worker's log lines
        }
        try:
            worker.send(request)
            reply = worker.receive(timeout)
        except (OSError, WorkerLost) as exc:
            kill()
            worker.close()
            if token is not None and token.cancelled:
                raise Cancelled(token.reason) from exc
            raise WorkerLost(str(exc)) from exc
        with guard:
            mine = not state["done"]
            state["done"] = True
        if reply is None or not mine:
            # Its deadline passed, or the call was cancelled as it answered.
            if mine:
                self._drop(worker)
            else:
                worker.close()
            raise Cancelled(
                token.reason if token is not None and token.cancelled else "timeout"
            )
        self._give_back(worker)
        return reply

    def close(self) -> None:
        with self._lock:
            workers = list(self._free) + list(self._busy)
            self._free, self._busy = [], set()
        for worker in workers:
            worker.kill(self.grace)


# --------------------------------------------------------------- resources


class _Queue:
    def __init__(self) -> None:
        self.writer: asyncio.Future | None = None
        self.readers: list[asyncio.Future] = []


class Resources:
    """Per-session ordering of calls by resource (asyncio).

    A mutating call waits for every call made before it on its resource; a
    read waits only for the mutating call made last before it. So mutations
    on one resource run one at a time in the order made, reads run side by
    side, and no read overtakes a mutation made before it (type_text,
    press_key Return, read_tile_log in one batch reads after the Return)."""

    def __init__(self) -> None:
        self._queues: dict[str, _Queue] = {}

    @contextlib.asynccontextmanager
    async def hold(self, resource: str, *, exclusive: bool) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        queue = self._queues.setdefault(resource, _Queue())
        mine = loop.create_future()
        if exclusive:
            before = [f for f in (queue.writer, *queue.readers) if f is not None]
            queue.writer, queue.readers = mine, []
        else:
            before = [queue.writer] if queue.writer is not None else []
            queue.readers = [f for f in queue.readers if not f.done()] + [mine]
        pending = [f for f in before if not f.done()]
        try:
            if pending:
                await asyncio.wait(pending)
        except asyncio.CancelledError:
            # Those after this call still wait for those before it.
            _release_after(pending, mine)
            raise
        try:
            yield
        finally:
            if not mine.done():
                mine.set_result(None)


def _release_after(before: list[asyncio.Future], mine: asyncio.Future) -> None:
    async def release() -> None:
        await asyncio.wait(before)
        if not mine.done():
            mine.set_result(None)

    task = asyncio.ensure_future(release())
    _releasers.add(task)
    task.add_done_callback(_releasers.discard)


_releasers: set[asyncio.Future] = set()


# ------------------------------------------------------------------ default

_default: ToolRunner | None = None
_default_lock = threading.Lock()


def default_runner() -> ToolRunner:
    """The process's runner (in-process only until use_workers())."""
    global _default
    with _default_lock:
        if _default is None:
            _default = ToolRunner()
        return _default


def use_workers(pool: WorkerPool | None = None) -> WorkerPool:
    """Run tools.WORKER_TOOLS in worker processes from now on (the daemon)."""
    runner = default_runner()
    if runner.pool is None:
        runner.pool = pool or WorkerPool()
        runner.pool.prewarm()
    return runner.pool
