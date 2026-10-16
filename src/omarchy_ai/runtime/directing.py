"""Jev DIRECT: which directive comes next after each step.

Split out of runtime/runtime.py, which re-exports every name here; TaskRuntime
inherits DirectingMixin."""
from __future__ import annotations

from .task import FAILED, Task, UNVERIFIED
from .certify import CERTIFY_P, MAX_PLAN_ATTEMPTS, VALIDATED_P
from .recovery import _timeout_step

# Completed steps after a refused certification that may add no evidence
# before the answer is delivered unverified (_repeating_after_rejection).
MAX_STEPS_WITHOUT_EVIDENCE = 2
MAX_CONSECUTIVE_RETRIES = 2


class DirectingMixin:
    """TaskRuntime's Jev DIRECT step."""

    # ------------------------------------------------------------- direct
    def _allowed_directives(self, task: Task) -> list[str]:
        last = task.steps[-1] if task.steps else None
        allowed = ["CONTINUE", "SPAWN_SUBAGENT", "ASK_USER", "FAIL"]
        if len(self._candidates(exclude={"TEST_AGENT", "REVIEW_AGENT"}, task=task)) > 1:
            allowed.append("CHANGE_EXECUTOR")
        if last and last["outcome"] in ("failed", "blocked") and self._consecutive(task) <= MAX_CONSECUTIVE_RETRIES:
            allowed.append("RETRY")
        changed = bool(task.files_modified)
        if changed and self._reviewers(task) and not self._reviewed_since_change(task):
            allowed.append("REQUEST_REVIEW")
        validated = (task.test_plan.get("validated") or 0) >= VALIDATED_P
        if validated and not self._tests_since_change(task):
            allowed.append("RUN_TESTS")
        if not validated and task.test_plan.get("attempts", 0) < MAX_PLAN_ATTEMPTS and self._tester(task):
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

    def _repeating_after_rejection(self, task: Task) -> bool:
        """Jev refused to certify, and the work since then is done but has
        added nothing a second certification could use.

        Real task 2026-10-05 23:03 (a read-only question about a server):
        the answer was there after one step, Jev scored it 0.73 with nothing
        missing, and CERTIFY then waits for new harness evidence. A coding
        agent that only reads never produces any, so CONTINUE was the one
        directive left and the worker gave the same answer nine more times,
        to the step budget, whose closing message does not carry the answer.
        The same on 2026-10-02 (15 steps) and 2026-10-05 08:13 (8 steps)."""
        rejected_at = task.budget.get("cert_rejected_at", 0)
        if not rejected_at or any(e["at"] > rejected_at for e in task.evidence if e["source"] != "executor"):
            return False
        since = [s for s in task.steps if s.get("outcome") == "done" and (s.get("finished_at") or 0) > rejected_at]
        return len(since) >= MAX_STEPS_WITHOUT_EVIDENCE

    def _direct(self, task: Task) -> bool:
        task.phase = "direct"
        allowed = self._allowed_directives(task)
        last = task.steps[-1] if task.steps else None
        if self._repeating_after_rejection(task):
            answer = next((s["claim"] for s in reversed(task.steps) if s.get("outcome") == "done" and s.get("claim")), "")
            p = task.certification.get("p")
            self._finish(task, UNVERIFIED, (answer[:1500] + "\n\n" if answer else "")
                         + "Not verified: Jev's confidence in this result was "
                         + (f"{p:.2f}" if isinstance(p, (int, float)) else "too low")
                         + f" (it certifies at {CERTIFY_P}), and repeating the work added no new evidence.")
            return False
        # Repeated completed assignments with sufficient harness evidence
        # should be judged, not sent through another identical work loop.
        # Certification remains Jev's decision and retains all evidence gates.
        if ("CERTIFY" in allowed and len(task.steps) >= 2
                and all(s.get("outcome") == "done" for s in task.steps[-2:])
                and task.steps[-2].get("executor") == (last or {}).get("executor")):
            return self._certify(task)
        # A worker saying it ran out of its own assignment time is operational
        # feedback, not information the user can supply. Keeping ASK_USER in
        # the choice set caused an endless timeout -> Continue -> timeout loop.
        if _timeout_step(last):
            allowed = [directive for directive in allowed if directive != "ASK_USER"]
        # The test and review subagents are reached only through REQUEST_MORE_TESTS
        # and REQUEST_REVIEW (live run 2026-09-23: CONTINUE -> TEST_AGENT looped
        # nine times past the test-plan attempt limit).
        if not self._workers_ready(task):
            return False
        executors = self._candidates(exclude={"TEST_AGENT", "REVIEW_AGENT"}, task=task)
        if task.agent:
            executors = {k: v for k, v in executors.items() if k == task.agent}
            allowed = [d for d in allowed if d != "CHANGE_EXECUTOR"]
        directive, pick = self.control.direct(task, allowed, executors)
        task.add_decision("direct", directive.value, directive.p,
                          {"executor": pick.value, "executor_p": pick.p, "allowed": allowed, "fallback": directive.fallback})
        self.store.save(task)
        d = directive.value
        last = task.steps[-1] if task.steps else None
        if d == "FAIL":
            # Say why it failed; a worker's claim is only its claim. 2026-09-27
            # 20:43 the user heard "failed. I could not complete this.
            # Implemented and verified the connection" -- Codex's own words.
            done = [s for s in task.steps if s["outcome"] not in ("waiting_approval", "paused", "declined")]
            ok, gap = self._certification_gate(task)
            why = gap if not ok else "Jev judged that the acceptance criteria are not met"
            unmet = "; ".join(task.plan[:3])
            message = f"I could not complete this: {why}. It had to achieve: {unmet[:400]}."
            if done and done[-1].get("claim"):
                message += (f" The last worker ({done[-1]['executor']}) reported this, NOT verified: "
                            f"{done[-1]['claim'][:400]}")
            self._finish(task, FAILED, message)
            return False
        if d == "ASK_USER":
            if _timeout_step(last):
                # Be defensive if a decision backend returns a value outside
                # its offered directives. The global task budget still gives
                # the honest final stop condition if no worker can progress.
                task.add_note("Ignored non-actionable executor timeout as a user question.")
                name = pick.value if pick.value in executors else (last or {}).get("executor", "SYSTEM_AGENT")
                task.next_dispatch = self._assignment(task, name, "CONTINUE")
                return True
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
            task.next_dispatch = self._assignment(task, self._tester(task) or "TEST_AGENT", d)
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
        if task.agent:
            name = task.agent
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
