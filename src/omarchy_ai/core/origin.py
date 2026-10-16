"""Where a request comes from: the channel, the place and the person.

Every decision about a request (Jev's fast pass and switchboard, the tool
catalog, skill choice, a task's goal) reads it the same way. 2026-10-15
19:58: "make it a bit calmer here", said to the speaker in the room, became
the night light on this computer's screen -- nobody deciding knew the
request came from a speaker in another room, not from someone at the desk.
"""
from __future__ import annotations

from ..config import load_config
from . import conversations, identities

# conversation source -> (what the user is talking to, whether they are at this computer)
CHANNELS = {
    "satellite": ("the voice speaker", False),
    "phone-voice": ("the phone app, by voice", False),
    "phone-text": ("the phone app, typing", False),
    "desktop-voice": ("this computer's microphone", True),
    "desktop-text": ("this computer's chat window, typing", True),
    "system": ("no one: a scheduled or background job", False),
}


def current(conversation: str | None = None) -> dict:
    """{"channel", "talking_to", "at_this_computer", "room", "person"} for the
    request being handled now (the conversation in context, or the one
    given). Unknown parts are None, never guessed."""
    cid = conversation if conversation is not None else conversations.CURRENT.get()
    try:
        channel = conversations.source(cid) if cid else ""
    except Exception:  # noqa: BLE001 -- origin only informs a decision
        channel = ""
    channel = channel.removeprefix("test-")
    talking_to, at_computer = CHANNELS.get(channel, (None, None))
    room = None
    if channel == "satellite":
        room = (getattr(load_config(), "satellite_room", "") or "").strip() or None
    who = identities.current()
    return {"channel": channel or None, "talking_to": talking_to, "at_this_computer": at_computer,
            "room": room, "person": identities.name(who.person) if who.person else None,
            "person_known_by": who.how if who.person else None}


def describe(info: dict | None = None) -> str:
    """One sentence for a prompt or a task goal; "" when nothing is known."""
    info = current() if info is None else info
    if not info.get("channel"):
        return ""
    where = info["talking_to"] or info["channel"]
    if info.get("room"):
        where += f" in the {info['room']}"
    parts = [f"This request came through {where}."]
    if info.get("at_this_computer") is False:
        parts.append("The user is not at this computer, so 'here' means where they are, not its screen.")
    elif info.get("at_this_computer"):
        parts.append("The user is at this computer.")
    if info.get("person"):
        parts.append(f"It is from {info['person']}.")
    return " ".join(parts)
