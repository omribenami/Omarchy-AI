"""TaskRuntime: the execution plane. Owns task state, permissions and evidence.

    start(goal) -> plan (acceptance criteria) -> Jev ROUTE -> dispatch executor
      -> harness observes (commands, workspace diff, tests) -> Jev DIRECT
      -> CONTINUE | RETRY | CHANGE_EXECUTOR | SPAWN_SUBAGENT | REQUEST_REVIEW
         | RUN_TESTS | REQUEST_MORE_TESTS | ROLLBACK | ASK_USER | FAIL | CERTIFY
      -> ... -> code evidence gates + Jev CERTIFY -> certified

The user sees one task. Internally each step is one executor assignment with
only the context it needs; the runtime records every command, decision and
piece of evidence in the task file, so the task survives any single agent
call, a permission wait, a question to the user, or a daemon restart.

Permission waits are real pauses: the worker thread ends, the task is saved
as waiting_approval, and resume() -- from a desktop-notification button, the
CLI, or (ELEVATED only) the voice assistant -- re-dispatches the step.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import threading
import time
from typing import Callable

from ..core import approvals, logs
from . import shell
from ..execution import provenance
from .control import LIVE_GAPS, ControlPlane
from .executors import Executor, default_executors
from .executors.base import (BLOCKED, DIAGNOSE, DONE, FAILED as R_FAILED, IMPLEMENT, NEEDS_APPROVAL,
                             NEEDS_CODE_CHANGE, NEEDS_USER, REVIEW, TEST, WORK, Assignment, Report)
from . import llm, questions
from .llm import WorkerModel, WorkerModelError
from .permissions import _TOOLS_CLI, Assessment, Risk, Scope, classify, coding_agent_risk, decide, split_commands
from .task import (ACTIVE, AWAITING_CONFIRMATION, CANCELLED, CERTIFIED, FAILED, INTERRUPTED, RUNNING, TERMINAL, UNVERIFIED,
                   WAITING_APPROVAL, WAITING_USER, Task, TaskStore, new_id, owned_elsewhere, owner_alive,
                   request_text, this_process)

from .planning import (  # noqa: F401 -- re-exported
    PlanningMixin,
    API_WORKERS,
    CODING,
    PLAN_PROMPT,
    PLAN_TIMEOUT,
    PROJECT_TERMS,
    _LINK,
    _escalates,
    _model_of,
    links,
)
from .recovery import (  # noqa: F401 -- re-exported
    RecoveryMixin,
    _ARTIFACT,
    _OBJECT_ID,
    _timeout_question,
    _timeout_step,
)
from .certify import (  # noqa: F401 -- re-exported
    CertifyMixin,
    CERTIFY_P,
    CONFIRM_FLOOR_P,
    CONFIRM_MINUTES,
    MAX_CERT_REJECTIONS,
    MAX_PLAN_ATTEMPTS,
    NO_ANSWER_IS_YES,
    SIMULATED_P,
    USER_ONLY_P,
    VALIDATED_P,
    _MISSING,
)
from .directing import (  # noqa: F401 -- re-exported
    DirectingMixin,
    MAX_CONSECUTIVE_RETRIES,
    MAX_STEPS_WITHOUT_EVIDENCE,
)

log = logging.getLogger(__name__)

# Risk of the assistant's existing tools when a direct step calls them.
_ACTION_RISK = {"list_windows": Risk.LOW, "list_bar_icons": Risk.LOW, "battery_status": Risk.LOW,
                "list_cast_targets": Risk.LOW, "screenshot": Risk.LOW}


class TaskRuntime(PlanningMixin, DirectingMixin, CertifyMixin, RecoveryMixin):
    def __init__(self, *, store: TaskStore | None = None, executors: dict[str, Executor] | None = None,
                 control: ControlPlane | None = None, planner: WorkerModel | None = None,
                 auto_approve: str | None = None, notify: bool = True, run_action=None, desktop_task=None):
        self.store = store or TaskStore()
        self.executors = executors if executors is not None else default_executors()
        self.control = control or ControlPlane()
        self.planner = planner or WorkerModel()
        self.default_auto_approve = auto_approve or _config("task_auto_approve", "NORMAL")
        self.notify = notify
        self._run_action = run_action
        self._desktop_task = desktop_task
        self.listeners: list[Callable[[Task, str], None]] = []
        self._lock = threading.RLock()
        self._threads: dict[str, threading.Thread] = {}
        self._cancel: dict[str, threading.Event] = {}
        self._availability: tuple[float, dict] | None = None
        self._prompting: set[str] = set()  # tasks whose fingerprint prompt is on the phone now
        self._last_progress_notice: dict[str, float] = {}
        self._restart_recoveries: list[str] = []
        self._mark_interrupted()

    @staticmethod
    def _refresh_task_hud() -> None:
        try:
            from ..display import assistant_huds
            assistant_huds.refresh_tasks()
        except Exception:  # cosmetic display must never affect task execution
            log.debug("task runtime: could not refresh task HUD", exc_info=True)

    # ------------------------------------------------------------------ API
    def start(self, goal: str, workspace: str | None = None, *, source: str = "cli", background: bool = True,
              auto_approve: str | None = None, max_steps: int | None = None, agent: str = "",
              unsandboxed: bool = False, conversation: str = "") -> Task:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("a task needs a goal")
        ws = Path(workspace or os.getcwd()).expanduser().resolve()
        if not ws.is_dir():
            raise ValueError(f"workspace {ws} is not a directory")
        agent = (agent or "").upper().replace(" ", "_").replace("-", "_")
        if agent and agent not in self.executors:
            raise ValueError(f"unknown agent {agent!r}; known: {', '.join(sorted(self.executors))}")
        task = Task(id=new_id(), goal=goal[:4000], workspace=str(ws), source=source,
                    auto_approve=(auto_approve or self.default_auto_approve).upper(), agent=agent,
                    unsandboxed=bool(unsandboxed and agent in CODING), conversation=conversation)
        from ..core import identities
        who = identities.current()
        task.person, task.person_how = who.person or "", who.how or ""
        task.budget["max_steps"] = int(max_steps or _config("task_max_steps", 12))
        task.budget["max_seconds"] = int(_config("task_max_minutes", 60)) * 60
        self.store.save(task)
        self._refresh_task_hud()
        self._launch(task, background)
        return task

    def respond(self, task_id: str | None, *, approve: bool | None = None, answer: str | None = None,
                channel: str = "cli", background: bool = True) -> dict:
        """Approve/deny the pending permission request, or answer a question, then resume.

        The response must match what the task is waiting for: an approval goes
        only to a task waiting for approval, an answer only to one waiting for
        an answer. Without an id, the latest task waiting for that kind of
        response is used (never an interrupted or finished one)."""
        if approve is None and not answer:
            return {"ok": False, "message": "give an approval decision or an answer"}
        if approve is not None and not answer:
            # 2026-09-30 15:11: a worker asked "approve sending this email? Reply
            # Approve" as a question; the user's approve was refused because the
            # task waited for an answer, not an approval. A yes/no to a question
            # is its answer. (The harness still gates every risky step itself.)
            asking = (self.store.load(task_id) if task_id else
                      None if self.store.latest({WAITING_APPROVAL}) else self.store.latest({WAITING_USER}))
            if asking is not None and asking.status == WAITING_USER:
                task_id, answer = asking.id, ("Yes, approved: go ahead." if approve else "No, not approved: do not do it.")
                approve = None
        wanted = WAITING_APPROVAL if approve is not None else WAITING_USER
        task = self._get(task_id, {wanted})
        if task is None:
            if task_id and self.store.load(task_id):
                current = self.store.load(task_id)
                return {"ok": False, "message": f"task {task_id} is {current.status}, not waiting for "
                                                + ("approval" if wanted == WAITING_APPROVAL else "an answer")}
            return {"ok": False, "message": "no task is waiting for " + ("approval" if wanted == WAITING_APPROVAL
                                                                         else "an answer")}
        if (task.status == WAITING_USER and questions.cancel_intent(answer or "")
                and task.certification.get("status") != AWAITING_CONFIRMATION):
            # 2026-10-18 20:50: "cancel it" typed into the phone's answer box
            # went to the worker as an answer; the task never stopped, and the
            # next answers turned the lights on three more times. A cancel is
            # a cancel, whichever box it was typed in.
            self.store.update(task.id, lambda t: t.answers.append(str(answer)[:2000]))
            done = self.cancel(task.id)
            return {**done, "message": f"{done['message']} (the answer said to cancel it); nothing more will run"
                    if done.get("ok") else done.get("message", "")}
        if owned_elsewhere(task):
            return {"ok": False, "message": f"task {task.id} is being run by another process (pid {task.owner['pid']})"}
        if task.status == WAITING_APPROVAL:
            request = task.pending_approval or {}
            if approve and channel == "voice" and request.get("tap_only"):
                # A target that came from untrusted content (execution/provenance.py):
                # the spoken yes is relayed by the model that read it.
                sent = self.ask_fingerprint(task.id)
                if sent["ok"]:
                    return {"ok": True, "message": (
                        f"Not approved yet: '{request.get('subject')}' takes a target from untrusted content, so a "
                        "fingerprint prompt went to the phone. The task resumes when the user touches it. Tell the "
                        "user that in one sentence.")}
                return {"ok": False, "message": (
                    f"'{request.get('subject')}' takes a target from untrusted content. It can only be approved "
                    "with the Approve button on the desktop (the notification or the approval envelope), or "
                    f"`omarchy-ai-task approve {task.id}` -- not by voice. {sent['message']}")}
            if approve and channel == "voice" and Risk.parse(request.get("risk"), Risk.HIGH) >= Risk.HIGH:
                # A spoken yes is relayed by the model, so it cannot approve
                # HIGH risk; a fingerprint on the phone can (2026-09-30: the
                # user said "approve" and had no way to finish from there).
                sent = self.ask_fingerprint(task.id)
                if sent["ok"]:
                    return {"ok": True, "message": (
                        f"Not approved yet: '{request.get('subject')}' is HIGH risk, so a fingerprint prompt went to "
                        "the phone. The task resumes when the user touches it. Tell the user that in one sentence.")}
                return {"ok": False, "message": (
                    f"'{request.get('subject')}' is HIGH risk ({', '.join(request.get('reasons', []))}). It can only "
                    "be approved with the Approve button on the desktop (the notification or the approval "
                    f"envelope), or `omarchy-ai-task approve {task.id}` -- not by voice. {sent['message']}")}
            refused = (approvals.voice_refusal(risk=Risk.parse(request.get("risk"), Risk.HIGH),
                                               tainted=approvals.current_session().tainted)
                       if approve and channel == "voice" else None)
            if refused is not None:
                # One policy (core/approvals.py): a yes from a speaker who is not
                # verified as the owner, or from a conversation with untrusted
                # content in it, is not an approval; a tap still is.
                sent = self.ask_fingerprint(task.id)
                if sent["ok"]:
                    return {"ok": True, "message": (
                        f"Not approved yet: a spoken yes does not approve '{request.get('subject')}' ({refused}), so "
                        "a fingerprint prompt went to the phone. The task resumes when the user touches it. Tell the "
                        "user that in one sentence.")}
                return {"ok": False, "message": (
                    f"Not approved: a spoken yes does not approve '{request.get('subject')}' ({refused}). It can be "
                    "approved with the Approve button on the desktop (the notification or the approval envelope), "
                    f"or `omarchy-ai-task approve {task.id}`. {sent['message']}")}
            paused = next((s for s in task.steps if s["n"] == request.get("step")), None)
            if approve:
                task.grants.append(request.get("fingerprint", ""))
                task.add_note(f"User approved {request.get('kind')}: {request.get('subject')}")
                if task.next_dispatch:
                    # The whole command, not the 600 characters shown for approval.
                    task.next_dispatch.setdefault("context", {})["user_approved"] = (
                        request.get("command") or request.get("subject"))
                    task.next_dispatch["context"]["user_approved_kind"] = request.get("kind")
                if paused:
                    paused["outcome"] = "paused"
            else:
                # Refused for the rest of the task; the paused executor resumes
                # without it (its other work stays valid), rollbacks just stop.
                task.declined.append(request.get("fingerprint", ""))
                task.add_note(f"User DECLINED {request.get('kind')}: {request.get('subject')}. It will be refused "
                              "if requested again; continue without it.")
                if paused:
                    paused["outcome"] = "declined"
                if task.next_dispatch:
                    task.next_dispatch.setdefault("context", {})["user_declined"] = (
                        f"{request.get('subject')} -- do not request it or an equivalent again; continue without it")
            task.pending_approval = None
        else:
            task.answers.append(answer[:2000])
            if task.certification.get("status") == AWAITING_CONFIRMATION:
                pass  # the work is done: the answer is read in _confirmed, and no worker gets it
            elif not task.next_dispatch and task.steps:
                # A coordinator question has no paused dispatch. Reconsidering
                # the unchanged last report can ask the identical question
                # forever; deliver the answer to a concrete continuation.
                last = task.steps[-1]
                coder = self._coder_after(task, last)
                task.next_dispatch = (self._assignment(task, coder, "CHANGE_EXECUTOR") if coder
                                      else self._assignment(task, last["executor"], "CONTINUE"))
            if task.next_dispatch:
                task.next_dispatch.setdefault("context", {})["user_answer"] = answer[:2000]
            task.question = None
        # A user answer authorizes another bounded work segment. Preserve
        # cumulative accounting, but do not resume straight into an exhausted
        # budget left by the work that preceded the question.
        if len(task.steps) >= task.budget["max_steps"]:
            task.budget["max_steps"] = len(task.steps) + int(_config("task_max_steps", 12))
        if task.budget.get("used_seconds", 0) >= task.budget["max_seconds"]:
            task.budget["max_seconds"] = task.budget["used_seconds"] + int(_config("task_max_minutes", 60)) * 60
        task.result = ""
        self.store.save(task)
        self._refresh_task_hud()
        if task.status == CANCELLED:
            return {"ok": False, "message": f"task {task.id} was cancelled"}
        self._launch(task, background)
        return {"ok": True, "message": f"resumed task {task.id}", "task": task.summary()}

    def _coder_after(self, task: Task, last: dict) -> str | None:
        """The coding agent that takes over when the last worker reported a
        needed code change and the user then said go ahead. 2026-10-08: the
        answer went back to the same general worker, which ran `codex` by hand
        from a shell command, five approvals deep, and never changed a line."""
        if task.agent or last.get("report_status") != NEEDS_CODE_CHANGE or last.get("executor") in CODING:
            return None
        ready = [n for n in self._candidates(exclude={"TEST_AGENT", "REVIEW_AGENT"}, task=task) if n in CODING]
        picked = next((d.get("executor") for d in reversed(task.decisions) if d.get("kind") == "direct"), None)
        return picked if picked in ready else (sorted(ready)[0] if ready else None)

    def ask_fingerprint(self, task_id: str, fingerprint: str | None = None) -> dict:
        """Send the Flux fingerprint prompt for a waiting approval now: from
        the phone's approvals list, or when the user says "approve" to a HIGH
        request. The prompt at request time lasts ~20 s; this is the way back
        to it. The fingerprint's answer resumes the task (_fingerprint_prompt)."""
        task = self.store.load(task_id)
        request = (task.pending_approval or {}) if task else {}
        if not task or task.status != WAITING_APPROVAL or not request:
            return {"ok": False, "message": f"task {task_id} is not waiting for approval"}
        if fingerprint and request.get("fingerprint") != fingerprint:
            return {"ok": False, "message": "that request is no longer waiting (already answered or changed)"}
        if not _flux_approval_available():
            return {"ok": False, "message": "No phone is set up for fingerprint approval (Flux)."}
        with self._lock:
            if task.id in self._prompting:
                return {"ok": True, "message": "The fingerprint prompt is already on the phone."}
            self._prompting.add(task.id)

        def prompt():
            try:
                _fingerprint_prompt(self, task, request.get("fingerprint"))
            finally:
                with self._lock:
                    self._prompting.discard(task.id)
        threading.Thread(target=prompt, daemon=True, name=f"fingerprint-{task.id}").start()
        return {"ok": True, "message": "Sent the fingerprint prompt to the phone."}

    def expire_confirmations(self, now: float | None = None) -> list[str]:
        """A "Did it work?" nobody answered within CONFIRM_MINUTES is a yes:
        the user has that long to say no. Called on the heartbeat."""
        now = time.time() if now is None else now
        done = []
        for task in self.store.list(50):
            if (task.status != WAITING_USER or task.certification.get("status") != AWAITING_CONFIRMATION
                    or owned_elsewhere(task)):
                continue
            asked = float(task.budget.get("confirmation_asked") or task.updated_at or now)
            if now - asked >= CONFIRM_MINUTES * 60 and self.respond(task.id, answer=NO_ANSWER_IS_YES,
                                                                    channel="policy").get("ok"):
                done.append(task.id)
        if done:
            log.info("unanswered confirmations taken as yes: %s", ", ".join(done))
        return done

    def tidy_approvals(self, now: float | None = None) -> list[str]:
        """Approvals nobody answered do not wait forever (2026-09-30: three
        tasks had waited up to a day for a request the rules no longer made).
        One older than task_approval_hours expires and its task stops; a newer
        one that the current rules allow resumes on its own. Returns what it did."""
        now = time.time() if now is None else now
        ttl = float(_config("task_approval_hours", 4)) * 3600
        done = []
        for task in self.store.list(50):
            request = task.pending_approval or {}
            if task.status != WAITING_APPROVAL or not request or owned_elsewhere(task):
                continue
            asked = float(request.get("asked_at") or task.updated_at or now)
            if now - asked > ttl:
                hours = f"{ttl / 3600:g}"

                def expire(t, hours=hours, fingerprint=request.get("fingerprint")):
                    if t.status == WAITING_APPROVAL and (t.pending_approval or {}).get("fingerprint") == fingerprint:
                        t.status = CANCELLED
                        t.result = (f"The approval request expired: nobody answered it within {hours} hours. "
                                    "Ask again if it is still wanted.")
                        t.pending_approval = None
                        t.next_dispatch = None
                self.store.update(task.id, expire)
                done.append(f"{task.id}: expired")
            elif _allowed_now(task, request):
                task.add_note(f"The rules no longer ask about {request.get('kind')}: {request.get('subject')}; resumed.")
                self.store.save(task)
                if self.respond(task.id, approve=True, channel="policy").get("ok"):
                    done.append(f"{task.id}: no longer needs approval")
        if done:
            log.info("approvals tidied: %s", "; ".join(done))
            self._refresh_task_hud()
        return done

    def expire_stale_questions(self, now: float | None = None) -> list[str]:
        """A task waiting on a "question" that asks nothing does not wait
        forever. Real tasks 2026-10-17: a finished "the light is on" and a
        Shabbat-times answer sat as questions for a day; every reply resumed
        them. Older than questions.STALE_QUESTION_HOURS, such a task ends:
        cancelled if the worker said it was cancelled, otherwise unverified
        with the report as its result, delivered once as a result (the
        outbox key is the notice's fingerprint). Nothing is run again. A real
        question keeps waiting. Called on the heartbeat."""
        now = time.time() if now is None else now
        ttl = questions.STALE_QUESTION_HOURS * 3600
        done = []
        for task in self.store.list(50):
            if (task.status != WAITING_USER or task.certification.get("status") == AWAITING_CONFIRMATION
                    or owned_elsewhere(task) or questions.is_question(task.question or "")):
                continue
            asked = float(task.budget.get("question_asked") or task.updated_at or now)
            if now - asked < ttl:
                continue

            def expire(t, question=task.question):
                if t.status != WAITING_USER or t.question != question:
                    return  # answered or changed meanwhile
                report = questions.strip_prefix(question or "")
                t.question, t.next_dispatch, t.phase = None, None, "done"
                t.certification.setdefault("status", "not_certified")
                if questions.cancel_acknowledged(report):
                    t.status, t.result = CANCELLED, "Cancelled by the user."
                else:
                    t.status = UNVERIFIED
                    t.result = ((report[:1200] + "\n\n" if report else "")
                                + f"Not verified: it waited over {questions.STALE_QUESTION_HOURS:g} hours on a question "
                                "that asked nothing, so it was closed. Nothing was run again.")[:1500]
                t.add_note("Closed: waiting on a question that asked nothing.")
            closed = self.store.update(task.id, expire)
            if closed is not None and closed.status in TERMINAL:
                done.append(f"{task.id}: {closed.status}")
                self._emit(closed, closed.status)
        if done:
            log.info("stale task questions closed: %s", "; ".join(done))
        return done

    def reassign(self, task_id: str | None, agent: str, *, unsandboxed: bool = False,
                 background: bool = True) -> dict:
        """Hand an open task to the agent the user names; every later work
        step uses it (2026-09-27: "move that task to Codex, I'm
        paying API credit for nothing" -- it ran on qwen, then grok). A
        question or approval the old worker was waiting on is dropped: the
        new agent works it out itself."""
        agent = (agent or "").upper().replace(" ", "_").replace("-", "_")
        if agent not in self.executors or agent not in CODING:
            return {"ok": False, "message": f"can only move a task to {', '.join(sorted(CODING))}"}
        task = self._get(task_id, None) if task_id else self.store.latest(
            ACTIVE | {WAITING_APPROVAL, WAITING_USER, INTERRUPTED})
        if task is None:
            return {"ok": False, "message": "no such task"}
        if task.status in TERMINAL:
            return {"ok": False, "message": f"task {task.id} already {task.status}"}
        if owned_elsewhere(task):
            return {"ok": False, "message": f"task {task.id} is being run by another process (pid {task.owner['pid']})"}
        waiting = task.status in (WAITING_APPROVAL, WAITING_USER, INTERRUPTED)
        task.agent, task.unsandboxed = agent, bool(unsandboxed)
        task.add_note(f"User moved the task to {agent}" + (" without its sandbox" if unsandboxed else "")
                      + "; it continues from the work so far.")
        if waiting:
            dropped = (task.pending_approval or {}).get("subject") or task.question
            task.pending_approval, task.question = None, None
            task.next_dispatch = self._assignment(task, agent, "CHANGE_EXECUTOR")
            if dropped:
                task.next_dispatch["context"]["previous_worker_was_waiting_on"] = str(dropped)[:1500]
        self.store.save(task)
        self._refresh_task_hud()
        if waiting:
            self._launch(task, background)
        return {"ok": True, "message": f"task {task.id} moved to {agent}", "task": task.summary()}

    def steer(self, task_id: str, text: str) -> dict:
        """New direction from the user for a task that keeps running: its next
        step sees it with the user's answers (and Jev with its notes). Used
        instead of starting a duplicate task for the same job, and when the
        assistant corrects a task that is going the wrong way."""
        text = str(text or "").strip()[:2000]
        if not text:
            return {"ok": False, "message": "no guidance given"}
        task = self.store.load(task_id)
        if task is None:
            return {"ok": False, "message": "no such task"}
        if task.status in TERMINAL:
            return {"ok": False, "message": f"task {task.id} already {task.status}"}
        if task.status == WAITING_USER:
            # A task waiting on its question takes the update as the answer and
            # goes on (2026-09-30: "yes, send it" was noted, and it kept waiting).
            return self.respond(task.id, answer=text, channel="voice")

        def mark(t):
            t.answers.append(f"User update: {text}")
            t.add_note(f"User update: {text}")
            if t.next_dispatch:
                t.next_dispatch.setdefault("context", {})["user_update"] = text
        self.store.update(task.id, mark)
        return {"ok": True, "message": f"task {task.id} will follow the update from its next step"}

    def cancel(self, task_id: str | None) -> dict:
        """Works across processes: the file is marked cancelled under the store
        lock; a process driving the task adopts that on its next save and stops
        (a command already running finishes first unless it is in this process)."""
        task = self._get(task_id, None) if task_id else self.store.latest(ACTIVE | {WAITING_APPROVAL, WAITING_USER,
                                                                                   INTERRUPTED})
        if task is None:
            return {"ok": False, "message": "no such task"}
        if task.status in TERMINAL:
            return {"ok": False, "message": f"task {task.id} already {task.status}"}
        event = self._cancel.get(task.id)
        if event:
            event.set()
        from ..execution import runner
        runner.default_runner().cancel(owner=f"task:{task.id}", reason="the task was cancelled")

        def mark(t):
            if t.status not in TERMINAL:
                t.status = CANCELLED
                t.result = "Cancelled by the user."
                t.pending_approval = None
                t.next_dispatch = None
        task = self.store.update(task.id, mark) or task
        self._refresh_task_hud()
        self._emit(task, "cancelled")
        return {"ok": True, "message": f"cancelled {task.id}"}

    def status(self, task_id: str | None = None) -> dict | None:
        # The JSON record survives a daemon restart; its "running" value
        # does not prove a worker still exists.  Reconcile before presenting
        # it as truth to chat, the phone, CLI, or HUD callers.
        self._mark_interrupted()
        task = self._get(task_id, None)
        return task.summary() if task else None

    def wait(self, task_id: str, timeout: float | None = None) -> Task | None:
        thread = self._threads.get(task_id)
        if thread:
            thread.join(timeout)
        return self.store.load(task_id)

    def available(self, refresh: bool = False) -> dict[str, dict]:
        if refresh or not self._availability or time.time() - self._availability[0] > 300:
            from .executors import availability
            self._availability = (time.time(), availability(self.executors))
        return self._availability[1]

    # ------------------------------------------------------------ internals
    def _get(self, task_id, statuses) -> Task | None:
        if task_id:
            task = self.store.load(task_id)
            return task if task and (statuses is None or task.status in statuses) else None
        return self.store.latest(statuses)

    def _launch(self, task: Task, background: bool) -> None:
        with self._lock:
            running = self._threads.get(task.id)
            if running and running.is_alive():
                return
            if task.status in TERMINAL or owned_elsewhere(task):
                log.warning("not launching task %s (%s, owner %s)", task.id, task.status, task.owner)
                return
            self._cancel[task.id] = threading.Event()
            if not background:
                self._drive(task.id)
                return
            # The conversation and tool call that started it stay on its log lines (core/logs.py).
            thread = threading.Thread(target=logs.carry(self._drive), args=(task.id,), name=f"task-{task.id}",
                                      daemon=True)
            self._threads[task.id] = thread
            thread.start()

    @staticmethod
    def _collect_outputs(task: Task) -> None:
        """Link the files the task wrote for the user (core/outputs.py) from its record."""
        try:
            from ..core import outputs
            for path in outputs.collect(task.id):
                if path not in task.artifacts:
                    task.artifacts.append(path)
        except Exception:  # noqa: BLE001 -- bookkeeping must never fail a task
            log.debug("could not list task outputs", exc_info=True)

    def _emit(self, task: Task, event: str) -> None:
        self._refresh_task_hud()
        for listener in list(self.listeners):
            try:
                listener(task, event)
            except Exception:  # noqa: BLE001
                log.warning("task listener failed", exc_info=True)
        if self.notify:
            _notify(self, task, event)

    def _progress(self, task: Task, label: str) -> None:
        """Publish one short, factual live-stage label to every task surface."""
        task.phase = "status:" + " ".join(str(label).split())[:140]
        self.store.save(task)
        self._emit(task, "progress")

    def report_running_progress(self, every_seconds: float = 45) -> None:
        """Surface a bounded heartbeat from the authoritative task record.

        The chat and HUD receive the identical event.  This is deliberately
        separate from executor output: a slow browser or coding step may have
        no new text for a while, but the task owner is still alive.
        """
        self._mark_interrupted()
        now = time.time()
        for task in self.store.list(50):
            if task.status != RUNNING or not owner_alive(task.owner):
                continue
            if now - self._last_progress_notice.get(task.id, 0) < every_seconds:
                continue
            self._last_progress_notice[task.id] = now
            self._emit(task, "progress")

    # -------------------------------------------------------------- driver
    def _drive(self, task_id: str) -> None:
        with logs.bind(task_id=task_id, step_id=None):  # its log lines (core/logs.py)
            self._drive_bound(task_id)

    def _drive_bound(self, task_id: str) -> None:
        task = self.store.load(task_id)
        if task is None:
            return
        from ..core import identities
        # It acts as the person who started it (MyApi is the owner's).
        token = identities.set_current(identities.Identity(task.person or None, task.person_how or None))
        try:
            self._loop(task)
        except Exception as exc:  # noqa: BLE001 -- a crash must leave an honest record
            log.exception("task %s crashed", task.id)
            task.add_error(f"runtime crash: {type(exc).__name__}: {exc}")
            self._finish(task, FAILED, f"The task runtime hit an internal error: {exc}")
        finally:
            identities.reset_current(token)
            task.owner = None
            self.store.save(task)

    def _loop(self, task: Task) -> None:
        cancel = self._cancel.setdefault(task.id, threading.Event())
        task.status = RUNNING
        task.owner = this_process()
        task.phase = "starting"
        self.store.save(task)  # adopts a cancel made since the launch, by any process
        if task.status == CANCELLED:  # cancelled between launch and start
            task.owner = None
            return
        # A task can spend noticeable time planning or selecting an executor.
        # Tell every attached surface immediately, rather than making progress
        # visibility depend on the first worker process having started.
        self._emit(task, "progress")
        if task.certification.get("status") == AWAITING_CONFIRMATION and task.answers:
            return self._confirmed(task)
        resumed_at = time.time()
        used = float(task.budget.get("used_seconds", 0))
        if not task.objective:
            self._plan(task)
            self._baseline(task)
        if not task.steps and not task.next_dispatch:
            if not self._route(task):
                return
        ops = 0
        while True:
            ops += 1
            task.budget["used_seconds"] = round(used + time.time() - resumed_at)
            self.store.save(task)  # also adopts a cancel made by another process
            if cancel.is_set() or task.status == CANCELLED:
                task.status = CANCELLED
                task.result = task.result or "Cancelled by the user."
                self.store.save(task)
                self._emit(task, "cancelled")
                return
            if len(task.steps) >= task.budget["max_steps"] or ops > task.budget["max_steps"] * 3:
                return self._out_of_budget(task, "step budget")
            if task.budget["used_seconds"] > task.budget["max_seconds"]:
                return self._out_of_budget(task, "time budget")
            if task.next_dispatch:
                if not self._dispatch(task, task.next_dispatch):
                    return  # waiting for approval / the user
                continue
            if not self._direct(task):
                return

    def _api_allowed(self, task: Task) -> bool:
        if str(_config("task_api_worker", "fallback")).lower() == "always":
            return True
        if self._coding_ready():
            return False
        return f"api-worker:{task.id}" in task.grants

    # ------------------------------------------------------------ dispatch
    def _assignment(self, task: Task, executor: str, directive: str, last: dict | None = None) -> dict:
        last = last or (task.steps[-1] if task.steps else None)
        last_claim = (last or {}).get("claim", "")[:1200]
        needs_code = (task.needs_code or 0) >= 0.5 or bool(last and last.get("report_status") == NEEDS_CODE_CHANGE)
        if executor == "TEST_AGENT" or directive == "REQUEST_MORE_TESTS":
            role = TEST
        elif executor == "REVIEW_AGENT" or directive == "REQUEST_REVIEW":
            role = REVIEW
        elif executor in CODING and executor == task.agent:
            role = WORK  # asked for by name to do the job, not just to diagnose it
        elif executor in CODING:
            role = IMPLEMENT if needs_code else DIAGNOSE
        else:
            role = WORK
        prev = (last or {}).get("executor", "")
        instructions = {
            "ROUTE": f"Accomplish this: {task.objective or task.goal}",
            "CONTINUE": (f"Continue the task. The previous step ({prev}) reported (unverified): {last_claim}\n"
                         "Do the remaining work so every acceptance criterion is met, then check the result."),
            "RETRY": (f"Retry. The previous attempt by {prev} did not succeed: {last_claim}\n"
                      "Use a different approach from the one that failed."),
            "CHANGE_EXECUTOR": (f"Take over from {prev}, which reported (unverified): {last_claim}\n"
                                + ("A code defect was identified: fix it in the repository and verify the fix."
                                   if role == IMPLEMENT else "Continue from there with your own capabilities.")),
            "SPAWN_SUBAGENT": ("Focused sub-assignment: check the current real state against the acceptance criteria "
                               "and report concrete evidence for each one (what is met, what is not, and why)."),
            "REQUEST_REVIEW": "Independently review the changes made during this task against the goal and criteria.",
            "REQUEST_MORE_TESTS": ("Design verification for this task. Find how the acceptance criteria can be checked "
                                   "against the real behavior on this machine/project, run each check once to be sure "
                                   "it works, and return them as test_commands."),
        }.get(directive, f"Accomplish this: {task.objective or task.goal}")
        instructions += (
            "\n\nPersistence rule: do not stop at 'unsupported' or 'no tool'. Use existing system/API capabilities "
            "first. If a reusable capability is genuinely absent, have Claude Code or Codex implement and test a "
            "narrow Omarchy user tool through the propose/install approval workflow, then resume this same task "
            "and finish its original acceptance criteria. Wait for a specific approval or user answer when needed; "
            "do not turn a solvable capability gap into a final failure. Missing ACCESS is different: a key that "
            "is not there, a 401/403, an account or project that may not use a model or endpoint, a login. That "
            "is the user's to grant: ask them with the exact error, and do not rewrite their project around it."
        )
        try:
            from ..core import outputs
            instructions += (
                f"\n\nFiles for the user: write any report, screenshot, CSV or other file the user should receive "
                f"into {outputs.task_dir(task.id)} (create the folder). Files there are handed to the user when "
                "the task ends; do not put anything else there."
            )
        except ValueError:
            pass
        timeout = float(_config("task_coding_agent_timeout", 1200)) if executor in CODING else 600.0
        return {"executor": executor, "role": role, "directive": directive, "instructions": instructions,
                "write": role in (IMPLEMENT, WORK), "timeout": timeout, "context": {}}

    def _context_for(self, task: Task, dispatch: dict) -> dict:
        recent = [{"executor": s["executor"], "role": s["role"], "outcome": s["outcome"],
                   "report_UNVERIFIED": s["claim"][:900], "findings_UNVERIFIED": s.get("findings", [])[:6]}
                  for s in task.steps[-3:]]
        harness = [{"kind": e["kind"], "ok": e["ok"], "text": e["text"][:500]}
                   for e in task.evidence if e["source"] != "executor"][-8:]
        context = {"objective": task.objective, "acceptance_criteria": task.plan, "earlier_steps": recent,
                   "evidence_observed_by_omarchy": harness, "files_modified_so_far": task.files_modified[-20:],
                   "notes": task.notes[-5:], "user_answers": task.answers[-3:]}
        for previous in reversed(task.steps):
            if previous.get("executor") == dispatch.get("executor") and previous.get("worker_checkpoint"):
                context["worker_checkpoint"] = previous["worker_checkpoint"]
                break
        if task.reviews:
            context["latest_review"] = task.reviews[-1]
        if task.unsandboxed and dispatch.get("executor") == task.agent:
            # Retain the user's requested mode in task context/history. The
            # coding-agent CLIs use their non-interactive flags by default.
            context["unsandboxed"] = True
        context.update(dispatch.get("context") or {})
        return context

    def _dispatch(self, task: Task, dispatch: dict) -> bool:
        """Run one executor assignment. False when the task must wait."""
        if task.agent and dispatch["role"] not in (TEST, REVIEW) and dispatch["executor"] != task.agent:
            dispatch = self._assignment(task, task.agent, "CHANGE_EXECUTOR")  # moved to it mid-run
        name = dispatch["executor"]
        executor = self.executors.get(name)
        if executor is None:
            task.add_error(f"unknown executor {name}")
            task.next_dispatch = None
            return True
        if task.status == CANCELLED:
            # Adopted from the file by the loop's last save: no step after a
            # cancel, whoever made it (the loop then stops and says so).
            return True
        task.phase = f"{name.lower()}:{dispatch['role']}"
        if dispatch["write"] and name in CODING:
            self._checkpoint(task)
        step = task.add_step(name, dispatch["instructions"], dispatch["role"])
        step["directive"] = dispatch.get("directive")
        tier = llm.TIER.set(task.model_tier)  # this task's escalation step, for every worker call below
        step["model"] = _model_of(executor)
        self._progress(task, name.replace("_", " ").title())
        assignment = Assignment(task_id=task.id, role=dispatch["role"], goal=task.goal,
                                instructions=dispatch["instructions"], workspace=task.workspace,
                                context=self._context_for(task, dispatch), write_access=dispatch["write"],
                                timeout=dispatch.get("timeout", 600))
        ctx = WorkContextImpl(self, task, step["n"])
        try:
            with logs.bind(step_id=step["n"]):
                report = executor.run(assignment, ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("executor %s crashed", name)
            report = Report(R_FAILED, claim=f"{name} crashed: {type(exc).__name__}: {exc}")
        finally:
            llm.TIER.reset(tier)
        if report.status == NEEDS_USER and not report.question and not questions.is_question(report.claim):
            # A report with nothing to answer is a result, not a question
            # (2026-10-17: "the light is on" waited a day as the question).
            task.add_note(f"{name} said it needed the user but asked nothing; its report is taken as its result.")
            report.status = DONE
        outcome = {DONE: "done", NEEDS_APPROVAL: "waiting_approval", NEEDS_USER: "waiting_user",
                   NEEDS_CODE_CHANGE: "done", BLOCKED: "blocked"}.get(report.status, "failed")
        task.finish_step(step, outcome, report.claim)
        step["report_status"] = report.status
        step["findings"] = [f[:400] for f in report.findings][:10]
        step["meta"] = {k: v for k, v in report.meta.items() if k != "history"}
        if getattr(getattr(executor, "model", None), "last_used", None):
            step["model"] = executor.model.last_used  # the backend that really answered last
        if report.meta.get("models"):  # what the external agent actually used
            step["model"] = ", ".join(report.meta["models"])
        if outcome in ("failed", "blocked") and _escalates(executor):
            self._escalate(task, step)
        if report.claim:
            task.add_evidence("claim", report.claim, source="executor", step=step["n"])
        self._observe_workspace(task, step["n"])
        if report.verdict or dispatch["role"] == REVIEW:
            task.reviews.append({"by": name, "verdict": report.verdict or "unclear", "issues": report.findings[:10],
                                 "summary": report.claim[:800], "step": step["n"], "at": time.time()})
            task.add_evidence("review", f"{name} review verdict: {report.verdict or 'unclear'}; "
                              + "; ".join(report.findings[:5]), source="reviewer",
                              ok=report.verdict == "pass", step=step["n"])
        if dispatch["role"] == TEST and report.test_commands:
            self._propose_tests(task, report.test_commands)
        if report.status == NEEDS_APPROVAL and report.approval:
            if report.meta.get("history"):
                dispatch.setdefault("context", {})["your_progress_before_the_pause"] = report.meta["history"]
            task.next_dispatch = dispatch
            task.pending_approval = {**report.approval, "step": step["n"]}
            task.status = WAITING_APPROVAL
            task.phase = "waiting_approval"
            self.store.save(task)
            self._emit(task, "waiting_approval")
            return False
        if report.status == NEEDS_USER:
            task.next_dispatch = dispatch
            self._ask_user(task, report.question or report.claim or "I need your input to continue.")
            return False
        task.next_dispatch = None
        self.store.save(task)
        self._emit(task, "progress")
        return True

    # ------------------------------------------------------------ testing
    def _propose_tests(self, task: Task, commands: list[str]) -> None:
        prefix = f"cd {task.workspace} && "
        commands = [c[len(prefix):] if c.startswith(prefix) else c for c in commands]
        masked = [c for c in commands if masked_exit_code(c)]
        if masked:
            task.test_plan.update(commands=commands[:8], validated=0.0, proposed_at=time.time())
            task.add_decision("validate", False, 0.0, {"commands": commands[:8], "reason": "exit code masked"})
            task.add_note("Harness rejected the verification plan: these commands always exit 0 and cannot fail: "
                          + " | ".join(masked[:3]))
            self.store.save(task)
            return
        p = self.control.validate(task, commands)
        task.test_plan.update(commands=commands[:8], validated=p, proposed_at=time.time())
        task.add_decision("validate", p is not None and p >= VALIDATED_P, p, {"commands": commands[:8]})
        if p is None or p < VALIDATED_P:
            task.add_note(f"Jev did not accept the proposed verification (p={p}); it would not prove the behavior.")
        self.store.save(task)

    def _run_tests(self, task: Task) -> None:
        task.phase = "testing"
        ctx = WorkContextImpl(self, task, len(task.steps))
        for command in task.test_plan.get("commands", []):
            result = ctx.run_command(command, task.workspace, 900, role="test")
            decision = result.get("decision")
            if decision == "ask":
                task.add_note(f"Test command needs approval and was skipped: {command}")
                continue
            task.tests.append({"command": command, "exit_code": result.get("exit_code"), "after_change": True,
                               "output": (result.get("output") or "")[-1500:], "at": time.time(),
                               "step": len(task.steps)})
            task.add_evidence("test", f"`{command}` -> exit {result.get('exit_code')}: "
                              + (result.get("output") or "")[-600:], ok=result.get("exit_code") == 0,
                              step=len(task.steps) or None)
        self.store.save(task)

    def _tests_since_change(self, task: Task) -> list[dict]:
        return [t for t in task.tests if t.get("after_change") and t["at"] >= task.budget.get("changed_at", 0)]

    def _tester(self, task: Task) -> str | None:
        """Who designs verification: the API test agent only where API work
        is allowed; otherwise a coding agent (the task's own first)."""
        available = self._candidates(roles={TEST}, task=task)
        if "TEST_AGENT" in available:
            return "TEST_AGENT"
        coding = [n for n in available if n in CODING]
        return task.agent if task.agent in coding else (coding[0] if coding else None)

    def _reviewers(self, task: Task) -> list[str]:
        writers = {s["executor"] for s in task.steps if s["role"] in (IMPLEMENT, WORK)}
        available = self._candidates(roles={REVIEW}, task=task)
        independent = [n for n in available if n in CODING and n not in writers]
        return independent + (["REVIEW_AGENT"] if "REVIEW_AGENT" in available else [])

    def _reviewed_since_change(self, task: Task) -> bool:
        return any(r["at"] >= task.budget.get("changed_at", 0) for r in task.reviews)

    # ----------------------------------------------------------- endings
    def _ask_user(self, task: Task, question: str) -> None:
        task.question = question[:800]
        task.budget["question_asked"] = time.time()
        task.status = WAITING_USER
        task.phase = "waiting_user"
        self.store.save(task)
        self._emit(task, "waiting_user")

    def _out_of_budget(self, task: Task, which: str) -> None:
        verified = self._certification_gate(task)[0]
        self._finish(task, UNVERIFIED if task.steps else FAILED,
                     f"Stopped: the task used its {which}. "
                     + ("The last results look complete but were not certified." if verified
                        else f"Unfinished: {task.certification.get('gate') or 'more work was needed'}."))

    def _escalate(self, task: Task, step: dict) -> None:
        """A cheaper worker model failed a step: the rest of this task runs
        one step up the qualified ladder (runtime/models.py)."""
        from . import models
        steps = models.ladder()
        if task.model_tier + 1 < len(steps):
            task.model_tier += 1
            task.add_note(f"Step {step['n']} fell short on {step.get('model')}; the next steps use the stronger worker "
                          f"model {steps[task.model_tier]}.")

    def _finish(self, task: Task, status: str, message: str) -> None:
        if status in (CERTIFIED, UNVERIFIED, FAILED):
            try:
                from . import models
                used = {s["model"] for s in task.steps if s.get("model") and s.get("executor") not in CODING}
                if used and any(m in models.ladder() or m in models._load().get("exams", {}) for m in used):
                    models.record_outcome(used, status == CERTIFIED)
            except Exception:  # noqa: BLE001 -- bookkeeping must never fail a task
                log.debug("could not record worker model outcome", exc_info=True)
        jev = getattr(self.control, "jev", None)
        if status == CERTIFIED and jev is not None:
            # A verified job of several steps becomes a skill holding the exact
            # commands that worked, or brings the one it carried out up to date
            # (core/skills.py; 2026-10-12: the skill for such a job was saved
            # only when the user asked, and as a paraphrase).
            try:
                from ..core import skills
                learned = skills.learn_from_task(task, jev)
                if learned:
                    message = (f"{message[:1400]} {'Saved as' if learned['created'] else 'Updated'} skill "
                               f"'{learned['skill']['name']}'.")
            except Exception:  # noqa: BLE001 -- a skill not saved must never fail a task
                log.warning("could not learn a skill from task %s", task.id, exc_info=True)
        self._collect_outputs(task)
        task.status = status
        task.phase = "done"
        task.result = message[:1500]
        task.next_dispatch = None
        if status != CERTIFIED:
            task.certification.setdefault("status", "not_certified")
        self.store.save(task)
        self._emit(task, status)


# ---------------------------------------------------------------- context
class WorkContextImpl:
    """The only way an executor touches the machine. Every call is checked
    against the task's permission policy and recorded in the task."""

    def __init__(self, runtime: TaskRuntime, task: Task, step: int):
        self.runtime, self.task, self.step = runtime, task, step
        self.scope = Scope(workspaces=[Path(task.workspace)])

    @property
    def cancel_requested(self) -> bool:
        event = self.runtime._cancel.get(self.task.id)
        if self.task.status == CANCELLED and event:
            event.set()  # stops a command running in this process
        return bool(event and event.is_set()) or self.task.status == CANCELLED

    def _decide(self, kind: str, subject: str, assessment: Assessment):
        decision = decide(kind, subject, assessment, auto_approve=Risk.parse(self.task.auto_approve, Risk.NORMAL),
                          grants=set(self.task.grants))
        if decision.behavior == "ask" and decision.fingerprint in self.task.declined:
            decision.behavior = "deny"
            assessment.reasons.append("the user already declined this in this task")
        return decision

    def _request(self, decision, kind: str, subject: str) -> dict:
        request = {"fingerprint": decision.fingerprint, "kind": kind, "subject": subject[:600],
                "risk": decision.assessment.risk.name, "reasons": decision.assessment.reasons[:5],
                "asked_at": time.time()}
        if kind == "command" and len(subject) > 600:
            # 2026-10-08: a 744-character command was approved, replayed from the
            # shortened subject, no longer matched its grant, was asked for again
            # and then failed on its cut-off quote. The subject is for display.
            request["command"] = subject
        if kind == "command" and re.match(r"\s*sudo\b", subject):
            request["task_summary"] = (self.task.objective or self.task.goal)[:240]
            request["sudo_action"] = subject[:600]
        return request

    def _stage(self, text: str) -> None:
        self.runtime._progress(self.task, text)

    def checkpoint(self, history: list[dict], pending: dict | None) -> None:
        """Save action outcomes, plus uncertain in-flight work, before a restart."""
        step = next(s for s in self.task.steps if s["n"] == self.step)
        step["worker_checkpoint"] = {"history": history[-20:], "pending": pending}
        self.runtime.store.save(self.task)

    def run_command(self, command: str, cwd: str | None = None, timeout: float = 120, *, role: str = "") -> dict:
        from ..execution import passwords
        # A literal `sshpass -p SECRET` becomes `sshpass` before anything is
        # classified, recorded or shown for approval; the harness supplies it.
        command = passwords.sanitize_command(command)
        words = (split_commands(command) or [command])[0].split()
        self._stage("Running " + " ".join(words[:2]))
        cwd = str(Path(cwd or self.task.workspace).expanduser())
        assessment = classify(command, cwd, self.scope)
        destructive = approvals.destructive_assessment(assessment.reasons)
        if destructive:
            assessment.always_ask = True  # a human tap, whatever auto-approve says (core/approvals.py)
        decision = self._decide("command", command, assessment)
        record = {"command": command[:2000], "cwd": cwd, "step": self.step, "role": role,
                  "decision": decision.behavior, "risk": assessment.risk.name, "reasons": assessment.reasons[:4],
                  "at": time.time()}
        if decision.behavior == "deny":
            self.task.add_command(record)
            self.task.add_evidence("refused", f"refused `{command[:300]}`: {'; '.join(assessment.reasons)}",
                                   ok=False, step=self.step)
            self.runtime.store.save(self.task)
            return {"command": command, "decision": "deny", "risk": "BLOCKED", "exit_code": None,
                    "output": f"REFUSED by the harness (never allowed): {'; '.join(assessment.reasons)}"}
        if decision.behavior == "ask":
            self.task.add_command(record)
            self.runtime.store.save(self.task)
            request = self._request(decision, "command", command)
            if destructive:
                request["tap_only"] = True
            return {"command": command, "decision": "ask", "risk": assessment.risk.name, "exit_code": None,
                    "request": request,
                    "output": f"NEEDS USER APPROVAL ({assessment.risk.name}): {'; '.join(assessment.reasons)}"}
        stdin_text = None
        if assessment.risk >= Risk.HIGH and re.match(r"\s*sudo\s", command):
            # The phone's fingerprint screen asks who runs this sudo and why.
            from ..execution import approval_context
            approval_context.note(f"Task: {request_text(self.task.goal)}")
            command, stdin_text = _sudo(command)
        event = self.runtime._cancel.get(self.task.id)
        run_as, env = passwords.ssh_env(command, shell._base_env())
        installs = [t for v, t in _TOOLS_CLI.findall(command) if v == "install" and t]
        if len(installs) == 1 and decision.behavior == "allow" and assessment.always_ask:
            # The user just approved this exact install; the CLI must not ask twice.
            from ..execution.user_tools import APPROVED_ENV
            env[APPROVED_ENV] = installs[0]
        # Second layer (runtime/sandbox.py): what the command may read and
        # write follows the risk it was allowed at; HIGH (a human tap) runs as is.
        from . import sandbox
        policy = sandbox.for_command(command, assessment.risk, cwd=cwd, workspace=self.task.workspace,
                                     writes=assessment.writes)

        def execute(_key: str = "") -> shell.CommandResult:
            return shell.run(run_as, cwd, timeout=min(max(timeout, 1), 3600), stdin_text=stdin_text, cancel=event,
                             output_dir=self.runtime.store.dir / self.task.id, env=env, sandbox=policy)

        if assessment.risk >= Risk.ELEVATED:
            result = self._journaled_command(command, cwd, execute)
        else:
            result = execute()
        result.output = passwords.redact(result.output)  # e.g. a vault token a command printed
        self.task.tainted = True  # its output is untrusted (execution/provenance.py)
        if not result.ok and (hint := sandbox.explain_failure(result.output, policy)):
            result.output = f"{result.output.rstrip()}\n{hint}"
        data = result.as_dict()
        record.update(exit_code=result.exit_code, timed_out=result.timed_out, duration=round(result.duration, 2),
                      output=result.output[-3000:], full_output_path=result.full_output_path)
        self.task.add_command(record)
        ok, meaning = result.ok, ""
        if (not result.ok and re.search(r"(?<![\w-])(?:ssh|scp|rsync)\s", command) and not passwords.needs_password(command)
                and re.search(r"Permission denied \([^)]*password", result.output)):
            # 2026-09-27: the worker asked the user to install its key by hand
            # instead of using the password it had been told about.
            meaning = (" (this host wants a password: run it as `sshpass <the same command>` or install the key "
                       "with `ssh-copy-id -i <key.pub> user@host`; the harness types the password, never put it in "
                       "the command)")
        last = (split_commands(command) or [""])[-1].split()
        if result.exit_code == 1 and not result.output.strip() and last and last[0] in ("grep", "egrep", "rg", "pgrep"):
            ok, meaning = None, " (no match -- for this tool exit 1 with no output means nothing was found)"
        self.task.add_evidence("command", f"`{command[:300]}` -> exit {result.exit_code}{meaning}"
                               + (" (timed out)" if result.timed_out else "") + ": "
                               + (result.output if len(result.output) <= 1400
                                  else result.output[:800] + "\n…\n" + result.output[-600:]),
                               ok=ok, step=self.step)
        self.runtime.store.save(self.task)
        return {**data, "decision": "allow", "risk": assessment.risk.name}

    def _step_record(self) -> dict:
        return next((s for s in self.task.steps if s.get("n") == self.step), {})

    def _journaled_command(self, command: str, cwd: str, execute) -> shell.CommandResult:
        """An ELEVATED-or-above command, at most once (core/effects.py). Its
        origin is the task, the step and how many such commands this step
        has finished: a deliberate repeat after one finished runs again; the
        same command after a crash mid-run is not run a second time."""
        from ..core import effects
        step = self._step_record()
        done = int(step.get("effects_done") or 0)

        def stored(r: shell.CommandResult) -> dict:
            return {**r.as_dict(effects.RESULT_CHARS), "duration": r.duration}

        def loaded(d: dict) -> shell.CommandResult:
            return shell.CommandResult(str(d.get("command") or command), str(d.get("cwd") or cwd),
                                       d.get("exit_code"), str(d.get("output") or ""), float(d.get("duration") or 0),
                                       timed_out=bool(d.get("timed_out")), cancelled=bool(d.get("cancelled")),
                                       truncated=bool(d.get("truncated")), full_output_path=d.get("full_output_path"),
                                       error=d.get("error"))

        def unsure(effect, state: str) -> shell.CommandResult:
            info = effects.describe(effect, state)
            return shell.CommandResult(command, cwd, None, info["message"], 0.0, error=f"effect {state}")

        result = effects.perform("command", "run_command", {"command": command, "cwd": cwd},
                                 execute, target=command[:300],
                                 origin_id=f"task:{self.task.id}:step{self.step}:{done}",
                                 correlation_id=self.task.id, succeeded=lambda r: r.exit_code == 0,
                                 store=stored, load=loaded, unsure=unsure)
        if step and result.error not in ("effect uncertain", "effect in_progress"):
            step["effects_done"] = done + 1
        return result

    def launch(self, command: str) -> dict:
        assessment = classify(command, self.task.workspace, self.scope)
        assessment.raise_to(Risk.NORMAL, "")
        decision = self._decide("launch", command, assessment)
        record = {"command": f"[launch] {command[:1000]}", "cwd": self.task.workspace, "step": self.step,
                  "decision": decision.behavior, "risk": assessment.risk.name, "at": time.time()}
        self.task.add_command(record)
        if decision.behavior == "deny":
            return {"ok": False, "decision": "deny", "message": "; ".join(assessment.reasons)}
        if decision.behavior == "ask":
            self.runtime.store.save(self.task)
            return {"ok": False, "decision": "ask", "request": self._request(decision, "launch", command)}
        log_dir = self.runtime.store.dir / self.task.id
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"launch-{int(time.time())}.log"
        try:
            with open(log_path, "w") as out:
                proc = subprocess.Popen(["bash", "-c", command], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                        start_new_session=True, env=shell._base_env(), cwd=self.task.workspace)
            time.sleep(2)
            code = proc.poll()
        except OSError as exc:
            return {"ok": False, "decision": "allow", "message": str(exc)}
        text = log_path.read_text(errors="replace")[-800:] if log_path.exists() else ""
        ok = code in (None, 0)
        message = ("started (still running)" if code is None else f"exited {code}") + (f": {text}" if text else "")
        self.task.add_evidence("launch", f"launched `{command[:200]}`: {message[:600]}", ok=ok, step=self.step)
        self.runtime.store.save(self.task)
        return {"ok": ok, "decision": "allow", "message": message}

    def desktop(self, goal: str) -> dict:
        runner = self.runtime._desktop_task
        if runner is None:
            from ..execution.desktop_jev import desktop_task as runner
        try:
            result = runner({"goal": goal})
            data = json.loads(result.message) if result.message.strip().startswith("{") else {
                "status": "completed" if result.ok else "failed", "reason": result.message}
        except Exception as exc:  # noqa: BLE001
            data = {"status": "failed", "reason": f"desktop loop unavailable: {exc}"}
        self.task.add_evidence("desktop", f"Jev desktop loop for '{goal[:200]}': {data.get('status')} "
                               f"verified_steps={json.dumps(data.get('verified_steps', []))[:500]} "
                               f"reason={str(data.get('reason'))[:200]}",
                               ok=data.get("status") == "completed", step=self.step)
        self.runtime.store.save(self.task)
        return data

    def screen(self, question: str) -> dict:
        result = self._action_runner()("describe_screen", {"question": question[:500]})
        self.task.tainted = True
        self.task.add_evidence("screen", f"screen check '{question[:150]}': {provenance.clip(result.message, 800)}",
                               source="vision", ok=result.ok, step=self.step)
        self.runtime.store.save(self.task)
        return {"ok": result.ok, "text": provenance.clip(result.message, 3000)}

    def action(self, name: str, args: dict) -> dict:
        label = str(args.get("goal") or args.get("request") or args.get("url") or name).replace("\n", " ")
        self._stage(label[:110])
        assessment = Assessment(_ACTION_RISK.get(name, Risk.NORMAL))
        subject = f"{name} {json.dumps(args, sort_keys=True)}"
        # The one approval policy (core/approvals.py): a send asks the user
        # with its exact arguments; a destructive action or a target from
        # untrusted content needs a tap, whatever auto-approve says.
        verdict = approvals.decide(name, args, tainted=self.task.tainted, said=self._user_said())
        why = verdict.reason if verdict.level == approvals.TAP else None
        if verdict.level == approvals.DENY:
            assessment.raise_to(Risk.BLOCKED, verdict.reason)
        elif verdict.level != approvals.AUTO:
            assessment.always_ask = True
            assessment.reasons.append(verdict.reason)
        decision = self._decide("action", subject, assessment)
        if decision.behavior != "allow":
            request = self._request(decision, "action", subject)
            if why is not None:
                request["tap_only"] = True
            return {"ok": False, "decision": decision.behavior, "request": request}
        granted = decision.fingerprint in self.task.grants
        # What the tool's own side effects are caused by (core/effects.py):
        # this task, step and call.
        from ..core import effects
        step = self._step_record()
        calls = int(step.get("actions_done") or 0)
        with effects.origin(f"task:{self.task.id}:step{self.step}:action{calls}"), \
                approvals.approved_call(name, args, "task") if granted else contextlib.nullcontext():
            result = self._action_runner()(name, args)
        if getattr(result, "untrusted", False):
            self.task.tainted = True
        if step:
            step["actions_done"] = calls + 1
        self.task.add_command({"command": f"[tool] {subject[:500]}", "step": self.step, "decision": "allow",
                               "risk": assessment.risk.name, "exit_code": 0 if result.ok else 1,
                               "output": result.message[-1500:], "at": time.time()})
        self.task.add_evidence("action", f"{subject[:200]} -> {'ok' if result.ok else 'failed'}: {provenance.clip(result.message, 600)}",
                               ok=result.ok, step=self.step)
        self.runtime.store.save(self.task)
        return {"ok": result.ok, "decision": "allow", "message": result.message}

    def _user_said(self) -> str:
        """The user's own words for this task: the goal, their answers, and
        what they approved (execution/provenance.py's taint check)."""
        approved = [n for n in self.task.notes if n.startswith("User approved")]
        return "\n".join([self.task.goal, *self.task.answers, *approved])

    def _action_runner(self):
        if self.runtime._run_action is not None:
            return self.runtime._run_action
        # On the ToolRunner: each call has its tool's deadline, and cancelling
        # the task stops the calls it has running (Runtime.cancel).
        from ..execution import runner
        owner = f"task:{self.task.id}"
        return lambda name, args: runner.default_runner().run(name, args, owner=owner)

    def check_external(self, kind: str, subject: str, risk, reasons: list[str]) -> dict:
        assessment = Assessment(Risk(risk) if not isinstance(risk, Risk) else risk, list(reasons))
        decision = self._decide(kind, subject, assessment)
        self.task.add_command({"command": f"[{kind}] {subject[:500]}", "step": self.step,
                               "decision": decision.behavior, "risk": assessment.risk.name,
                               "reasons": reasons[:4], "at": time.time()})
        self.runtime.store.save(self.task)
        out = {"decision": decision.behavior, "risk": assessment.risk.name}
        if decision.behavior == "ask":
            out["request"] = self._request(decision, kind, subject)
        return out

    def run_external(self, argv: list[str], cwd: str, timeout: float, stdin_text: str | None = None) -> dict:
        # Vendor CLIs keep their own credentials; the environment is the
        # desktop one (not scrubbed) so their auth works.
        try:
            from ..execution.actions import _desktop_env
            env = _desktop_env()
        except Exception:  # noqa: BLE001
            env = dict(os.environ)
        env["NO_COLOR"] = "1"
        result = shell.run(argv, cwd, timeout=timeout, stdin_text=stdin_text,
                           cancel=self.runtime._cancel.get(self.task.id), env=env,
                           output_dir=self.runtime.store.dir / self.task.id)
        from ..execution.passwords import redact
        result.output = redact(result.output)
        self.task.add_command({"command": f"[executor] {' '.join(argv[:6])} …", "cwd": cwd, "step": self.step,
                               "decision": "allow", "exit_code": result.exit_code, "timed_out": result.timed_out,
                               "duration": round(result.duration, 1), "output": result.output[-1500:],
                               "full_output_path": result.full_output_path, "at": time.time()})
        self.runtime.store.save(self.task)
        return result.as_dict(max_chars=40_000)

    def note(self, text: str) -> None:
        self.task.add_note(text)


# ------------------------------------------------------------------ helpers
_MASKING_TAIL = re.compile(r"^(?:echo|printf|true|:|exit\s+0)\b")


def masked_exit_code(command: str) -> bool:
    """True when a check can never fail: `x; echo $?`, `x || true`, `x || echo no`."""
    parts = split_commands(command)
    if len(parts) > 1 and _MASKING_TAIL.match(parts[-1].strip()) and re.search(r";|\|\||\n", command):
        return True
    return bool(re.search(r"\|\|\s*(?:true|:|echo|printf|exit\s+0)\b", command))


def _config(name: str, default):
    try:
        from ..config import load_config
        return getattr(load_config(), name, default)
    except Exception:  # noqa: BLE001
        return default


def _sudo(command: str) -> tuple[str, str | None]:
    """An approved root command: feed the user-enabled keyring password to
    `sudo -S`, never through a model or a log. Without Sudo Access enabled,
    `sudo -n` fails fast instead of hanging on a prompt. With Flux's phone
    approval on for sudo, no password is fed at all: sudo asks the phone for
    a fingerprint (execution/flux_approve.py), and a denial or no answer
    fails on the empty stdin."""
    try:
        from ..config import load_config
        from ..execution import flux_approve, sudo_approval
        if flux_approve.sudo_ready():
            return re.sub(r"^\s*sudo\s", "sudo -S -p '' ", command, count=1), ""
        if load_config().sudo_access_enabled:
            password = sudo_approval.retrieve()
            if password:
                return re.sub(r"^\s*sudo\s", "sudo -S -p '' ", command, count=1), password + "\n"
    except Exception:  # noqa: BLE001
        pass
    return re.sub(r"^\s*sudo\s", "sudo -n ", command, count=1), None


def _notify(runtime: TaskRuntime, task: Task, event: str) -> None:
    title = NOTICE_TITLES.get(event)
    if not title:
        return
    if event == "waiting_approval" and task.pending_approval:
        req = task.pending_approval
        if req.get("sudo_action"):
            body = (f"Task: {req.get('task_summary', request_text(task.goal))[:240]}\n\n"
                    f"Sudo action: {req['sudo_action'][:300]}\n{'; '.join(req.get('reasons', []))}")
        else:
            body = f"{request_text(task.goal)[:120]}\n\n{req.get('risk')}: {req.get('subject', '')[:300]}\n{'; '.join(req.get('reasons', []))}"
        fingerprint = _flux_approval_available()
        if fingerprint:
            body += "\n\nOr approve it on your phone with your fingerprint (Flux)."
        threading.Thread(target=_approval_prompt, args=(runtime, task.id, req.get("fingerprint"), title, body),
                         daemon=True).start()
        # The user, 2026-09-30: Flux was spammed with approvals, and taps
        # opened the phone bridge's web page, which Flux no longer uses. With
        # fingerprint approval the Flux prompt is the one phone ping;
        # otherwise the phone is told to approve on the desktop.
        if fingerprint:
            threading.Thread(target=_fingerprint_prompt, args=(runtime, task, req.get("fingerprint")),
                             daemon=True).start()
        else:
            _phone(title, f"{request_text(task.goal)[:120]}\n{req.get('risk')}: {str(req.get('subject', ''))[:200]}\n"
                   "Approve it on the desktop.", kind="approval", conversation=task.conversation)
        return
    title, body, fingerprint = notice(task, event)
    # Notifications are delivery attempts, not task transitions. Store a
    # stable fingerprint before delivery so a reconnect/restart cannot repeat
    # an unchanged task question or outcome indefinitely. The step number
    # means a later, genuinely new question is still shown.
    if task.notifications.get(event) == fingerprint:
        return
    task.notifications[event] = fingerprint
    runtime.store.save(task)
    from ..core import outbox
    # One message (core/outbox.py): said in an open conversation, else shown
    # where the user is. The daemon's task listener (runtime/service.py)
    # sends the same key first when a conversation can say it.
    outbox.send(outbox.Message(
        outbox.QUESTION if event == "waiting_user" else outbox.RESULT, title, body, origin_id=f"task-{task.id}",
        dedupe_key=outbox_key(task, fingerprint), conversation=task.conversation or "",
        # A click on a finished task's notification (or its Open button) shows the whole task.
        actions={"default": "Open task"} if event in TERMINAL else None,
        on_action=(lambda choice, task_id=task.id: _open_task(task_id) if choice == "default" else None)))
    if event in TERMINAL:
        _deliver_artifacts(task)


def _deliver_artifacts(task: Task) -> None:
    """Each file the task made for the user, once (core/outbox.py attachment)."""
    from ..core import outbox, outputs
    from ..core.conversations import MAX_ATTACHMENTS_PER_TURN
    try:
        base = outputs.task_dir(task.id).resolve()
    except ValueError:
        return
    for path in task.artifacts[:MAX_ATTACHMENTS_PER_TURN]:
        resolved = Path(path).resolve()
        if base not in resolved.parents:
            continue  # only what the task wrote into its own outputs folder
        try:
            outbox.attachment(str(resolved), origin_id=f"task-{task.id}", conversation=task.conversation or "",
                              label=request_text(task.goal)[:120], untrusted=bool(task.tainted),
                              person=task.person or None)
        except Exception:  # noqa: BLE001 -- one file must not stop the others
            log.warning("could not deliver task file %s", resolved.name, exc_info=True)


NOTICE_TITLES = {"waiting_approval": "Omarchy task needs approval", "waiting_user": "Omarchy task has a question",
                 CERTIFIED: "Omarchy task done ✓", UNVERIFIED: "Omarchy task finished (unverified)",
                 FAILED: "Omarchy task failed"}


def notice(task: Task, event: str) -> tuple[str, str, str]:
    """(title, body, fingerprint) of a task question or outcome notice."""
    title = NOTICE_TITLES.get(event, "")
    said = (task.question if event == "waiting_user" else task.result) or ""
    body = f"{request_text(task.goal)[:120]}\n\n{said[:400]}"
    steps = len(getattr(task, "steps", None) or [])
    fingerprint = hashlib.sha1(f"{event}\x1f{steps}\x1f{body}".encode()).hexdigest()
    return title, body, fingerprint


def outbox_key(task: Task, fingerprint: str) -> str:
    """The outbox's dedupe key of one task notice: one delivery, whoever sends it."""
    return f"task:{task.id}:{fingerprint}"


def _open_task(task_id: str) -> None:
    show = shlex.join([sys.executable, "-m", "omarchy_ai.cli.task", "show", task_id])
    try:
        subprocess.Popen(["omarchy-launch-floating-terminal-with-presentation", show],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        log.warning("could not open task %s in a terminal", task_id, exc_info=True)


def _phone(title: str, body: str, *, kind: str = "other", conversation: str = "") -> None:
    """An approval's phone notice (its desktop prompt is the approval channel
    itself): through the outbox, to the phone and its chat."""
    try:
        from ..core import outbox
        outbox.send(outbox.Message(outbox.APPROVAL, title, body, channels=(outbox.PUSH, outbox.CHAT),
                                   push_kind=kind, conversation=conversation))
    except Exception:  # noqa: BLE001 -- the desktop prompt must still go out
        log.debug("phone notice unavailable", exc_info=True)


def _allowed_now(task: Task, request: dict) -> bool:
    """Whether the current rules would run this request without asking. Only
    coding-agent requests are re-assessed: their subject names everything the
    risk depends on; a command's depends on the step's working directory."""
    if request.get("kind") != "executor":
        return False
    name, mode, workspace = (str(request.get("subject") or "").split(":", 2) + ["", ""])[:3]
    if mode not in ("write", "read-only", "unsandboxed") or not workspace or not Path(workspace).is_dir():
        return False
    # Older versions created a blanket HIGH approval before rerunning a
    # coding agent with its non-interactive flag. Coding agents now always
    # use that flag, so stale requests are reassessed as ordinary write work.
    risk, reasons = coding_agent_risk(Path(workspace), mode != "read-only")
    decision = decide("executor", str(request.get("subject")), Assessment(risk, reasons),
                      auto_approve=Risk.parse(task.auto_approve, Risk.NORMAL), grants=set(task.grants))
    return decision.allowed


def _flux_approval_available() -> bool:
    try:
        from ..execution import flux_approve
        return flux_approve.available()
    except Exception:  # noqa: BLE001
        return False


def _fingerprint_fields(task: Task) -> tuple[str, str, str]:
    """(the request, who asks, why) for the phone's fingerprint screen. Until
    2026-10-05 every task showed "Omarchy AI <risk> step: <goal>", whatever
    the step and whoever was doing it."""
    request = task.pending_approval or {}
    what = str(request.get("subject") or task.goal)
    steps = getattr(task, "steps", None) or []
    executor = str((getattr(task, "next_dispatch", None) or {}).get("executor")
                   or (steps[-1].get("executor") if steps else "") or getattr(task, "agent", "")).upper()
    if request.get("kind") == "rollback":
        by = "jev"  # Jev directs a rollback itself
    else:
        by = "codex" if "CODEX" in executor else "claude" if "CLAUDE" in executor else "omarchy"
    reasons = "; ".join(str(r) for r in request.get("reasons") or [])
    risk = str(request.get("risk") or "").lower()
    why = f"Task: {request_text(task.goal)}" + (f" ({risk} risk: {reasons})" if reasons else f" ({risk} risk)" if risk else "")
    return what, by, why


def _fingerprint_prompt(runtime: TaskRuntime, task: Task, fingerprint: str) -> None:
    """The phone enrolled in Flux shows the request, and only a fingerprint
    signature that the root-owned key verifies approves it: a human check
    like the notification's button and the PIN, which the model cannot fake.
    No answer leaves the task waiting for the other channels."""
    from ..execution import flux_approve
    request = task.pending_approval or {}
    ok, reason = flux_approve.request(*_fingerprint_fields(task))
    log.info("task %s: fingerprint approval: %s", task.id, reason)
    if ok is None:
        return
    current = runtime.store.load(task.id)
    if (not current or current.status != WAITING_APPROVAL
            or (current.pending_approval or {}).get("fingerprint") != fingerprint):
        return
    runtime.respond(task.id, approve=ok, channel="fingerprint")


def _approval_prompt(runtime: TaskRuntime, task_id: str, fingerprint: str, title: str, body: str) -> None:
    """A real human click on the desktop notification is an approval channel
    the model cannot fake (a voice approval is relayed by the live model);
    the paired phone's PIN-checked card (phone/server.py) is the other."""
    try:
        proc = subprocess.run(["notify-send", "-a", "Omarchy AI", "-u", "critical", "-A", "approve=Approve",
                               "-A", "deny=Deny", title, body], capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired):
        return
    choice = proc.stdout.strip()
    task = runtime.store.load(task_id)
    if not task or task.status != WAITING_APPROVAL or (task.pending_approval or {}).get("fingerprint") != fingerprint:
        return
    if choice in ("approve", "deny"):
        runtime.respond(task_id, approve=choice == "approve", channel="notification")
