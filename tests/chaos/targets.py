"""Small headless programs the kit's self-tests run in a chaos child.

Each one drives real core modules against the child's temp state, with
crashpoints placed here rather than in production code. Anything that
would reach the desktop (HUD refreshes) is stubbed out first.
"""

from __future__ import annotations

from omarchy_ai.core.crashpoints import crashpoint


def where() -> dict:
    from omarchy_ai import config

    return {
        name: str(getattr(config, name))
        for name in ("CONFIG_DIR", "STATE_DIR", "CACHE_DIR", "RUNTIME_DIR")
    }


def decide_action(title: str = "Send the fake email") -> str:
    """Prepare, claim and finish one pending action, as execution/myapi_agent.decide does."""
    from omarchy_ai.core import pending_actions

    aid = pending_actions.create(
        {
            "title": title,
            "service": "gmail",
            "method": "GMAIL_SEND_EMAIL",
            "arguments": {},
        }
    )
    crashpoint("chaos.after_create")
    pending_actions.claim(aid)
    crashpoint("chaos.after_claim")
    try:
        crashpoint("chaos.before_finish")
    finally:
        pending_actions.finish(aid, "done", "finished")
    return aid


def pending_snapshot() -> dict:
    from omarchy_ai.core import pending_actions

    return pending_actions._load()


def add_reminder(title: str) -> str:
    from omarchy_ai.core import agenda

    agenda._refresh_routines_hud = lambda: None
    job = agenda.create({"kind": "remind", "title": title, "in_minutes": 30})
    crashpoint("chaos.after_agenda_create")
    return job["id"]


def agenda_snapshot() -> list:
    from omarchy_ai.core import agenda

    return agenda._load()


def myapi_send(subject: str) -> bool:
    """One proxied write through the real client; the fake server is set up by the child."""
    from omarchy_ai.myapi import MyApiClient

    MyApiClient().call_service(
        "gmail", "/gmail/v1/users/me/messages/send", "POST", body={"subject": subject}
    )
    crashpoint("chaos.after_dispatch")
    return True
