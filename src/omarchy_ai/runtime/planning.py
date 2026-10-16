"""Planning and routing: the acceptance-criteria plan, and which executor may run.

Split out of runtime/runtime.py, which re-exports every name here; TaskRuntime
inherits PlanningMixin."""
from __future__ import annotations

import re
import time

from .executors import Executor
from .llm import WorkerModelError
from .permissions import Assessment, Risk
from .task import CANCELLED, FAILED, Task, WAITING_APPROVAL, request_text

CODING = {"CLAUDE_CODE", "CODEX"}
# Workers that spend the paid API worker model. The user's rule (2026-09-27,
# "I'm paying API credit for nothing"): Codex or Claude Code do the work; the
# API workers run only when both cannot (not installed, logged out, usage
# limit reached), and only after the user approves it for that task.
API_WORKERS = {"SYSTEM_AGENT", "TEST_AGENT", "REVIEW_AGENT"}
from .glossary import PROJECT_TERMS  # noqa: E402

PLAN_PROMPT = """You turn a user's request to Omarchy AI (a voice/desktop assistant on an Arch Linux + Hyprland
machine) into a task objective with checkable acceptance criteria. Do not solve the task.
Reply with one JSON object:
{"objective": "<one or two sentences: what must be true when done>",
 "acceptance_criteria": ["<observable, checkable condition>", ... 1 to 5 items],
 "verification_ideas": ["<how someone other than the worker could check it on this machine>", ...]}
Criteria must be about the real outcome (the device works, the test passes, the file exists with X), not about
effort. For a question the user wants answered, the criterion is that the answer is supported by evidence.
Never add deliverables the user did not ask for (no reports, notes or saved files unless requested).
Preserve the requested final deliverable as an acceptance criterion, not only its prerequisites.
Do not add 'without manual intervention': authentication or approval can legitimately require the user.
An existing skill is a procedure, not a missing executable tool. Read it before proposing implementation.
Copy every link, file path and product, API or model name from the request into the objective exactly as written;
never replace a named thing with a general one (a named API does not become "a suitable API").
Keep the user's own words for anything you are not sure of; an unknown term becomes a criterion that the worker
finds out what it means, never a guess.
An accepted task does not become complete merely because an existing tool is missing. Include the user's actual
end result as a criterion. If achieving it requires a reusable capability Omarchy lacks, the plan must include
building and testing an approval-gated user tool with Claude Code or Codex, installing it only after approval, and
then resuming the same task to achieve and verify the original result.

""" + PROJECT_TERMS


PLAN_TIMEOUT = 90

_LINK = re.compile(r"https?://[^\s<>\"'`)\]]+")


def links(text: str, limit: int = 5) -> list[str]:
    """The distinct links in `text`, in order, without trailing punctuation."""
    found = []
    for match in _LINK.findall(text or ""):
        link = match.rstrip(".,;:!?")
        if link not in found:
            found.append(link)
    return found[:limit]

def _model_of(executor: Executor) -> str:
    """The model behind an executor, so "which model is doing this?" has a
    real answer (real gap 2026-09-26: asked mid-task, she could not say)."""
    worker = getattr(executor, "model", None)
    try:
        name = worker if isinstance(worker, str) else getattr(worker, "model", None)
    except Exception:  # noqa: BLE001 -- the gateway config may be unreadable
        name = None
    if name:
        return str(name)
    return {"CLAUDE_CODE": "Claude Code (its default model)", "CODEX": "Codex (its default model)",
            "DIRECT_TOOL": "desktop tools (Jev)"}.get(executor.name, "unknown")


def _escalates(executor: Executor) -> bool:
    """Only workers on the auto-chosen model move up the ladder."""
    worker = getattr(executor, "model", None)
    try:
        return bool(getattr(worker, "auto", False))
    except Exception:  # noqa: BLE001
        return False


class PlanningMixin:
    """TaskRuntime's planning and routing methods."""

    # ---------------------------------------------------------------- plan
    def _plan(self, task: Task) -> None:
        self._progress(task, "Sending to Jev: planning")
        try:
            # 90 s, shared by the backends (30 s each when all three are up).
            # 2026-10-08 00:29: 45 s gave each 15 s. Measured on that request
            # the same night: claude 10 s, codex 15 s, the API ladder 25-40 s
            # (shorter requests: 6-7 s, 9-11 s, 8-19 s), so no fallback could
            # finish. The call returns as soon as one backend answers.
            plan = self.planner.complete(PLAN_PROMPT, {"request": task.goal, "workspace": task.workspace},
                                         timeout=PLAN_TIMEOUT)
            task.objective = str(plan.get("objective") or task.goal)[:1000]
            # 2026-10-07: the user named an API by its docs link; the objective
            # said "a vision-capable model" and the worker built on another API.
            dropped = [u for u in links(task.goal) if u not in task.objective]
            if dropped:
                refs = " References from the request: " + " ".join(dropped)
                task.objective = task.objective[:max(0, 1000 - len(refs))] + refs
            task.plan = [str(c)[:300] for c in (plan.get("acceptance_criteria") or []) if c][:5] or [request_text(task.goal)[:300]]
            ideas = [str(i)[:300] for i in (plan.get("verification_ideas") or [])][:4]
            if ideas:
                task.add_note("Verification ideas (planner): " + " | ".join(ideas))
        except WorkerModelError as exc:
            task.add_error(f"planner unavailable: {exc}")
            task.objective, task.plan = task.goal[:1000], [request_text(task.goal)[:300]]
        self.store.save(task)

    # --------------------------------------------------------------- route
    def _candidates(self, *, roles: set[str] | None = None, exclude: set[str] = frozenset(),
                    task: Task | None = None) -> dict[str, str]:
        if task is not None:
            exclude = set(exclude) | (CODING - set(self._coding_ready()))
            if not self._api_allowed(task):
                exclude |= API_WORKERS
        out = {}
        for name, info in self.available().items():
            executor = self.executors[name]
            if not info["available"] or name in exclude:
                continue
            if roles is not None and not (executor.roles & roles):
                continue
            out[name] = executor.description
        return out

    def _coding_ready(self, refresh: bool = False) -> list[str]:
        """Checked live, not from the 5-minute cache: an agent that just hit
        its usage limit must stop being chosen at once."""
        ready = []
        for name in sorted(CODING & set(self.executors)):
            executor = self.executors[name]
            detect = getattr(executor, "detect", None)
            if refresh and detect:
                detect(force=True)  # a cached failure may be a one-off slow start
            try:
                ok, _ = executor.available()
            except Exception:  # noqa: BLE001
                ok = False
            if ok:
                ready.append(name)
        if refresh:
            self.available(refresh=True)
        return ready

    def _workers_ready(self, task: Task) -> bool:
        """Codex/Claude Code first; the paid API worker only with the user's
        OK when neither can run. False while waiting for that OK."""
        # WorkflowAgent is a first-party coordinator, not a fallback coding
        # model. It owns mixed MyApi/browser jobs even when coding CLIs are
        # unavailable, and it must be offered before this method asks the
        # user to spend API-worker credit on a general system agent.
        workflow = self.available().get("WORKFLOW_AGENT", {})
        if workflow.get("available"):
            return True
        ready = self._coding_ready()
        if task.agent and task.agent not in ready:
            ready = self._coding_ready(refresh=True)
        if task.agent and task.agent not in ready:
            reason = self.executors[task.agent].available()[1] or "it is not available"
            other = next((n for n in ready if n != task.agent), None)
            if other:
                task.add_note(f"{task.agent} cannot run ({reason}); {other} continues the task.")
                task.agent = other
                self.store.save(task)
                return True
            if f"api-worker:{task.id}" not in task.grants:
                return self._request_api(task, f"{task.agent}: {reason}")
            task.add_note(f"{task.agent} cannot run ({reason}); the approved API worker continues.")
            task.agent, task.unsandboxed = "", False
            self.store.save(task)
            return True
        if not ready and not self._api_allowed(task):
            why = "; ".join(f"{n}: {self.executors[n].available()[1] or 'unavailable'}"
                            for n in sorted(CODING & set(self.executors))) or "no coding agent is set up"
            return self._request_api(task, why)
        return True

    def _request_api(self, task: Task, why: str) -> bool:
        if not self.available().get("SYSTEM_AGENT", {}).get("available"):
            self._finish(task, FAILED, f"No worker can run: {why}; the API worker is unavailable too.")
            return False
        model = _model_of(self.executors["SYSTEM_AGENT"]) or "the API worker model"
        subject = f"use the paid API worker ({model}) for this task, because Codex and Claude Code can't run"
        assessment = Assessment(Risk.ELEVATED, [why[:300], "spends API credit"])
        # Always asked (never auto-approved): the grant is this exact fingerprint.
        task.pending_approval = {"fingerprint": f"api-worker:{task.id}", "kind": "api", "subject": subject,
                                 "risk": "ELEVATED", "reasons": assessment.reasons, "asked_at": time.time()}
        task.next_dispatch = None
        task.status, task.phase = WAITING_APPROVAL, "waiting_approval"
        task.add_note(f"Waiting for approval to use the API worker: {why[:300]}")
        self.store.save(task)
        self._emit(task, "waiting_approval")
        return False

    def _route(self, task: Task) -> bool:
        self._progress(task, "Sending to Jev: choosing tools")
        if not self._workers_ready(task):
            return False
        candidates = self._candidates(exclude={"TEST_AGENT", "REVIEW_AGENT"}, task=task)
        if task.agent:
            # The user named the worker (2026-09-27: "open codex --yolo and ask
            # him ..." ran on qwen, then grok). No routing.
            task.add_decision("route", task.agent, 1.0, {"requested_by_user": True})
            task.next_dispatch = self._assignment(task, task.agent, "ROUTE")
            self.store.save(task)
            return True
        if not candidates:
            self._finish(task, FAILED, "No executor is available (the worker model and coding agents are all unreachable).")
            return False
        # 2026-10-12 22:15: "start a 6-minute countdown to lock down the PCs" was
        # judged too vague seven times in a row, each answer met with the same
        # fixed question -- the user's whole sentence, three taps on Yes, one on
        # No. It is asked once: after an answer a worker takes the job with
        # that answer (it can ask something specific itself), and a no ends it.
        answered = bool(task.answers) and not task.steps
        if answered and self.control.declined(task):
            task.add_decision("route", "CANCEL", None, {"declined_by_user": True})
            self._finish(task, CANCELLED, "Cancelled: you said not to do it.")
            return False
        pick, needs_code = self.control.route(task, candidates, ask=not answered)
        task.needs_code = needs_code
        task.add_decision("route", pick.value, pick.p, {"needs_code_change": needs_code, "fallback": pick.fallback,
                                                        "probabilities": pick.probabilities})
        self._progress(task, f"Jev decision: {pick.value.replace('_', ' ')}")
        if pick.value == "ASK_USER":
            self._ask_user(task, "Before I start, can you tell me more precisely what you want done?")
            return False
        task.next_dispatch = self._assignment(task, pick.value, "ROUTE")
        self.store.save(task)
        return True
