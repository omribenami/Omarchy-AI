"""Executor registry: the workers Jev can route to.

Order matters only for display. Availability is checked at routing time, so
an executor whose CLI is missing or logged out simply is not offered to Jev.
"""
from __future__ import annotations

from .base import Assignment, Executor, Report, WorkContext
from .coding_agents import ClaudeCode, Codex
from .direct import DirectTool, InternalReviewer
from .system_agent import SystemAgent, TestAgent


def default_executors(model=None) -> dict[str, Executor]:
    executors = [DirectTool(model), SystemAgent(model), TestAgent(model), InternalReviewer(model),
                 ClaudeCode(), Codex()]
    return {e.name: e for e in executors}


def availability(executors: dict[str, Executor]) -> dict[str, dict]:
    report = {}
    for name, executor in executors.items():
        try:
            ok, reason = executor.available()
        except Exception as exc:  # noqa: BLE001
            ok, reason = False, str(exc)
        entry = {"available": ok, "reason": reason, "kind": executor.kind, "roles": sorted(executor.roles)}
        if hasattr(executor, "detect"):
            entry["detail"] = executor.detect()
        report[name] = entry
    return report


__all__ = ["Assignment", "Executor", "Report", "WorkContext", "default_executors", "availability",
           "ClaudeCode", "Codex", "DirectTool", "InternalReviewer", "SystemAgent", "TestAgent"]
