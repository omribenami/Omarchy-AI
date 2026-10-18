"""Background workers: the lighter delegate path beside start_task (harness 4.2).

The conversation stays with the user; work that takes more than one quick
tool call goes elsewhere. A job of several steps that needs planning,
evidence and certification goes to the Task Runtime (start_task). A bounded
job of one kind goes to a background worker here:

    delegate(kind="research", brief="...")  # a web search and the answer, with sources
    delegate(kind="browser", brief="...")   # one goal on a website
    delegate(kind="files", brief="...")     # the Task Runtime already does file work:
                                            # routed to start_task, no second task system

A worker:
- runs its tool call on the ToolRunner (execution/runner.py): the tool's
  deadline, a cancel token, and its tool's own canceller;
- holds its resource while it runs: one worker at a time on the browser
  (research and browser jobs share the assistant's one browser), one on the
  desktop; at most MAX_WORKERS run or wait at once, and a delegate beyond
  that is refused with the ids that are busy;
- gets a bounded brief (brief(), like runtime/control.py brief()) and
  returns a structured report (kind, ok, summary, source, error_class);
- inherits the taint of the conversation turn that delegated it (its call
  runs under that turn's provenance and approvals session), and its report
  is untrusted: the summary is fenced wherever a model reads it
  (execution/provenance.py), and the worker is marked tainted;
- reports through the outbox (core/outbox.py): progress notes coalesced and
  rate-limited per worker, and one result, deduped by `worker:<id>:result`
  in the outbox ledger, across restarts too. The result is also kept in the
  agenda inbox until a conversation says it, as a task result is.

Workers are listed, cancelled and redirected through the task tools
(runtime/service.py): task_status(task_id="w-...") is a worker's state and
report, task_status(list=true) lists them beside the tasks, and
task_respond(task_id, cancel=true) / task_respond(task_id, guidance=...)
stop it or replace its brief (a running call is stopped and run again with
the new brief, on the same page).

Records live in STATE_DIR/workers.json. A worker a restart cut off is
marked interrupted and the user is told once.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote_plus

from ..core import outbox, statefile
from ..execution import provenance

log = logging.getLogger(__name__)

RESEARCH, BROWSER, FILES = "research", "browser", "files"
KINDS = (RESEARCH, BROWSER, FILES)
# Kinds the Task Runtime already covers ("file/media processing" is a
# start_task job): delegate routes them there instead of running a worker.
TASK_KINDS = frozenset({FILES})
TOOL = {RESEARCH: "browser_task", BROWSER: "browser_task"}
RESOURCE = {RESEARCH: "browser", BROWSER: "browser"}

MAX_WORKERS = 3
BRIEF_CHARS = 1200
REPORT_CHARS = 1500
KEEP = 50  # records kept in workers.json
PREFIX = "w-"
SOURCE = "external:worker"  # what a worker's report is, to a model reading it
SEARCH_URL = "https://www.google.com/search?q="

QUEUED, RUNNING, DONE, FAILED, CANCELLED, INTERRUPTED = (
    "queued",
    "running",
    "done",
    "failed",
    "cancelled",
    "interrupted",
)
OPEN = frozenset({QUEUED, RUNNING})
_TITLES = {
    DONE: "Omarchy {kind} done",
    FAILED: "Omarchy {kind} failed",
    INTERRUPTED: "Omarchy {kind} cut off by a restart",
}

# One worker at a time on each (process-wide; the browser also refuses a
# second task of its own, execution/browser_jev.py).
_resource_locks = {"browser": threading.Lock(), "desktop": threading.Lock()}

# The path of the records; None: STATE_DIR/workers.json, resolved at each use.
PATH: Path | None = None


def is_worker(task_id: object) -> bool:
    return isinstance(task_id, str) and task_id.startswith(PREFIX)


def result_key(worker_id: str) -> str:
    """The outbox dedupe key of a worker's one result."""
    return f"worker:{worker_id}:result"


class Busy(Exception):
    """MAX_WORKERS are running or waiting already."""


@dataclass(eq=False)
class Worker:
    id: str
    kind: str
    brief: str
    url: str = ""
    status: str = QUEUED
    conversation: str = ""
    created: float = field(default_factory=time.time)
    updated: float = field(default_factory=time.time)
    revision: int = 0  # how many times the brief was redirected
    redirects: list[str] = field(default_factory=list)  # the briefs it replaced
    progress: str = ""
    report: dict | None = None
    # The delegating turn (provenance.Turn): its origin and taint carry over.
    origin: str = ""
    tainted: bool = False
    sources: list[str] = field(default_factory=list)
    delivered: bool = False

    def summary(self, *, report: bool = True) -> dict:
        out: dict[str, Any] = {
            "id": self.id,
            "kind": self.kind,
            "status": self.status,
            "brief": self.brief[:240],
            "background_worker": True,
        }
        if self.revision:
            out["redirected"] = self.revision
        if self.progress and self.status in OPEN:
            out["progress"] = self.progress[:200]
        if report and self.report:
            out["report"] = self.report
        if self.tainted:
            out["untrusted_sources"] = self.sources[-3:]
        return out


def brief(worker: Worker) -> str:
    """The bounded goal one worker's tool call gets."""
    text = provenance.clip(" ".join(worker.brief.split()), BRIEF_CHARS)
    if worker.kind == RESEARCH:
        return (
            "Research this and finish on the page that answers it, so the answer and its source can be "
            f"read there: {text}"
        )
    return text


def _args(worker: Worker, resume: bool) -> dict:
    goal = brief(worker)
    url = worker.url or (
        SEARCH_URL + quote_plus(worker.brief[:200])
        if worker.kind == RESEARCH
        else "https://www.google.com"
    )
    args: dict[str, Any] = {"url": url, "goal": goal}
    if resume:
        args["resume"] = True
    return args


class Workers:
    """The process's background workers (get() in runtime/service.py)."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        runner=None,
        execute: Callable[[str, dict], Any] | None = None,
        limit: int = MAX_WORKERS,
    ) -> None:
        self._path = path
        if runner is None:
            from ..execution.runner import default_runner

            runner = default_runner()
        self.runner = runner
        self.execute = execute  # tests: a fake tool; None: the real one
        self.limit = limit
        self._lock = threading.RLock()
        self._workers: dict[str, Worker] = {}
        self._handles: dict[
            str, object
        ] = {}  # the runner's owner of each worker's calls
        self._threads: dict[str, threading.Thread] = {}
        self._load()

    # ---------------------------------------------------------------- records

    @property
    def path(self) -> Path:
        if self._path is not None:
            return self._path
        if PATH is not None:
            return PATH
        from .. import config

        return config.STATE_DIR / "workers.json"

    def _load(self) -> None:
        data = statefile.read_json(self.path, {})
        names = {f.name for f in fields(Worker)}
        cut_off: list[Worker] = []
        for raw in (data or {}).get("workers", []) if isinstance(data, dict) else []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            worker = Worker(**{k: v for k, v in raw.items() if k in names})
            if worker.status in OPEN:
                worker.status = INTERRUPTED
                worker.updated = time.time()
            self._workers[worker.id] = worker
            if worker.status in _TITLES and not worker.delivered:
                cut_off.append(worker)
        if cut_off:
            self._save()
        for worker in cut_off:  # a result claimed before the restart is not repeated
            self._deliver(worker)

    def _save(self) -> None:
        with self._lock:
            records = sorted(self._workers.values(), key=lambda w: w.created)[-KEEP:]
            self._workers = {w.id: w for w in records}
            with statefile.locked_json(self.path, {}) as doc:
                doc.data = {"workers": [asdict(w) for w in records]}

    def get(self, worker_id: str) -> Worker | None:
        with self._lock:
            return self._workers.get(worker_id)

    def recent(self, limit: int = 8) -> list[Worker]:
        with self._lock:
            return sorted(
                self._workers.values(), key=lambda w: w.created, reverse=True
            )[:limit]

    def running(self) -> list[Worker]:
        with self._lock:
            return [w for w in self._workers.values() if w.status in OPEN]

    # ---------------------------------------------------------------- control

    def start(
        self,
        kind: str,
        text: str,
        *,
        url: str = "",
        conversation: str = "",
        turn: provenance.Turn | None = None,
    ) -> Worker:
        if kind not in TOOL:
            raise ValueError(f"no background worker runs {kind!r} jobs")
        with self._lock:
            busy = self.running()
            if len(busy) >= self.limit:
                raise Busy(
                    f"Not started: {self.limit} background workers are already running "
                    f"({', '.join(w.id for w in busy)}). Tell the user; cancel one with task_respond "
                    "(task_id, cancel=true) or wait for one to finish."
                )
            worker = Worker(
                id=f"{PREFIX}{time.strftime('%H%M%S')}-{uuid.uuid4().hex[:4]}",
                kind=kind,
                brief=text,
                url=url,
                conversation=conversation,
                origin=turn.origin if turn else "",
                tainted=bool(turn and turn.tainted),
                sources=list(turn.sources) if turn else [],
            )
            self._workers[worker.id] = worker
            self._handles[worker.id] = object()
            self._save()
            thread = threading.Thread(
                target=self._run,
                args=(worker,),
                name=f"worker-{worker.id}",
                daemon=True,
            )
            self._threads[worker.id] = thread
        log.info("worker %s started: %s %r", worker.id, kind, text[:120])
        thread.start()
        return worker

    def cancel(self, worker_id: str) -> dict:
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return {"ok": False, "message": f"No background worker {worker_id}."}
            if worker.status not in OPEN:
                return {
                    "ok": False,
                    "message": f"Worker {worker_id} is not running ({worker.status}).",
                }
            worker.status, worker.updated = CANCELLED, time.time()
            self._save()
            handle = self._handles.get(worker_id)
        stopped = self.runner.cancel(owner=handle, reason="cancelled") if handle else 0
        log.info("worker %s cancelled (%d call(s) stopped)", worker_id, stopped)
        return {
            "ok": True,
            "message": f"Cancelled background worker {worker_id}; it reports nothing more.",
        }

    def redirect(self, worker_id: str, text: str) -> dict:
        """Replace the brief of an open worker: a running call is stopped and
        run again with the new brief (continuing on the same page)."""
        text = str(text or "").strip()
        if not text:
            return {"ok": False, "message": "guidance needs the new brief"}
        with self._lock:
            worker = self._workers.get(worker_id)
            if worker is None:
                return {"ok": False, "message": f"No background worker {worker_id}."}
            if worker.status not in OPEN:
                return {
                    "ok": False,
                    "message": f"Worker {worker_id} already ended ({worker.status}); delegate a new one.",
                }
            worker.redirects = (worker.redirects + [worker.brief])[-3:]
            worker.brief = text[: BRIEF_CHARS * 2]
            worker.revision += 1
            worker.updated = time.time()
            running = worker.status == RUNNING
            self._save()
            handle = self._handles.get(worker_id)
        if running and handle is not None:
            self.runner.cancel(owner=handle, reason="redirected")
        log.info("worker %s redirected: %r", worker_id, text[:120])
        return {
            "ok": True,
            "message": f"Worker {worker_id} now follows the new brief; its one result is announced.",
        }

    def join(self, worker_id: str, timeout: float | None = None) -> bool:
        """Wait for a worker's thread (tests); True when it ended."""
        thread = self._threads.get(worker_id)
        if thread is None:
            return True
        thread.join(timeout)
        return not thread.is_alive()

    # ---------------------------------------------------------------- work

    def _run(self, worker: Worker) -> None:
        lock = _resource_locks[RESOURCE[worker.kind]]
        while not lock.acquire(timeout=0.2):
            if worker.status == CANCELLED:
                return
        try:
            result = None
            ran = False
            while True:
                with self._lock:
                    if worker.status == CANCELLED:
                        return
                    revision = worker.revision
                    worker.status, worker.updated = RUNNING, time.time()
                    self._save()
                self._progress(worker, "redirected" if revision else "started")
                result = self._call(worker, resume=ran)
                ran = True
                with self._lock:
                    if worker.status == CANCELLED:
                        return
                    if worker.revision != revision:
                        continue  # redirected while it ran: again, with the new brief
                break
            self._finish(worker, result)
        except Exception:  # noqa: BLE001 -- a worker's crash is its result, never the daemon's
            log.exception("worker %s crashed", worker.id)
            with self._lock:
                if worker.status != CANCELLED:
                    worker.report = {
                        "kind": worker.kind,
                        "ok": False,
                        "summary": "the worker crashed",
                        "source": "system",
                        "error_class": "crashed",
                    }
                    worker.status = FAILED
                    self._save()
            if worker.status == FAILED:
                self._deliver(worker)
        finally:
            lock.release()

    def _call(self, worker: Worker, *, resume: bool):
        from ..core import approvals, conversations, effects

        conversations.CURRENT.set(worker.conversation)
        origin = worker.origin or f"worker:{worker.id}"
        turn = provenance.Turn(origin, worker.tainted, tuple(worker.sources))
        with (
            effects.origin(origin),
            provenance.in_turn(turn),
            approvals.session(worker.tainted),
        ):
            return self.runner.run(
                TOOL[worker.kind],
                _args(worker, resume),
                execute=self.execute,
                owner=self._handles.get(worker.id),
                tag=worker.id,
            )

    def _progress(self, worker: Worker, text: str) -> None:
        def note(said: str) -> None:
            with self._lock:
                worker.progress = said
                self._save()

        outbox.progress(f"worker-{worker.id}", text, note)

    def _finish(self, worker: Worker, result) -> None:
        source = getattr(result, "provenance", "") or "system"
        message = str(getattr(result, "message", ""))
        untrusted = bool(getattr(result, "untrusted", False))
        summary = (
            provenance.unfence(message) if untrusted else provenance.escape(message)
        )
        ok = bool(getattr(result, "ok", False))
        with self._lock:
            worker.report = {
                "kind": worker.kind,
                "ok": ok,
                "summary": summary[:REPORT_CHARS],
                "source": source,
                "error_class": getattr(result, "error_class", None),
                "seconds": round(float(getattr(result, "duration", 0.0)), 1),
            }
            if untrusted:
                worker.tainted = True
                if source not in worker.sources:
                    worker.sources = (worker.sources + [source])[-5:]
            worker.status, worker.updated = (DONE if ok else FAILED), time.time()
            self._save()
        log.info(
            "worker %s %s after %.1fs",
            worker.id,
            worker.status,
            worker.report["seconds"],
        )
        self._deliver(worker)

    def _deliver(self, worker: Worker) -> None:
        """The worker's one result, through the outbox (deduped across restarts)."""
        if worker.delivered or worker.status not in _TITLES:
            return
        report = worker.report or {}
        summary = str(report.get("summary") or "")
        outcome = {
            DONE: "finished",
            FAILED: "failed",
            INTERRUPTED: "was cut off by a restart",
        }[worker.status]
        # What a model reads in the announcement: the report fenced when untrusted.
        said = summary[:600]
        if summary and worker.tainted:
            said = provenance.fence(said, str(report.get("source") or SOURCE))
        detail = f"Background {worker.kind} job {worker.id} {outcome}. {said}".strip()
        entry = None
        try:
            from ..core import agenda

            entry = agenda.task_result(
                worker.id, worker.brief[:160], worker.status, detail
            )
        except Exception:  # noqa: BLE001 -- the outbox still says it
            log.warning("worker %s: agenda inbox unavailable", worker.id, exc_info=True)
        if entry is None:
            entry = {
                "id": uuid.uuid4().hex[:10],
                "task_id": worker.id,
                "title": worker.brief[:160],
                "detail": detail,
                "outcome": worker.status,
                "urgent": False,
            }
        if worker.tainted:
            entry["untrusted"] = SOURCE
        outbox.send(
            outbox.Message(
                outbox.RESULT,
                _TITLES[worker.status].format(kind=worker.kind),
                f"{worker.brief[:120]}\n\n{summary[:300]}".strip(),
                origin_id=f"worker-{worker.id}",
                dedupe_key=result_key(worker.id),
                entry=entry,
                conversation=worker.conversation,
            )
        )
        with self._lock:
            worker.delivered = True
            self._save()


def report_json(worker: Worker, *, report: bool = True) -> str:
    return json.dumps(worker.summary(report=report), ensure_ascii=False)
