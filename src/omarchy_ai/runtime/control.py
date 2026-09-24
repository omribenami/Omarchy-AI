"""Jev as the control plane: ROUTE, DIRECT, VALIDATE, CERTIFY.

Jev answers typed questions (choice/boolean) about a compact state; it
never writes text, code or commands. Each decision here is one Jev call
over a bounded brief built from the task record -- never a raw transcript.

Code owns what Jev should not judge (TypeSafe's guidance for Jev, same as
core/agenda.py): which directives are even possible right now, budget and
retry limits, and the evidence gates that must pass before Jev is asked to
certify at all. Executor claims are labeled untrusted in every brief.

MiniMax Code's goal verifier (agent-modules/goal/src/verification) shaped
the certify step: a bounded evidence brief (objective, claim, changes,
recent tail) with hard size limits, an independent verifier that only
sees evidence, and a host that -- not the worker -- settles completion.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import logging

from ..core.jev import Jev, JevError, boolean, choice
from .task import Task

log = logging.getLogger(__name__)

MAX_BRIEF_CHARS = 14_000   # MiniMax MAX_VERIFICATION_BRIEF_CHARS

DIRECTIVES = {
    "CONTINUE": "The last step made real progress and more work remains; give the next part to an executor.",
    "RETRY": "The last step failed for a reason a second attempt by the same executor, with a different approach, is likely to fix.",
    "CHANGE_EXECUTOR": "The last executor is the wrong kind of worker for what remains (for example a code defect was found and a coding agent should fix it, or a coding task needs system diagnosis).",
    "SPAWN_SUBAGENT": "A focused side investigation or verification by an internal Omarchy subagent is needed before going on.",
    "REQUEST_REVIEW": "Code or configuration was changed and an independent reviewer who did not write it should check it.",
    "RUN_TESTS": "A validated verification plan exists and has not yet been run against the current state.",
    "REQUEST_MORE_TESTS": "There is no validated way to verify the result yet (no test plan, or the plan would not prove the requested behavior).",
    "ROLLBACK": "Changes made during this task made things worse or cannot be completed safely; undo them.",
    "ASK_USER": "Only the user can unblock this: a missing decision, preference, credential or physical action.",
    "FAIL": "The goal cannot be achieved with the available executors and permissions, or repeated attempts made no progress.",
    "CERTIFY": "Independent evidence already shows the whole goal is done; request certification.",
}


@dataclass
class Choice:
    value: str
    p: float | None
    probabilities: dict | None = None
    fallback: bool = False


def _clip(text, n: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[: n - 1] + "…"


def _middle(text, n: int) -> str:
    """Head and tail of long evidence: a listing's first lines matter as much as its last."""
    text = "" if text is None else str(text)
    return text if len(text) <= n else text[: n * 3 // 5] + " … " + text[-(n * 2 // 5):]


def brief(task: Task, *, evidence_items: int = 14, steps: int = 8) -> str:
    """Compact, bounded, JSON state for Jev. Untrusted text is marked."""
    harness = [e for e in task.evidence if e["source"] != "executor"][-evidence_items:]
    state = {
        "goal": _clip(task.goal, 1500),
        "objective": _clip(task.objective, 800),
        "acceptance_criteria": [_clip(c, 300) for c in task.plan[:8]],
        "workspace": task.workspace,
        "steps": [{"n": s["n"], "executor": s["executor"], "role": s["role"], "outcome": s["outcome"],
                   "claim_UNTRUSTED": _clip(s["claim"], 350)} for s in task.steps[-steps:]],
        "evidence_observed_by_harness": [{"id": e["id"], "kind": e["kind"], "source": e["source"], "ok": e["ok"],
                                          "step": e["step"], "text": _middle(e["text"], 700)} for e in harness],
        "files_modified": task.files_modified[-15:],
        "test_plan": {"commands": task.test_plan.get("commands", [])[:6],
                      "validated": task.test_plan.get("validated")},
        "tests_after_last_change": [{"command": _clip(t["command"], 160), "exit_code": t["exit_code"]}
                                    for t in task.tests if t.get("after_change", True)][-6:],
        "reviews": [{"by": r["by"], "verdict": r["verdict"], "issues": [_clip(i, 200) for i in r.get("issues", [])[:4]]}
                    for r in task.reviews[-3:]],
        "notes": [_clip(n, 300) for n in task.notes[-6:]],
        "user_answers": [_clip(a, 300) for a in task.answers[-3:]],
        "errors": [_clip(e, 200) for e in task.errors[-3:]],
        "steps_used": len(task.steps), "max_steps": task.budget.get("max_steps"),
    }
    text = json.dumps(state, ensure_ascii=False)
    # Shrink oldest-first until it fits (MiniMax fitBrief).
    while len(text) > MAX_BRIEF_CHARS and (state["evidence_observed_by_harness"] or state["steps"]):
        if len(state["evidence_observed_by_harness"]) >= len(state["steps"]):
            state["evidence_observed_by_harness"].pop(0)
        else:
            state["steps"].pop(0)
        text = json.dumps(state, ensure_ascii=False)
    return text[:MAX_BRIEF_CHARS]


class ControlPlane:
    def __init__(self, jev: Jev | None = None):
        self.jev = jev or Jev()

    def _ask(self, task: Task, state: str, questions: dict) -> dict | None:
        try:
            return self.jev.ask(state, questions, timeout=15, retries=2)
        except (JevError, Exception) as exc:  # noqa: BLE001 -- Jev trouble must never crash a task
            log.warning("Jev unavailable for task %s: %s", task.id, exc)
            task.add_error(f"Jev unavailable: {exc}")
            return None

    # -- ROUTE -----------------------------------------------------------
    def route(self, task: Task, candidates: dict[str, str]) -> tuple[Choice, float | None]:
        """(executor or ASK_USER, P(needs code change))."""
        criteria = dict(candidates)
        criteria["ASK_USER"] = "The request is too ambiguous to act on safely without asking the user first."
        questions = {
            "executor": choice(
                "Which worker should take the FIRST step of this task? Judge by what the task actually requires "
                "(desktop control, system tools and diagnosis, code changes in a repository, review), the risk, and "
                "cost: prefer the simplest worker that can do the whole first step. A request to fix a bug in a "
                "program's source code needs a coding agent; a problem whose cause is unknown usually needs "
                "system diagnosis first.", criteria),
            "needs_code_change": boolean(
                "Will completing this goal require modifying source code files in a software repository?"),
        }
        answers = self._ask(task, brief(task), questions)
        if not answers:
            fallback = "SYSTEM_AGENT" if "SYSTEM_AGENT" in candidates else next(iter(candidates), "ASK_USER")
            return Choice(fallback, None, fallback=True), None
        pick = answers["executor"]
        return Choice(pick["choice"], pick["p"], pick["probabilities"]), answers["needs_code_change"]["p"]

    # -- DIRECT ----------------------------------------------------------
    def direct(self, task: Task, allowed: list[str], executors: dict[str, str]) -> tuple[Choice, Choice]:
        criteria = {k: DIRECTIVES[k] for k in allowed}
        questions = {
            "directive": choice(
                "Given the task state, what should happen next? Base it on evidence observed by the harness; "
                "claims marked UNTRUSTED are what an executor says about its own work and are not proof. "
                "Do not repeat something that already failed the same way.", criteria),
        }
        if executors:
            questions["executor"] = choice(
                "If the next step needs a worker, which one fits the remaining work best? Prefer a different worker "
                "when the current one failed or is the wrong kind.", dict(executors))
        answers = self._ask(task, brief(task), questions)
        if not answers:
            return self._fallback_direct(task, allowed), Choice(next(iter(executors), ""), None, fallback=True)
        d = answers["directive"]
        e = answers.get("executor") or {"choice": "", "p": None, "probabilities": None}
        return Choice(d["choice"], d["p"], d["probabilities"]), Choice(e["choice"], e["p"], e["probabilities"])

    @staticmethod
    def _fallback_direct(task: Task, allowed: list[str]) -> Choice:
        """Without Jev: never certify, never escalate; stop honestly."""
        last = task.steps[-1] if task.steps else None
        if last and last["outcome"] in ("failed", "blocked") and task.attempts(last["executor"]) < 2 and "RETRY" in allowed:
            return Choice("RETRY", None, fallback=True)
        return Choice("FAIL", None, fallback=True)

    # -- VALIDATE --------------------------------------------------------
    def validate(self, task: Task, commands: list[str]) -> float | None:
        """P(the checks would catch it if the goal were NOT achieved).

        Probed on the live Jev (2026-09-23, docs/ADR-0002): a compound question
        ("the claim is true AND every criterion AND real behavior") scored a
        sound plan 0.26; "meaningful, not trivial" passed `ss -ltnp` (0.94).
        This single counterfactual question separated good plans (0.95, 0.80)
        from compile/cat/import-only/listing plans (0.32-0.39). Exit-code
        masking (`; echo $?`) fools it (0.93), so code rejects that first
        (runtime.masked_exit_code)."""
        state = json.dumps({"goal": _clip(task.goal, 1500),
                            "proposed_verification_commands": [_clip(c, 400) for c in commands[:8]]},
                           ensure_ascii=False)
        answers = self._ask(task, state, {"detects": boolean(
            "Would at least one of these commands fail (exit non-zero) if the requested behavior were still "
            "broken or not done?")})
        return answers["detects"]["p"] if answers else None

    # -- CERTIFY ---------------------------------------------------------
    def certify(self, task: Task) -> tuple[float | None, Choice | None]:
        gaps = {
            "none": "Nothing is missing: the independent evidence covers every acceptance criterion.",
            "tests": "The result has not been verified by passing tests or checks run after the last change.",
            "runtime_behavior": "The real behavior (service running, device working, app state) was not observed after the change.",
            "review": "Changed code has not been independently reviewed, or the review found problems.",
            "unfixed_failure": "A failure, error or failing check is still unresolved.",
            "user_confirmation": "Only the user can confirm the outcome (subjective or physical result).",
        }
        answers = self._ask(task, brief(task, evidence_items=20), {
            "certified": boolean(
                "Is EVERY acceptance criterion of the goal met right now? Use the UNTRUSTED executor claims only to "
                "learn WHAT is being claimed (the answer given, the change made); decide whether it is TRUE from the "
                "evidence observed by the harness (commands the harness ran, tests, diffs, independent reviews). "
                "A claim contradicted or not supported by that evidence means no."),
            "gap": choice("What is the most important thing still missing before this goal can be certified?", gaps),
        })
        if not answers:
            return None, None
        g = answers["gap"]
        return answers["certified"]["p"], Choice(g["choice"], g["p"], g["probabilities"])
