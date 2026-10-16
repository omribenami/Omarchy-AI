"""Recovery: interrupted tasks, workspace checkpoints and rollback.

Split out of runtime/runtime.py, which re-exports every name here; TaskRuntime
inherits RecoveryMixin."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import time

from . import shell
from .permissions import Assessment, Risk, decide
from .task import INTERRUPTED, RUNNING, Task, WAITING_APPROVAL, WAITING_USER, owner_alive

_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # SHA-1 or SHA-256 repositories
_ARTIFACT = re.compile(r"(?:^|/)(?:__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules|\.tox)/|\.py[co]$")
def _timeout_step(step: dict | None) -> bool:
    """Whether a worker exhausted its own assignment time, not task time."""
    return bool(step and step.get("outcome") == "blocked"
                and ((step.get("meta") or {}).get("failure_kind") == "timeout"
                     or "ran out of time for this assignment" in str(step.get("claim", "")).lower()))


def _timeout_question(task: Task) -> bool:
    return (task.status == WAITING_USER
            and "ran out of time for this assignment" in str(task.question or "").lower()
            and _timeout_step(task.steps[-1] if task.steps else None))


class RecoveryMixin:
    """TaskRuntime's recovery, checkpoint and rollback methods."""

    def resume(self, task_id: str | None, background: bool = True) -> dict:
        """Continue an interrupted task (its process died mid-step)."""
        self._mark_interrupted()
        task = self._get(task_id, {INTERRUPTED})
        if task is None:
            current = self.store.load(task_id) if task_id else None
            if current and current.status in (WAITING_APPROVAL, WAITING_USER):
                return {"ok": False, "message": f"task is {current.status}; approve/deny or answer it instead"}
            if current:
                return {"ok": False, "message": f"task {current.id} is {current.status}, not interrupted"}
            return {"ok": False, "message": "no interrupted task"}
        self._launch(task, background)
        return {"ok": True, "message": f"resumed {task.id}", "task_id": task.id}

    def recover_after_restart(self, task_ids: list[str] | None = None) -> list[str]:
        """Continue work cut off by this daemon restart, never cancelled work."""
        ids = task_ids if task_ids is not None else list(self._restart_recoveries)
        recovered = []
        for task_id in ids:
            task = self.store.load(task_id)
            if not task or task.status != INTERRUPTED:
                continue
            task.add_note("Resuming automatically after the Omarchy AI restart.")
            self.store.save(task)
            self._launch(task, background=True)
            recovered.append(task.id)
        return recovered

    def _mark_interrupted(self) -> None:
        # A task left "running" by a previous process, or a dead worker thread
        # in this process, was cut off mid-step. The saved state alone is never
        # enough to call it running.
        def missing_owner(t: Task) -> bool:
            if t.status != RUNNING or not owner_alive(t.owner):
                return t.status == RUNNING
            if t.owner and t.owner.get("pid") == os.getpid():
                worker = self._threads.get(t.id)
                return worker is None or not worker.is_alive()
            return False

        def mark(t):
            if missing_owner(t):
                t.status = INTERRUPTED
                t.owner = None
                for step in t.steps:
                    if step.get("outcome") == "running":
                        step["outcome"] = "interrupted"
                        step["finished_at"] = time.time()
                t.add_note("The runtime restarted while this task was running; its last step may be incomplete.")
        for task in self.store.list(50):
            if missing_owner(task):
                interrupted = self.store.update(task.id, mark)
                if interrupted and interrupted.status == INTERRUPTED:
                    self._restart_recoveries.append(interrupted.id)
                    self._emit(interrupted, "interrupted")
        # Older versions could turn an executor timeout into a user question.
        # There is nothing a user can answer to fix that, and pressing
        # "continue" merely runs the same timed-out assignment again. Repair
        # the persisted state once and let normal recovery continue it.
        for task in self.store.list(50):
            if not _timeout_question(task):
                continue

            def resume_timeout(t: Task) -> None:
                t.status, t.phase, t.question = INTERRUPTED, "interrupted", None
                t.add_note("Automatically resuming: an executor timeout is not a question for the user.")

            repaired = self.store.update(task.id, resume_timeout)
            if repaired and repaired.status == INTERRUPTED:
                self._restart_recoveries.append(repaired.id)

    # ------------------------------------------------------ workspace/git
    def _git(self, task: Task, *args: str, timeout: float = 30) -> shell.CommandResult:
        return shell.run(["git", *args], task.workspace, timeout=timeout)

    def _is_git(self, task: Task) -> bool:
        return self._git(task, "rev-parse", "--is-inside-work-tree").output.strip() == "true"

    def _baseline(self, task: Task) -> None:
        if task.checkpoints or not self._is_git(task):
            return
        self._checkpoint(task)
        task.change_marker = self._marker(task)[0]

    def _checkpoint(self, task: Task) -> None:
        if not self._is_git(task):
            return
        stash = self._git(task, "stash", "create").output.strip()
        head = self._git(task, "rev-parse", "--verify", "-q", "HEAD").output.strip()
        untracked = self._git(task, "ls-files", "--others", "--exclude-standard").output.splitlines()
        if not _OBJECT_ID.fullmatch(stash or ""):
            stash = head if _OBJECT_ID.fullmatch(head or "") else ""
        if not stash:
            # No commit yet (fresh `git init`): the empty tree is the baseline,
            # so every file the task creates is a change (code review 2026-09-23).
            stash = self._git(task, "hash-object", "-t", "tree", "/dev/null").output.strip()
            if not _OBJECT_ID.fullmatch(stash):
                return
            head = ""
        if stash != head and head:
            # Keep the snapshot reachable so `git gc` cannot drop it.
            self._git(task, "update-ref", f"refs/omarchy-ai/tasks/{task.id}/{len(task.checkpoints)}", stash)
        task.checkpoints.append({"ref": stash, "head": head, "untracked": untracked[:2000],
                                 "step": len(task.steps), "at": time.time()})
        self.store.save(task)

    def _marker(self, task: Task) -> tuple[str, list[str], str]:
        """Paths are relative to the workspace, which may be a repo subdirectory:
        `git diff` needs --relative to agree with ls-files/ls-tree there."""
        if not task.checkpoints:
            return task.change_marker, [], ""
        base = task.checkpoints[0]
        names = self._git(task, "diff", "--relative", "--name-only", base["ref"]).output.splitlines()
        untracked = set(self._git(task, "ls-files", "--others", "--exclude-standard").output.splitlines())
        # Running tests creates bytecode/caches; they are not changes (live run 2026-09-23).
        new = sorted(n for n in untracked - set(base["untracked"]) if not _ARTIFACT.search(n))
        diff = self._git(task, "diff", "--relative", base["ref"]).output
        digest = hashlib.sha1(diff.encode())
        for name in new[:200]:
            try:
                st = (Path(task.workspace) / name).stat()
                digest.update(f"{name}:{st.st_size}:{st.st_mtime_ns}".encode())
            except OSError:
                continue
        stat = self._git(task, "diff", "--relative", "--stat", base["ref"]).output
        return digest.hexdigest(), [n for n in names + new if n], stat

    def _observe_workspace(self, task: Task, step: int) -> None:
        if not task.checkpoints:
            return
        marker, files, stat = self._marker(task)
        if marker == task.change_marker:
            return
        task.change_marker = marker
        task.change_step = step
        task.budget["changed_at"] = time.time()
        task.files_modified = []
        task.note_files(files)
        for test in task.tests:
            test["after_change"] = False
        task.add_evidence("workspace_diff", f"{len(files)} file(s) differ from the task baseline:\n{stat[-1500:]}"
                          + (f"\nnew files: {', '.join(n for n in files if n not in stat)[:400]}" if files else ""),
                          ok=None, step=step)

    def _rollback(self, task: Task) -> bool:
        base = task.checkpoints[0] if task.checkpoints else None
        if not base:
            task.add_note("Rollback requested but there is no checkpoint.")
            return True
        subject = f"restore {task.workspace} to the state before this task ({len(task.files_modified)} file(s))"
        fp = f"rollback:{task.id}:{task.change_marker}"
        assessment = Assessment(Risk.ELEVATED, ["discards the changes made during this task"])
        decision = decide("rollback", fp, assessment, auto_approve=Risk.parse(task.auto_approve, Risk.NORMAL),
                          grants=set(task.grants))
        if decision.behavior != "allow":
            task.pending_approval = {"fingerprint": decision.fingerprint, "kind": "rollback", "subject": subject,
                                     "risk": "ELEVATED", "reasons": assessment.reasons, "asked_at": time.time()}
            task.next_dispatch = None
            task.status = WAITING_APPROVAL
            task.add_note("Waiting for approval to roll back; after approval Jev will direct the rollback again.")
            self.store.save(task)
            self._emit(task, "waiting_approval")
            return False
        _, files, _ = self._marker(task)
        tracked = self._git(task, "ls-tree", "-r", "--name-only", base["ref"]).output.splitlines()
        restore = [f for f in files if f in set(tracked)]
        new = [f for f in files if f not in set(tracked) and f not in set(base["untracked"])]
        problems = []
        if restore:
            result = self._git(task, "restore", f"--source={base['ref']}", "--worktree", "--", *restore)
            if not result.ok:
                problems.append(f"git restore failed: {result.output[-300:]}")
        workspace = Path(task.workspace).resolve()
        for name in new:
            path = (workspace / name).resolve()
            if workspace in path.parents and path.is_file():
                path.unlink(missing_ok=True)
        # Success is what the workspace shows afterwards, not what was attempted.
        _, remaining, _ = self._marker(task)
        if remaining:
            problems.append(f"{len(remaining)} file(s) still differ from the baseline: {', '.join(remaining[:10])}")
        task.add_evidence("rollback", f"restored {len(restore)} file(s), removed {len(new)} new file(s)"
                          + (f"; {'; '.join(problems)}" if problems else "; workspace matches the baseline"),
                          ok=not problems, step=len(task.steps) or None)
        self._observe_workspace(task, len(task.steps))
        task.add_note("Rolled back all changes made during this task." if not problems
                      else f"Rollback incomplete: {'; '.join(problems)}")
        self.store.save(task)
        return True
