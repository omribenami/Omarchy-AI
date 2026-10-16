"""Certification: the code-owned evidence gate, Jev CERTIFY and the user's confirmation.

Split out of runtime/runtime.py, which re-exports every name here; TaskRuntime
inherits CertifyMixin."""
from __future__ import annotations

import time

from .control import LIVE_GAPS
from .executors.base import REVIEW, TEST
from .task import AWAITING_CONFIRMATION, CERTIFIED, FAILED, Task, UNVERIFIED
from .planning import _escalates

VALIDATED_P = 0.6          # Jev must think the test plan proves the behavior
CERTIFY_P = 0.8            # Jev certification threshold
MAX_CERT_REJECTIONS = 3
MAX_PLAN_ATTEMPTS = 2
USER_ONLY_P = 0.8           # P(only the user can tell it worked) at which the user is asked instead
CONFIRM_MINUTES = 10        # "Did it work?" unanswered this long counts as a yes (the user's rule, 2026-10-13)
NO_ANSWER_IS_YES = "(no answer: taken as a yes)"
CONFIRM_FLOOR_P = 0.4       # ... but not when Jev thinks it failed (the two that began this re-scored 0.51, 0.69)
SIMULATED_P = 0.7           # P(only simulated evidence) that ends a live gap as "needs a live test"

# What a refused certification is missing, in words for the user. Until
# 2026-10-07 the result said "could not be verified: runtime_behavior".
_MISSING = {
    "tests": "no passing test or check ran after the last change",
    "runtime_behavior": "the real behavior was not observed after the change",
    "review": "the change was not independently reviewed, or the review found problems",
    "unfixed_failure": "a failure or failing check is still unresolved",
    "user_confirmation": "only you can confirm the outcome",
}


class CertifyMixin:
    """TaskRuntime's certification methods."""

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
        p, gap, simulated, user_only = self.control.certify(task)
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
        missing = _MISSING.get(gap.value if gap else "", "the evidence was not enough to confirm it")
        if gap and gap.value in LIVE_GAPS and (simulated or 0) >= SIMULATED_P:
            # 2026-10-07: a drone script went back to the worker three times,
            # twice, for a flight no worker can make. When all a worker can
            # show is a simulation, another round only spends the budget.
            task.certification["status"] = "needs_live_test"
            claim = next((s["claim"] for s in reversed(task.steps) if s.get("outcome") == "done" and s.get("claim")), "")
            tests = self._tests_since_change(task)
            checks = (f" and {sum(t['exit_code'] == 0 for t in tests)}/{len(tests)} checks pass" if tests else "")
            self._finish(task, UNVERIFIED, f"The work is done{checks}, but it needs a live test by you: {missing}, "
                                           "and the workers could only simulate that part."
                                           + (f"\n\n{claim[:900]}" if claim else ""))
            return False
        if (p >= CONFIRM_FLOOR_P and not task.budget.get("confirmation_asked")
                and ((gap and gap.value == "user_confirmation") or (user_only or 0) >= USER_ONLY_P)):
            # 2026-10-12: a countdown spoken in another room, then two
            # computers locked. Every call returned ok, and it ended unverified
            # twice (0.73, 0.74): no command here can hear a speaker. More
            # rounds add nothing; the user can settle it in a word, and a yes
            # certifies it (so the skill it ran is brought up to date).
            task.budget["confirmation_asked"] = time.time()
            task.certification["status"] = AWAITING_CONFIRMATION
            self._ask_user(task, "Did it work? Every step reported success, but I cannot check the result myself. "
                                 f"If you do not say no within {CONFIRM_MINUTES} minutes I will take it as a yes: "
                                 + task.title[:300])
            return False
        # A confident but wrong "done" never fails a step: the rejection is
        # the signal that the worker model was not up to it.
        last = next((s for s in reversed(task.steps) if s["executor"] in self.executors), None)
        if last and _escalates(self.executors[last["executor"]]):
            self._escalate(task, last)
        if task.cert_rejections >= MAX_CERT_REJECTIONS:
            self._finish(task, UNVERIFIED, f"The work was done but could not be verified: {missing}.")
            return False
        self.store.save(task)
        return True

    def _confirmed(self, task: Task) -> None:
        """The user answered "did it work?": their word is the evidence no
        worker could get. Jev reads the answer; nothing is run again."""
        answer = task.answers[-1]
        silent = answer == NO_ANSWER_IS_YES
        verdict = "worked" if silent else self.control.confirmation(task)
        task.add_decision("confirm", verdict, None, {"by": "default" if silent else "user"})
        claim = next((s["claim"] for s in reversed(task.steps) if s.get("outcome") == "done" and s.get("claim")
                      and s.get("role") not in (TEST, REVIEW)), "")
        if verdict == "worked":
            evidence = task.add_evidence(
                "confirmation", f"The user did not say it failed within {CONFIRM_MINUTES} minutes of being asked."
                if silent else f"The user confirmed it worked: {answer[:300]}", source="user", ok=True)
            task.certification = {"status": "certified", "p": task.certification.get("p"),
                                  "by": "default" if silent else "user",
                                  "at": time.time(), "evidence": [evidence["id"]]}
            self._finish(task, CERTIFIED, self._done_message(task))
        elif verdict == "did_not_work":
            task.certification["status"] = "rejected"
            self._finish(task, FAILED, f"You said it did not work: {answer[:300]}"
                                       + (f"\n\nWhat was done: {claim[:900]}" if claim else ""))
        else:
            task.certification["status"] = "rejected"
            self._finish(task, UNVERIFIED, (claim[:1200] + "\n\n" if claim else "")
                         + "Not verified: only you can tell whether it worked, and your answer did not say.")

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
