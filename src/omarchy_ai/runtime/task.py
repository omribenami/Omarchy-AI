"""Persistent task state owned by the Task Runtime.

No model holds the whole task: Jev sees a compact brief built from this
record, each executor gets only its assignment, and the record survives
every agent call and a daemon restart (saved atomically after each change,
one JSON file per task under ~/.local/state/omarchy-ai/tasks/).

MiniMax Code keeps one Goal per session with host-owned status transitions
(agent-modules/goal/src/types.ts: a worker may only *propose* completion;
the host settles it after verification). The same asymmetry is kept here:
executors report claims, only the runtime records evidence, and only Jev's
certification -- behind code-owned evidence gates -- can mark a task done.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
import fcntl
import json
import os
from pathlib import Path
import secrets
import threading
import time

from ..config import STATE_DIR

TASKS_DIR = STATE_DIR / "tasks"

# Lifecycle. Terminal: certified, failed, cancelled.
PENDING, RUNNING, WAITING_APPROVAL, WAITING_USER = "pending", "running", "waiting_approval", "waiting_user"
CERTIFIED, UNVERIFIED, FAILED, CANCELLED, INTERRUPTED = "certified", "unverified", "failed", "cancelled", "interrupted"
TERMINAL = {CERTIFIED, UNVERIFIED, FAILED, CANCELLED}
ACTIVE = {PENDING, RUNNING}

# Bounds keep the file (and every brief built from it) small.
MAX_COMMANDS = 300
MAX_EVIDENCE = 200
MAX_TEXT = 4000


def _clip(text, n: int = MAX_TEXT) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def new_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + secrets.token_hex(3)


@dataclass
class Task:
    id: str
    goal: str
    workspace: str
    status: str = PENDING
    phase: str = "route"
    objective: str = ""
    source: str = "cli"          # voice | cli | schedule
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    plan: list[str] = field(default_factory=list)
    steps: list[dict] = field(default_factory=list)          # executor assignments + outcomes
    commands: list[dict] = field(default_factory=list)       # every command the harness ran or refused
    evidence: list[dict] = field(default_factory=list)       # harness-observed facts (and untrusted claims)
    decisions: list[dict] = field(default_factory=list)      # Jev decisions with probabilities
    files_modified: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    test_plan: dict = field(default_factory=dict)            # {"commands": [...], "validated": p}
    tests: list[dict] = field(default_factory=list)
    reviews: list[dict] = field(default_factory=list)
    checkpoints: list[dict] = field(default_factory=list)    # git baselines for rollback
    grants: list[str] = field(default_factory=list)          # permission fingerprints the user approved
    declined: list[str] = field(default_factory=list)        # fingerprints the user refused: never asked again
    auto_approve: str = "NORMAL"
    pending_approval: dict | None = None                     # {"fingerprint", "kind", "subject", "risk", "reasons"}
    question: str | None = None                              # ASK_USER
    answers: list[str] = field(default_factory=list)
    certification: dict = field(default_factory=dict)        # {"status", "p", "reason", "at"}
    result: str = ""                                         # short outcome for the user
    notes: list[str] = field(default_factory=list)           # runtime notes Jev should see (declines, rejections)
    next_dispatch: dict | None = None                        # the assignment to (re)dispatch on resume
    needs_code: float | None = None                          # Jev's P(goal needs a source change)
    change_marker: str = ""                                  # fingerprint of the workspace diff
    change_step: int = 0                                     # step that last changed the workspace
    cert_rejections: int = 0
    owner: dict | None = None                                # {"pid", "start"} of the process driving it now
    budget: dict = field(default_factory=lambda: {"max_steps": 12, "max_seconds": 3600})

    # -- recording -------------------------------------------------------
    def touch(self) -> None:
        self.updated_at = time.time()

    def add_step(self, executor: str, assignment: str, role: str = "work") -> dict:
        step = {"n": len(self.steps) + 1, "executor": executor, "role": role,
                "assignment": _clip(assignment, 2000), "started_at": time.time(),
                "finished_at": None, "outcome": "running", "claim": "", "evidence": []}
        self.steps.append(step)
        self.touch()
        return step

    def finish_step(self, step: dict, outcome: str, claim: str = "") -> None:
        step["outcome"] = outcome
        step["claim"] = _clip(claim, 2000)
        step["finished_at"] = time.time()
        self.touch()

    def add_command(self, record: dict) -> None:
        record = dict(record)
        record["output"] = _clip(record.get("output", ""), 3000)
        self.commands.append(record)
        del self.commands[:-MAX_COMMANDS]
        self.touch()

    def add_evidence(self, kind: str, text: str, *, source: str = "harness", ok: bool | None = None,
                     step: int | None = None) -> dict:
        """source='harness' is independently observed; anything an executor
        says about its own work is source='executor' and never counts as
        proof on its own."""
        item = {"id": len(self.evidence) + 1, "kind": kind, "source": source, "ok": ok,
                "step": step, "text": _clip(text, 2500), "at": time.time()}
        self.evidence.append(item)
        del self.evidence[:-MAX_EVIDENCE]
        if step is not None:
            for s in self.steps:
                if s["n"] == step:
                    s["evidence"].append(item["id"])
        self.touch()
        return item

    def add_decision(self, kind: str, value, p: float | None, extra: dict | None = None) -> None:
        self.decisions.append({"kind": kind, "value": value, "p": p, "at": time.time(), **(extra or {})})
        del self.decisions[:-100]
        self.touch()

    def add_error(self, text: str) -> None:
        self.errors.append(_clip(text, 800))
        del self.errors[:-50]
        self.touch()

    def note_files(self, paths) -> None:
        for p in paths:
            if p and p not in self.files_modified:
                self.files_modified.append(p)
        del self.files_modified[:-500]

    def add_note(self, text: str) -> None:
        self.notes.append(_clip(text, 500))
        del self.notes[:-20]
        self.touch()

    def attempts(self, executor: str | None = None) -> int:
        return sum(1 for s in self.steps if executor is None or s["executor"] == executor)

    def summary(self) -> dict:
        """What the user (or the live model) needs, no internals."""
        last = self.steps[-1] if self.steps else None
        return {"id": self.id, "goal": self.goal, "status": self.status, "phase": self.phase,
                "steps": len(self.steps), "last_step": ({k: last[k] for k in ("executor", "role", "outcome", "claim")}
                                                        if last else None),
                "pending_approval": self.pending_approval, "question": self.question,
                "certification": self.certification, "result": self.result,
                "files_modified": self.files_modified[-20:], "errors": self.errors[-3:]}


def _proc_start(pid: int) -> str | None:
    """Kernel start time of `pid` (field 22 of /proc/<pid>/stat), so a reused
    PID is not mistaken for the process that owned a task."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
        return stat[stat.rindex(")") + 2:].split()[19]
    except (OSError, ValueError, IndexError):
        return None


def this_process() -> dict:
    return {"pid": os.getpid(), "start": _proc_start(os.getpid())}


def owner_alive(owner: dict | None) -> bool:
    if not owner or not owner.get("pid"):
        return False
    start = _proc_start(int(owner["pid"]))
    return start is not None and start == owner.get("start")


def owned_elsewhere(task: Task) -> bool:
    """A live process other than this one is driving the task right now."""
    return owner_alive(task.owner) and task.owner.get("pid") != os.getpid()


class TaskStore:
    """One JSON file per task; atomic replace on every save.

    Several processes use the same files (the daemon drives voice tasks, the
    CLI drives its own and may approve or cancel any). Saves are serialized
    by an flock and never undo a cancellation made by another process: the
    driving process adopts it instead (code review 2026-09-23)."""

    def __init__(self, directory: Path | None = None):
        self.dir = Path(directory or TASKS_DIR)
        self._lock = threading.RLock()

    @contextmanager
    def locked(self):
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            with open(self.dir / ".lock", "a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _write(self, task: Task) -> None:
        task.touch()
        target = self.path(task.id)
        tmp = target.with_suffix(f".json.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(asdict(task), ensure_ascii=False, indent=1))
        os.replace(tmp, target)

    def update(self, task_id: str, change) -> Task | None:
        """Load, change and save one task atomically with respect to other savers."""
        with self.locked():
            task = self.load(task_id)
            if task is None:
                return None
            change(task)
            self._write(task)
            return task

    def path(self, task_id: str) -> Path:
        if not task_id or "/" in task_id or task_id.startswith("."):
            raise ValueError(f"bad task id {task_id!r}")
        return self.dir / f"{task_id}.json"

    def save(self, task: Task) -> None:
        with self.locked():
            on_disk = self.load(task.id)
            if on_disk and on_disk.status == CANCELLED and task.status != CANCELLED:
                task.status = CANCELLED
                task.result = on_disk.result or "Cancelled by the user."
            self._write(task)

    def load(self, task_id: str) -> Task | None:
        try:
            data = json.loads(self.path(task_id).read_text())
        except (OSError, ValueError):
            return None
        known = set(Task.__dataclass_fields__)
        return Task(**{k: v for k, v in data.items() if k in known})

    def list(self, limit: int = 20) -> list[Task]:
        if not self.dir.is_dir():
            return []
        files = sorted(self.dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
        return [t for t in (self.load(f.stem) for f in files) if t]

    def latest(self, statuses: set[str] | None = None) -> Task | None:
        for task in self.list(50):
            if statuses is None or task.status in statuses:
                return task
        return None
