"""The executor abstraction: every worker Jev can route to implements this.

An executor gets an Assignment (only the context its job needs, never the
whole task) and a WorkContext (the harness's permission-checked capabilities:
run a command, launch an app, delegate a desktop goal, look at the screen).
It returns a Report. Everything a Report says about the executor's own work
is a *claim*; the runtime records it as untrusted and gathers its own
evidence before Jev may certify anything.

Adding an executor: subclass Executor, set name/description/capabilities,
implement available() and run(), and register it in executors/__init__.py.
Nothing else in the codebase needs to know it exists.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

# Report statuses
DONE, FAILED, BLOCKED = "done", "failed", "blocked"
NEEDS_APPROVAL, NEEDS_USER, NEEDS_CODE_CHANGE = "needs_approval", "needs_user", "needs_code_change"
REPORT_STATUSES = {DONE, FAILED, BLOCKED, NEEDS_APPROVAL, NEEDS_USER, NEEDS_CODE_CHANGE}

# Roles an assignment can have. The runtime picks the role from Jev's
# directive; an executor may support only some of them.
WORK, DIAGNOSE, IMPLEMENT, TEST, REVIEW = "work", "diagnose", "implement", "test", "review"


@dataclass
class Assignment:
    task_id: str
    role: str
    goal: str
    instructions: str
    workspace: str
    context: dict = field(default_factory=dict)   # bounded findings/evidence from earlier steps
    write_access: bool = False
    timeout: float = 600


@dataclass
class Report:
    status: str
    claim: str = ""                                   # untrusted: the executor's own account
    findings: list[str] = field(default_factory=list)  # untrusted
    approval: dict | None = None                      # the permission request that stopped it
    question: str | None = None
    test_commands: list[str] = field(default_factory=list)  # proposed, validated by Jev before use
    verdict: str | None = None                        # review: pass | fail | partial
    meta: dict = field(default_factory=dict)          # cost, turns, session ids, model

    def __post_init__(self):
        if self.status not in REPORT_STATUSES:
            self.status = FAILED


class WorkContext(Protocol):
    """Harness capabilities offered to an executor. Every one is permission
    checked and recorded by the runtime; an executor never bypasses it."""

    cancel_requested: bool

    def run_command(self, command: str, cwd: str | None = None, timeout: float = 120) -> dict: ...
    def launch(self, command: str) -> dict: ...
    def desktop(self, goal: str) -> dict: ...
    def screen(self, question: str) -> dict: ...
    def action(self, name: str, args: dict) -> dict: ...
    def check_external(self, kind: str, subject: str, risk, reasons: list[str]) -> dict: ...
    def run_external(self, argv: list[str], cwd: str, timeout: float, stdin_text: str | None = None) -> dict: ...
    def note(self, text: str) -> None: ...


class Executor:
    name = "EXECUTOR"
    kind = "internal"          # internal | external | direct
    description = ""           # what Jev reads when routing
    roles: set[str] = {WORK}
    can_write_code = False

    def available(self) -> tuple[bool, str]:
        return True, ""

    def run(self, assignment: Assignment, ctx: WorkContext) -> Report:
        raise NotImplementedError
