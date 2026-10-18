"""Chaos-child programs for tests/chaos/test_side_effects.py.

Each one drives the real send path (execution/actions/myapi_tools.py,
core/agenda.py, execution/myapi_agent.py) against the fake MyApi the child
is pointed at. The effects.* crashpoints are in core/effects.py itself.
Anything that would reach the desktop, a phone or Jev is stubbed first.
"""

from __future__ import annotations

import time


def _effects_state() -> dict:
    from omarchy_ai.core import effects

    return {
        "records": effects.records(),
        "index": effects.statefile.read_json(effects.index_path(), {}),
    }


def send(origin: str, subject: str = "Hello") -> dict:
    """One Gmail send for one logical action (its origin), as a live turn makes it."""
    from omarchy_ai.core import approvals, effects
    from omarchy_ai.execution import actions

    args = {"recipient_email": "someone@example.com", "subject": subject, "body": "Hi."}
    # The user's tap on its confirm card (core/approvals.py).
    with (
        effects.origin(origin),
        approvals.approved_call("myapi_gmail_send", args, "tap"),
    ):
        result = actions.myapi_gmail_send(args)
    return {"ok": result.ok, "message": result.message, **_effects_state()}


def _quiet_agenda():
    from omarchy_ai.core import agenda

    agenda.notify = lambda *a, **k: None
    agenda._refresh_routines_hud = lambda: None
    return agenda


def schedule_send(subject: str = "Routine") -> str:
    """A routine that sends one email, created for a minute from now."""
    from omarchy_ai.core import approvals

    agenda = _quiet_agenda()
    # Approved when it is scheduled, as the user's tap would (core/approvals.py).
    with approvals.approved_call("schedule_task", {}, "tap"):
        job = agenda.create(
            {
                "title": "send the note",
                "kind": "action",
                "in_minutes": 1,
                "tool": "myapi_gmail_send",
                "args": {
                    "recipient_email": "someone@example.com",
                    "subject": subject,
                    "body": "Hi.",
                },
            }
        )
    return job["id"]


def heartbeat(minutes_ahead: float = 2) -> dict:
    """One heartbeat tick as if `minutes_ahead` had passed: runs whatever is due."""
    agenda = _quiet_agenda()
    started = agenda.tick(time.time() + minutes_ahead * 60, wait=True)
    return {"started": started, "jobs": agenda._load(), **_effects_state()}


def prepare_send() -> str:
    """A prepared email waiting for the user's Send (pending_actions)."""
    from omarchy_ai.core import pending_actions

    return pending_actions.create(
        {
            "service": "gmail",
            "method": "GMAIL_SEND_EMAIL",
            "title": "Send email",
            "fields": [],
            "arguments": {
                "recipient_email": "someone@example.com",
                "subject": "Prepared",
                "body": "Hi.",
                "user_id": "me",
            },
        }
    )


def press_send(aid: str) -> dict:
    """The user's Send on that card (myapi_agent.decide), Jev's checks stubbed."""
    from omarchy_ai.execution import myapi_agent

    myapi_agent._worked = lambda action, raw: True
    myapi_agent._sent_check = lambda action: None
    ok, message = myapi_agent.decide(aid, True, "test", by="tap")
    return {"ok": ok, "message": message, **_effects_state()}


def resend_pending(aid: str) -> dict:
    """The same approved action sent again through its origin (what 1.2's
    recovery would do): never a second write."""
    from omarchy_ai.core import pending_actions
    from omarchy_ai.execution import myapi_agent

    from omarchy_ai.core import approvals

    action = pending_actions.get(aid)
    approvals.grant(
        approvals.send_key(action["service"], action["method"], action["arguments"]),
        "tap",
    )
    result = myapi_agent._call(
        action["service"],
        action["method"],
        action["arguments"],
        origin=f"pending:{aid}",
    )
    return {"ok": result.ok, "message": result.message, **_effects_state()}
