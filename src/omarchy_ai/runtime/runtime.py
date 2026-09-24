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

import hashlib
import json
import logging
import os
from pathlib import Path
import re
import subprocess
import threading
import time
from typing import Callable

from . import shell
from .control import ControlPlane
from .executors import Executor, default_executors
from .executors.base import (BLOCKED, DIAGNOSE, DONE, FAILED as R_FAILED, IMPLEMENT, NEEDS_APPROVAL,
                             NEEDS_CODE_CHANGE, NEEDS_USER, REVIEW, TEST, WORK, Assignment, Report)
from .llm import WorkerModel, WorkerModelError
from .permissions import Assessment, Risk, Scope, classify, decide, split_commands
from .task import (ACTIVE, CANCELLED, CERTIFIED, FAILED, INTERRUPTED, RUNNING, TERMINAL, UNVERIFIED,
                   WAITING_APPROVAL, WAITING_USER, Task, TaskStore, new_id, owned_elsewhere, owner_alive,
                   this_process)

log = logging.getLogger(__name__)

CODING = {"CLAUDE_CODE", "CODEX"}
VALIDATED_P = 0.6          # Jev must think the test plan proves the behavior
CERTIFY_P = 0.8            # Jev certification threshold
MAX_CERT_REJECTIONS = 3
MAX_PLAN_ATTEMPTS = 2
MAX_CONSECUTIVE_RETRIES = 2

# Risk of the assistant's existing tools when a direct step calls them.
_ACTION_RISK = {"list_windows": Risk.LOW, "list_bar_icons": Risk.LOW, "battery_status": Risk.LOW,
                "list_cast_targets": Risk.LOW, "screenshot": Risk.LOW}

PLAN_PROMPT = """You turn a user's request to Omarchy AI (a voice/desktop assistant on an Arch Linux + Hyprland
machine) into a task objective with checkable acceptance criteria. Do not solve the task.
Reply with one JSON object:
{"objective": "<one or two sentences: what must be true when done>",
 "acceptance_criteria": ["<observable, checkable condition>", ... 1 to 5 items],
 "verification_ideas": ["<how someone other than the worker could check it on this machine>", ...]}
Criteria must be about the real outcome (the device works, the test passes, the file exists with X), not about
effort. For a question the user wants answered, the criterion is that the answer is supported by evidence.
Never add deliverables the user did not ask for (no reports, notes or saved files unless requested)."""


class TaskRuntime:
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
        self._mark_interrupted()

    # ------------------------------------------------------------------ API
    def start(self, goal: str, workspace: str | None = None, *, source: str = "cli", background: bool = True,
              auto_approve: str | None = None, max_steps: int | None = None) -> Task:
        goal = (goal or "").strip()
        if not goal:
            raise ValueError("a task needs a goal")
        ws = Path(workspace or os.getcwd()).expanduser().resolve()
        if not ws.is_dir():
            raise ValueError(f"workspace {ws} is not a directory")
        task = Task(id=new_id(), goal=goal[:4000], workspace=str(ws), source=source,
                    auto_approve=(auto_approve or self.default_auto_approve).upper())
        task.budget["max_steps"] = int(max_steps or _config("task_max_steps", 12))
        task.budget["max_seconds"] = int(_config("task_max_minutes", 60)) * 60
        self.store.save(task)
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
        wanted = WAITING_APPROVAL if approve is not None else WAITING_USER
        task = self._get(task_id, {wanted})
        if task is None:
            if task_id and self.store.load(task_id):
                current = self.store.load(task_id)
                return {"ok": False, "message": f"task {task_id} is {current.status}, not waiting for "
                                                + ("approval" if wanted == WAITING_APPROVAL else "an answer")}
            return {"ok": False, "message": "no task is waiting for " + ("approval" if wanted == WAITING_APPROVAL
                                                                         else "an answer")}
        if owned_elsewhere(task):
            return {"ok": False, "message": f"task {task.id} is being run by another process (pid {task.owner['pid']})"}
        if task.status == WAITING_APPROVAL:
            request = task.pending_approval or {}
            if approve and channel == "voice" and Risk.parse(request.get("risk"), Risk.HIGH) >= Risk.HIGH:
                return {"ok": False, "message": (
                    f"'{request.get('subject')}' is HIGH risk ({', '.join(request.get('reasons', []))}). It can only "
                    "be approved with the desktop notification's Approve button or `omarchy-ai-task approve "
                    f"{task.id}` -- not by voice.")}
            paused = next((s for s in task.steps if s["n"] == request.get("step")), None)
            if approve:
                task.grants.append(request.get("fingerprint", ""))
                task.add_note(f"User approved {request.get('kind')}: {request.get('subject')}")
                if task.next_dispatch:
                    task.next_dispatch.setdefault("context", {})["user_approved"] = request.get("subject")
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
            if task.next_dispatch:
                task.next_dispatch.setdefault("context", {})["user_answer"] = answer[:2000]
            task.question = None
        self.store.save(task)
        if task.status == CANCELLED:
            return {"ok": False, "message": f"task {task.id} was cancelled"}
        self._launch(task, background)
        return {"ok": True, "message": f"resumed task {task.id}", "task": task.summary()}

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

        def mark(t):
            if t.status not in TERMINAL:
                t.status = CANCELLED
                t.result = "Cancelled by the user."
                t.pending_approval = None
                t.next_dispatch = None
        self.store.update(task.id, mark)
        return {"ok": True, "message": f"cancelled {task.id}"}

    def status(self, task_id: str | None = None) -> dict | None:
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

    def _mark_interrupted(self) -> None:
        # A task left "running" by a previous process was cut off mid-step.
        # Only when its owning process is gone: the CLI and the daemon share the
        # task files, and each must leave the other's live tasks alone.
        def mark(t):
            if t.status in ACTIVE and not owner_alive(t.owner):
                t.status = INTERRUPTED
                t.owner = None
                t.add_note("The runtime restarted while this task was running; its last step may be incomplete.")
        for task in self.store.list(50):
            if task.status in ACTIVE and not owner_alive(task.owner) and task.id not in self._threads:
                self.store.update(task.id, mark)

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
            thread = threading.Thread(target=self._drive, args=(task.id,), name=f"task-{task.id}", daemon=True)
            self._threads[task.id] = thread
            thread.start()

    def _emit(self, task: Task, event: str) -> None:
        for listener in list(self.listeners):
            try:
                listener(task, event)
            except Exception:  # noqa: BLE001
                log.warning("task listener failed", exc_info=True)
        if self.notify:
            _notify(self, task, event)

    # -------------------------------------------------------------- driver
    def _drive(self, task_id: str) -> None:
        task = self.store.load(task_id)
        if task is None:
            return
        try:
            self._loop(task)
        except Exception as exc:  # noqa: BLE001 -- a crash must leave an honest record
            log.exception("task %s crashed", task.id)
            task.add_error(f"runtime crash: {type(exc).__name__}: {exc}")
            self._finish(task, FAILED, f"The task runtime hit an internal error: {exc}")
        finally:
            task.owner = None
            self.store.save(task)

    def _loop(self, task: Task) -> None:
        cancel = self._cancel.setdefault(task.id, threading.Event())
        task.status = RUNNING
        task.owner = this_process()
        self.store.save(task)
        if task.status == CANCELLED:  # cancelled between launch and start
            return
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

    # ---------------------------------------------------------------- plan
    def _plan(self, task: Task) -> None:
        task.phase = "plan"
        try:
            plan = self.planner.complete(PLAN_PROMPT, {"request": task.goal, "workspace": task.workspace}, timeout=45)
            task.objective = str(plan.get("objective") or task.goal)[:1000]
            task.plan = [str(c)[:300] for c in (plan.get("acceptance_criteria") or []) if c][:5] or [task.goal[:300]]
            ideas = [str(i)[:300] for i in (plan.get("verification_ideas") or [])][:4]
            if ideas:
                task.add_note("Verification ideas (planner): " + " | ".join(ideas))
        except WorkerModelError as exc:
            task.add_error(f"planner unavailable: {exc}")
            task.objective, task.plan = task.goal[:1000], [task.goal[:300]]
        self.store.save(task)

    # --------------------------------------------------------------- route
    def _candidates(self, *, roles: set[str] | None = None, exclude: set[str] = frozenset()) -> dict[str, str]:
        out = {}
        for name, info in self.available().items():
            executor = self.executors[name]
            if not info["available"] or name in exclude:
                continue
            if roles is not None and not (executor.roles & roles):
                continue
            out[name] = executor.description
        return out

    def _route(self, task: Task) -> bool:
        task.phase = "route"
        candidates = self._candidates(exclude={"TEST_AGENT", "REVIEW_AGENT"})
        if not candidates:
            self._finish(task, FAILED, "No executor is available (the worker model and coding agents are all unreachable).")
            return False
        pick, needs_code = self.control.route(task, candidates)
        task.needs_code = needs_code
        task.add_decision("route", pick.value, pick.p, {"needs_code_change": needs_code, "fallback": pick.fallback,
                                                        "probabilities": pick.probabilities})
        if pick.value == "ASK_USER":
            self._ask_user(task, "Before I start, can you tell me more precisely what you want done?")
            return False
        task.next_dispatch = self._assignment(task, pick.value, "ROUTE")
        self.store.save(task)
        return True

    # ------------------------------------------------------------ dispatch
    def _assignment(self, task: Task, executor: str, directive: str, last: dict | None = None) -> dict:
        last = last or (task.steps[-1] if task.steps else None)
        last_claim = (last or {}).get("claim", "")[:1200]
        needs_code = (task.needs_code or 0) >= 0.5 or bool(last and last.get("report_status") == NEEDS_CODE_CHANGE)
        if executor == "TEST_AGENT":
            role = TEST
        elif executor == "REVIEW_AGENT" or directive == "REQUEST_REVIEW":
            role = REVIEW
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
        if task.reviews:
            context["latest_review"] = task.reviews[-1]
        context.update(dispatch.get("context") or {})
        return context

    def _dispatch(self, task: Task, dispatch: dict) -> bool:
        """Run one executor assignment. False when the task must wait."""
        name = dispatch["executor"]
        executor = self.executors.get(name)
        if executor is None:
            task.add_error(f"unknown executor {name}")
            task.next_dispatch = None
            return True
        task.phase = f"{name.lower()}:{dispatch['role']}"
        if dispatch["write"] and name in CODING:
            self._checkpoint(task)
        step = task.add_step(name, dispatch["instructions"], dispatch["role"])
        step["directive"] = dispatch.get("directive")
        self.store.save(task)
        assignment = Assignment(task_id=task.id, role=dispatch["role"], goal=task.goal,
                                instructions=dispatch["instructions"], workspace=task.workspace,
                                context=self._context_for(task, dispatch), write_access=dispatch["write"],
                                timeout=dispatch.get("timeout", 600))
        ctx = WorkContextImpl(self, task, step["n"])
        try:
            report = executor.run(assignment, ctx)
        except Exception as exc:  # noqa: BLE001
            log.exception("executor %s crashed", name)
            report = Report(R_FAILED, claim=f"{name} crashed: {type(exc).__name__}: {exc}")
        outcome = {DONE: "done", NEEDS_APPROVAL: "waiting_approval", NEEDS_USER: "waiting_user",
                   NEEDS_CODE_CHANGE: "done", BLOCKED: "blocked"}.get(report.status, "failed")
        task.finish_step(step, outcome, report.claim)
        step["report_status"] = report.status
        step["findings"] = [f[:400] for f in report.findings][:10]
        step["meta"] = {k: v for k, v in report.meta.items() if k != "history"}
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
        return True

    # ------------------------------------------------------------- direct
    def _allowed_directives(self, task: Task) -> list[str]:
        last = task.steps[-1] if task.steps else None
        allowed = ["CONTINUE", "SPAWN_SUBAGENT", "ASK_USER", "FAIL"]
        if len(self._candidates(exclude={"TEST_AGENT", "REVIEW_AGENT"})) > 1:
            allowed.append("CHANGE_EXECUTOR")
        if last and last["outcome"] in ("failed", "blocked") and self._consecutive(task) <= MAX_CONSECUTIVE_RETRIES:
            allowed.append("RETRY")
        changed = bool(task.files_modified)
        if changed and self._reviewers(task) and not self._reviewed_since_change(task):
            allowed.append("REQUEST_REVIEW")
        validated = (task.test_plan.get("validated") or 0) >= VALIDATED_P
        if validated and not self._tests_since_change(task):
            allowed.append("RUN_TESTS")
        if not validated and task.test_plan.get("attempts", 0) < MAX_PLAN_ATTEMPTS and "TEST_AGENT" in self._candidates():
            allowed.append("REQUEST_MORE_TESTS")
        if changed and task.checkpoints:
            allowed.append("ROLLBACK")
        ok, reason = self._certification_gate(task)
        rejected_at = task.budget.get("cert_rejected_at", 0)
        if ok and rejected_at and not any(e["at"] > rejected_at for e in task.evidence if e["source"] != "executor"):
            # Live run 2026-09-23: after a rejection Jev picked CERTIFY again three
            # times on unchanged evidence. Asking again needs new evidence first.
            ok, reason = False, "certification was refused and no new evidence has been gathered since"
        if ok:
            allowed.append("CERTIFY")
        else:
            task.certification["gate"] = reason
        return allowed

    def _direct(self, task: Task) -> bool:
        task.phase = "direct"
        allowed = self._allowed_directives(task)
        # The test and review subagents are reached only through REQUEST_MORE_TESTS
        # and REQUEST_REVIEW (live run 2026-09-23: CONTINUE -> TEST_AGENT looped
        # nine times past the test-plan attempt limit).
        executors = self._candidates(exclude={"TEST_AGENT", "REVIEW_AGENT"})
        directive, pick = self.control.direct(task, allowed, executors)
        task.add_decision("direct", directive.value, directive.p,
                          {"executor": pick.value, "executor_p": pick.p, "allowed": allowed, "fallback": directive.fallback})
        self.store.save(task)
        d = directive.value
        last = task.steps[-1] if task.steps else None
        if d == "FAIL":
            done = [s for s in task.steps if s["outcome"] not in ("waiting_approval", "paused", "declined")]
            reason = (done[-1]["claim"] if done else "") or "no further progress was possible"
            self._finish(task, FAILED, f"I could not complete this. {reason[:600]}")
            return False
        if d == "ASK_USER":
            question = (last or {}).get("claim") or task.goal
            self._ask_user(task, f"I need your input to continue: {question[:600]}")
            return False
        if d == "CERTIFY":
            return self._certify(task)
        if d == "RUN_TESTS":
            self._run_tests(task)
            return True
        if d == "ROLLBACK":
            return self._rollback(task)
        if d == "REQUEST_MORE_TESTS":
            task.test_plan["attempts"] = task.test_plan.get("attempts", 0) + 1
            task.next_dispatch = self._assignment(task, "TEST_AGENT", d)
            return True
        if d == "REQUEST_REVIEW":
            reviewers = self._reviewers(task)
            name = pick.value if pick.value in reviewers else reviewers[0]
            task.next_dispatch = self._assignment(task, name, d)
            return True
        prev = (last or {}).get("executor")
        name = pick.value if pick.value in executors else (prev or "SYSTEM_AGENT")
        if d == "RETRY":
            name = prev or name
        elif d == "CHANGE_EXECUTOR" and name == prev:
            ranked = sorted((pick.probabilities or {}).items(), key=lambda kv: -kv[1])
            name = next((n for n, _ in ranked if n != prev and n in executors and n not in ("TEST_AGENT", "REVIEW_AGENT")),
                        next((n for n in executors if n != prev), name))
        elif d == "SPAWN_SUBAGENT" and self.executors.get(name, self.executors.get("SYSTEM_AGENT")).kind != "internal":
            name = "SYSTEM_AGENT" if "SYSTEM_AGENT" in executors else name
        task.next_dispatch = self._assignment(task, name, d)
        return True

    def _consecutive(self, task: Task) -> int:
        """How many times in a row the last executor has run."""
        count, last = 0, task.steps[-1]["executor"] if task.steps else None
        for step in reversed(task.steps):
            if step["executor"] != last:
                break
            count += 1
        return count

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

    def _reviewers(self, task: Task) -> list[str]:
        writers = {s["executor"] for s in task.steps if s["role"] in (IMPLEMENT, WORK)}
        available = self._candidates(roles={REVIEW})
        independent = [n for n in available if n in CODING and n not in writers]
        return independent + (["REVIEW_AGENT"] if "REVIEW_AGENT" in available else [])

    def _reviewed_since_change(self, task: Task) -> bool:
        return any(r["at"] >= task.budget.get("changed_at", 0) for r in task.reviews)

    # -------------------------------------------------------- certification
    def _certification_gate(self, task: Task) -> tuple[bool, str]:
        """Code-owned preconditions before Jev may even be asked to certify."""
        if not task.steps:
            return False, "no work has been done"
        dispatched = [s for s in task.steps if s["outcome"] not in ("waiting_approval", "paused", "declined")]
        if dispatched and dispatched[-1]["outcome"] not in ("done",):
            return False, f"the last step ended {dispatched[-1]['outcome']}"
        if task.pending_approval:
            return False, "a permission request is pending"
        changed_at = task.budget.get("changed_at", 0)
        fresh = [e for e in task.evidence if e["source"] == "harness" and e["ok"] and e["at"] >= changed_at]
        if not fresh:
            return False, "no independent evidence observed after the last change"
        if task.files_modified:
            tests = self._tests_since_change(task)
            if any(t["exit_code"] != 0 for t in tests):
                return False, "a verification command failed after the last change"
            reviews = [r for r in task.reviews if r["at"] >= changed_at]
            if any(r["verdict"] == "fail" for r in reviews):
                return False, "the latest review failed the change"
            validated = (task.test_plan.get("validated") or 0) >= VALIDATED_P
            reviewed_ok = any(r["verdict"] == "pass" for r in reviews)
            if not (tests and validated) and not (reviewed_ok and task.test_plan.get("attempts", 0) >= MAX_PLAN_ATTEMPTS):
                return False, "changed files have not been verified by validated tests run after the change"
        return True, ""

    def _certify(self, task: Task) -> bool:
        task.phase = "certify"
        ok, reason = self._certification_gate(task)
        if not ok:
            task.add_note(f"Certification refused by the harness: {reason}")
            return True
        p, gap = self.control.certify(task)
        task.add_decision("certify", p is not None and p >= CERTIFY_P, p, {"gap": gap.value if gap else None})
        if p is None:
            self._finish(task, UNVERIFIED, "The work appears finished, but Jev was unavailable to certify it, so it "
                                           "is not verified.")
            return False
        if p >= CERTIFY_P:
            task.certification = {"status": "certified", "p": p, "at": time.time(),
                                  "evidence": [e["id"] for e in task.evidence if e["source"] != "executor"][-10:]}
            self._finish(task, CERTIFIED, self._done_message(task))
            return False
        task.cert_rejections += 1
        task.budget["cert_rejected_at"] = time.time()
        task.certification = {"status": "rejected", "p": p, "gap": gap.value if gap else None, "at": time.time()}
        task.add_note(f"Jev did not certify (p={p:.2f}); missing: {gap.value if gap else 'unknown'}")
        if task.cert_rejections >= MAX_CERT_REJECTIONS:
            self._finish(task, UNVERIFIED, f"The work was done but could not be verified: "
                                           f"{gap.value if gap else 'evidence was insufficient'}.")
            return False
        self.store.save(task)
        return True

    def _done_message(self, task: Task) -> str:
        last = next((s for s in reversed(task.steps) if s["outcome"] == "done"), None)
        tests = self._tests_since_change(task)
        parts = [f"Done and verified: {task.objective or task.goal}"]
        if last and last["claim"]:
            parts.append(last["claim"][:500])
        if tests:
            parts.append(f"{sum(t['exit_code'] == 0 for t in tests)}/{len(tests)} verification checks passed.")
        if task.files_modified:
            parts.append(f"Changed {len(task.files_modified)} file(s).")
        return " ".join(parts)

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
                                     "risk": "ELEVATED", "reasons": assessment.reasons}
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

    # ----------------------------------------------------------- endings
    def _ask_user(self, task: Task, question: str) -> None:
        task.question = question[:800]
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

    def _finish(self, task: Task, status: str, message: str) -> None:
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
        return {"fingerprint": decision.fingerprint, "kind": kind, "subject": subject[:600],
                "risk": decision.assessment.risk.name, "reasons": decision.assessment.reasons[:5]}

    def run_command(self, command: str, cwd: str | None = None, timeout: float = 120, *, role: str = "") -> dict:
        cwd = str(Path(cwd or self.task.workspace).expanduser())
        assessment = classify(command, cwd, self.scope)
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
            return {"command": command, "decision": "ask", "risk": assessment.risk.name, "exit_code": None,
                    "request": self._request(decision, "command", command),
                    "output": f"NEEDS USER APPROVAL ({assessment.risk.name}): {'; '.join(assessment.reasons)}"}
        stdin_text = None
        if assessment.risk >= Risk.HIGH and re.match(r"\s*sudo\s", command):
            command, stdin_text = _sudo(command)
        event = self.runtime._cancel.get(self.task.id)
        result = shell.run(command, cwd, timeout=min(max(timeout, 1), 3600), stdin_text=stdin_text, cancel=event,
                           output_dir=self.runtime.store.dir / self.task.id)
        data = result.as_dict()
        record.update(exit_code=result.exit_code, timed_out=result.timed_out, duration=round(result.duration, 2),
                      output=result.output[-3000:], full_output_path=result.full_output_path)
        self.task.add_command(record)
        ok, meaning = result.ok, ""
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
        self.task.add_evidence("screen", f"screen check '{question[:150]}': {result.message[:800]}",
                               source="vision", ok=result.ok, step=self.step)
        self.runtime.store.save(self.task)
        return {"ok": result.ok, "text": result.message[:3000]}

    def action(self, name: str, args: dict) -> dict:
        assessment = Assessment(_ACTION_RISK.get(name, Risk.NORMAL))
        subject = f"{name} {json.dumps(args, sort_keys=True)}"
        decision = self._decide("action", subject, assessment)
        if decision.behavior != "allow":
            return {"ok": False, "decision": decision.behavior, "request": self._request(decision, "action", subject)}
        result = self._action_runner()(name, args)
        self.task.add_command({"command": f"[tool] {subject[:500]}", "step": self.step, "decision": "allow",
                               "risk": assessment.risk.name, "exit_code": 0 if result.ok else 1,
                               "output": result.message[-1500:], "at": time.time()})
        self.task.add_evidence("action", f"{subject[:200]} -> {'ok' if result.ok else 'failed'}: {result.message[:600]}",
                               ok=result.ok, step=self.step)
        self.runtime.store.save(self.task)
        return {"ok": result.ok, "decision": "allow", "message": result.message}

    def _action_runner(self):
        if self.runtime._run_action is not None:
            return self.runtime._run_action
        from ..execution.actions import run_action
        return run_action

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
        self.task.add_command({"command": f"[executor] {' '.join(argv[:6])} …", "cwd": cwd, "step": self.step,
                               "decision": "allow", "exit_code": result.exit_code, "timed_out": result.timed_out,
                               "duration": round(result.duration, 1), "output": result.output[-1500:],
                               "full_output_path": result.full_output_path, "at": time.time()})
        self.runtime.store.save(self.task)
        return result.as_dict(max_chars=40_000)

    def note(self, text: str) -> None:
        self.task.add_note(text)


# ------------------------------------------------------------------ helpers
_OBJECT_ID = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")  # SHA-1 or SHA-256 repositories
_ARTIFACT = re.compile(r"(?:^|/)(?:__pycache__|\.pytest_cache|\.mypy_cache|\.ruff_cache|node_modules|\.tox)/|\.py[co]$")
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
    `sudo -n` fails fast instead of hanging on a prompt."""
    try:
        from ..config import load_config
        from ..execution import sudo_approval
        if load_config().sudo_access_enabled:
            password = sudo_approval.retrieve()
            if password:
                return re.sub(r"^\s*sudo\s", "sudo -S -p '' ", command, count=1), password + "\n"
    except Exception:  # noqa: BLE001
        pass
    return re.sub(r"^\s*sudo\s", "sudo -n ", command, count=1), None


def _notify(runtime: TaskRuntime, task: Task, event: str) -> None:
    title = {"waiting_approval": "Omarchy task needs approval", "waiting_user": "Omarchy task has a question",
             CERTIFIED: "Omarchy task done ✓", UNVERIFIED: "Omarchy task finished (unverified)",
             FAILED: "Omarchy task failed"}.get(event)
    if not title:
        return
    if event == "waiting_approval" and task.pending_approval:
        req = task.pending_approval
        body = f"{task.goal[:120]}\n\n{req.get('risk')}: {req.get('subject', '')[:300]}\n{'; '.join(req.get('reasons', []))}"
        threading.Thread(target=_approval_prompt, args=(runtime, task.id, req.get("fingerprint"), title, body),
                         daemon=True).start()
        return
    body = f"{task.goal[:120]}\n\n{(task.question if event == 'waiting_user' else task.result)[:400]}"
    try:
        subprocess.Popen(["notify-send", "-a", "Omarchy AI", "-u", "normal", title, body],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except OSError:
        pass


def _approval_prompt(runtime: TaskRuntime, task_id: str, fingerprint: str, title: str, body: str) -> None:
    """A real human click on the desktop notification is the approval channel
    the model cannot fake (a voice approval is relayed by the live model)."""
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

