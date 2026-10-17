"""Chaos-child programs for tests/chaos/test_recovery.py (DoD-6, pending actions).

They prepare, press and recover pending actions (core/pending_actions.py,
execution/myapi_agent.py) against the fake MyApi the child is pointed at.
Jev's checks, the Sent check and notifications are stubbed first.
"""

from __future__ import annotations

import time


def prepare(expires_in: float | None = None, legacy_age: float | None = None) -> dict:
    """A prepared email in a conversation, waiting for the user's Send.
    expires_in moves its expiry; legacy_age makes it a file from before
    expires_at existed, created that many seconds ago."""
    from omarchy_ai.core import conversations, pending_actions

    cid = conversations.create("phone-text")
    aid = pending_actions.create(
        {
            "service": "gmail",
            "method": "GMAIL_SEND_EMAIL",
            "title": "Send email",
            "fields": [{"label": "To", "value": "someone@example.com"}],
            "arguments": {
                "recipient_email": "someone@example.com",
                "subject": "Prepared",
                "body": "Hi.",
                "user_id": "me",
            },
            "conversation": cid,
        }
    )
    with pending_actions._locked() as doc:
        action = doc.data[aid]
        if expires_in is not None:
            action["expires_at"] = time.time() + expires_in
        if legacy_age is not None:
            action.pop("expires_at", None)
            action["created"] = time.time() - legacy_age
    return {"id": aid, "conversation": cid}


def _quiet():
    from omarchy_ai.core import pending_actions
    from omarchy_ai.execution import myapi_agent

    notified: list[dict] = []
    pending_actions._notify = lambda title, body, cid: notified.append(
        {"title": title, "body": body, "conversation": cid}
    )
    myapi_agent._worked = lambda action, raw: True
    myapi_agent._sent_check = lambda action: None
    return notified


def _state(aid: str, cid: str) -> dict:
    from omarchy_ai.core import conversations, pending_actions

    conversations._cache.clear()
    conv = conversations._load(cid) or {"lines": []}
    return {
        "action": pending_actions.get(aid),
        "cards": [line for line in conv["lines"] if line.get("role") == "card"],
    }


def press(aid: str, cid: str) -> dict:
    """The user's Send on the card."""
    from omarchy_ai.execution import myapi_agent

    _quiet()
    ok, message = myapi_agent.decide(aid, True, "on the phone")
    return {"ok": ok, "message": message, **_state(aid, cid)}


def startup(aid: str, cid: str) -> dict:
    """What the daemon runs at startup: recover(), timed."""
    from omarchy_ai.core import pending_actions

    notified = _quiet()
    started = time.monotonic()
    changed = pending_actions.recover()
    return {
        "seconds": time.monotonic() - started,
        "changed": [a["id"] for a in changed],
        "notified": notified,
        **_state(aid, cid),
    }


def answer(aid: str, cid: str, choice: str) -> dict:
    """The user's check, resend or drop on an uncertain card."""
    from omarchy_ai.execution import myapi_agent

    _quiet()
    ok, message = myapi_agent.resolve(aid, choice, "on the phone")
    return {"ok": ok, "message": message, **_state(aid, cid)}
