"""Chaos-child programs for tests/chaos/test_outbox.py (DoD-7: a restart
during delivery never repeats a result).

A scheduled job's result goes to the inbox and through the outbox; each
transport appends what it would show to STATE_DIR/delivered.jsonl.
"""

from __future__ import annotations

import json

JOB = {
    "id": "backup",
    "title": "Nightly backup",
    "kind": "command",
    "next_run": 1_800_000_000.0,
}


def _record():
    from omarchy_ai import config
    from omarchy_ai.core import agenda, outbox

    log = config.STATE_DIR / "delivered.jsonl"
    config.STATE_DIR.mkdir(parents=True, exist_ok=True)

    def wrote(channel):
        def transport(title, body="", *rest, **kw):
            with log.open("a") as out:
                out.write(json.dumps({"channel": channel, "title": title}) + "\n")

        return transport

    outbox._desktop = wrote("desktop")
    outbox._push = wrote("push")
    outbox._desk_state = lambda: None  # presence unknown: desktop and phone
    outbox._phone_connected = lambda: False
    agenda._refresh_routines_hud = lambda: None
    return agenda, log


def report() -> dict:
    """The job finished: report it (the agenda's own call)."""
    agenda, log = _record()
    agenda._inbox_add(
        dict(JOB),
        "succeeded",
        "Command succeeded. Output tail: ok",
        title=JOB["title"],
        body="Command succeeded",
    )
    return state()


def state() -> dict:
    from omarchy_ai import config
    from omarchy_ai.core import agenda

    log = config.STATE_DIR / "delivered.jsonl"
    delivered = (
        [json.loads(line) for line in log.read_text().splitlines()]
        if log.exists()
        else []
    )
    return {
        "delivered": delivered,
        "inbox": [e["outcome"] for e in agenda._inbox_read()],
    }
