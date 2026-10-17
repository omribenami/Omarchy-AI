"""Actions Omarchy prepared that wait for the user's OK (execution/myapi_agent.py).

A MyApi call that sends, changes or deletes something is not run when it is
prepared: it waits here, and as a confirm card in its conversation, until the
user taps Send or Cancel on the phone or says yes or no in that chat. Kept in
the daemon's memory and on disk, so a restart does not lose one. Every change
is a locked read-modify-write (core/statefile.py), so the daemon and a CLI
cannot both claim one.

States: waiting -> running -> done (outcome sent, done, failed, cancelled,
dropped), or running -> uncertain when the process sending it died before
MyApi answered. An uncertain one waits for the user: check, resend (a new,
deliberate send under a new origin) or drop.

recover() (run at daemon startup and on each heartbeat) settles what a dead
process left behind, from the side-effect journal (core/effects.py), and
never sends anything itself:
- running, sent and confirmed -> done, with the stored result;
- running, failed or never dispatched -> done/failed, with the reason;
- running, dispatched with no answer -> uncertain, and a card asks the user;
- waiting past expires_at -> done/cancelled, and the user is told.
"""
from __future__ import annotations

import logging
import secrets
import time

from ..config import STATE_DIR
from . import statefile

log = logging.getLogger(__name__)

PATH = STATE_DIR / "pending_actions.json"
KEEP_SECONDS = 7 * 86400
# How long a prepared action waits for the user's answer. Files from before
# expires_at existed get created + this (the old rule: kept 7 days).
WAIT_SECONDS = KEEP_SECONDS
# A running action whose claimer is unknown (an old file) is stuck after this.
STALE_RUNNING_SECONDS = 120

WAITING, RUNNING, DONE, UNCERTAIN = "waiting", "running", "done", "uncertain"
ACTION = "action:"  # the task_id prefix of an action's cards (execution/myapi_agent.py)


def _load() -> dict:
    return statefile.read_json(PATH, {}, expect=dict)


def _locked():
    """The file under its cross-process lock (core/statefile.py); same shape as ever."""
    return statefile.locked_json(PATH, {}, expect=dict)


def _pruned(data: dict) -> dict:
    """Settled actions go a week after they were decided; open ones stay until settled."""
    now = time.time()
    return {k: v for k, v in data.items()
            if v.get("status") in (WAITING, RUNNING)
            or now - float(v.get("decided") or v.get("created") or 0) < KEEP_SECONDS}


def expires_at(action: dict) -> float:
    try:
        return float(action["expires_at"])
    except (KeyError, TypeError, ValueError):
        return float(action.get("created") or 0) + WAIT_SECONDS


def expired(action: dict, now: float | None = None) -> bool:
    return action.get("status") == WAITING and (time.time() if now is None else now) >= expires_at(action)


def origin_of(action: dict) -> str:
    """What the action's send is journaled under (core/effects.py)."""
    return str(action.get("origin") or f"pending:{action['id']}")


def effect_key(action: dict) -> str:
    """The journal key of the action's send under its current origin."""
    from ..execution.actions.myapi_tools import myapi_write_key
    return myapi_write_key(origin_of(action), str(action.get("service") or ""), str(action.get("method") or ""),
                           action.get("arguments") or {})


def _owner() -> dict:
    from ..runtime.task import this_process
    return this_process()


def create(action: dict) -> str:
    aid = secrets.token_hex(6)
    now = time.time()
    with _locked() as doc:
        doc.data[aid] = {**action, "id": aid, "created": now, "expires_at": now + WAIT_SECONDS,
                         "status": WAITING, "outcome": ""}
        doc.data = _pruned(doc.data)
    return aid


def get(aid) -> dict | None:
    if not isinstance(aid, str):
        return None
    return _load().get(aid)


def claim(aid: str) -> dict | None:
    """The action, marked as being decided, if it still waits (only one caller wins)."""
    with _locked() as doc:
        action = doc.data.get(aid)
        if not action or action.get("status") != WAITING or expired(action):
            return None
        action.update(status=RUNNING, claimed=time.time(), owner=_owner(), origin=origin_of(action))
        doc.data = _pruned(doc.data)
        return dict(action)


def claim_resend(aid: str) -> dict | None:
    """An uncertain action, running again for the user's Resend, under a new
    origin: a deliberate second send, never a replay of the first."""
    with _locked() as doc:
        action = doc.data.get(aid)
        if not action or action.get("status") != UNCERTAIN:
            return None
        resends = int(action.get("resends") or 0) + 1
        action.update(status=RUNNING, claimed=time.time(), owner=_owner(), resends=resends,
                      origin=f"pending:{aid}:resend:{resends}:{secrets.token_hex(4)}")
        doc.data = _pruned(doc.data)
        return dict(action)


def finish(aid: str, outcome: str, message: str = "") -> None:
    with _locked() as doc:
        if aid in doc.data:
            doc.data[aid].update(status=DONE, outcome=outcome, message=message[:500], decided=time.time())
            doc.data = _pruned(doc.data)


def mark_uncertain(aid: str, message: str) -> None:
    """A send that got no answer: it may have gone out, so the user decides."""
    with _locked() as doc:
        if aid in doc.data:
            doc.data[aid].update(status=UNCERTAIN, outcome=UNCERTAIN, message=message[:500], decided=time.time())
            doc.data[aid].pop("owner", None)
            doc.data = _pruned(doc.data)


def uncertain_card(action: dict) -> dict:
    """The card that asks the user about an action that may have been sent."""
    return {"kind": UNCERTAIN, "task_id": f"{ACTION}{action['id']}", "text": str(action.get("message") or ""),
            "choices": ["check", "resend", "drop"],
            "action": {"id": action["id"], "title": str(action.get("title") or ""),
                       "service": action.get("service", ""), "method": action.get("method", ""),
                       "fields": action.get("fields") or []}}


def settle_uncertain(aid: str, outcome: str, message: str = "") -> bool:
    """Close an uncertain action (the user's Drop, or a check that found it); False if it is not uncertain."""
    with _locked() as doc:
        action = doc.data.get(aid)
        if not action or action.get("status") != UNCERTAIN:
            return False
        action.update(status=DONE, outcome=outcome, message=message[:500], decided=time.time())
        doc.data = _pruned(doc.data)
        return True


# -- recovery ------------------------------------------------------------------

def _stuck(action: dict, now: float) -> bool:
    """A running action nobody is deciding any more: its claimer died, or
    (an old file, claimer unknown) it has been running for 2 minutes."""
    owner = action.get("owner")
    if owner:
        from ..runtime.task import owner_alive
        return not owner_alive(owner)
    return now - float(action.get("claimed") or action.get("created") or 0) >= STALE_RUNNING_SECONDS


def _reconcile(action: dict, now: float) -> dict | None:
    """The change for a stuck running action, from its effect record; None
    while its send is still in flight in a live process."""
    from . import effects
    title = str(action.get("title") or action.get("method") or "The action")
    key = effect_key(action)
    if effects.in_flight(key):
        return None
    effect = effects.lookup(key)
    state = effect.state if effect else None
    stored = (effect.result or {}) if effect else {}
    if state == effects.CONFIRMED:
        return {"status": DONE, "outcome": "done", "result": stored,
                "message": f"{title}: done ✓ (MyApi confirmed it before Omarchy AI stopped)."}
    if state == effects.FAILED:
        return {"status": DONE, "outcome": "failed", "result": stored,
                "message": f"{title} failed: {str(stored.get('message') or 'MyApi refused it')[:200]}"}
    if state in (effects.DISPATCHED, effects.UNCERTAIN):
        return {"status": UNCERTAIN, "outcome": UNCERTAIN, "effect_id": effect.effect_id if effect else "",
                "message": f"I may have sent this ({title}): Omarchy AI stopped while sending it, before MyApi "
                           "answered. Check whether it went out, then resend or drop it."}
    if action.get("tool"):
        # A tapped tool call (execution/myapi_agent.ask_tap) keeps no MyApi
        # record of its own here: unknown means it may have run.
        return {"status": UNCERTAIN, "outcome": UNCERTAIN,
                "message": f"{title} may have run: Omarchy AI stopped while running it. Check whether it "
                           "happened, then resend or drop it."}
    return {"status": DONE, "outcome": "failed",
            "message": f"{title} was not sent: Omarchy AI stopped before sending it, so nothing went out. "
                       "Ask again to send it."}


def recover(now: float | None = None) -> list[dict]:
    """Settle stuck running actions and expired waiting ones (see the module
    docstring), then put a card in each one's chat and notify the user.
    Returns the actions it changed."""
    now = time.time() if now is None else now
    changed: list[dict] = []
    if not any(a.get("status") == RUNNING or expired(a, now) for a in _load().values()):
        return changed  # the usual heartbeat: nothing to settle, no lock taken
    with _locked() as doc:
        for aid, action in doc.data.items():
            if action.get("status") == RUNNING and _stuck(action, now):
                change = _reconcile({**action, "id": aid}, now)
            elif expired(action, now):
                when = time.strftime("%H:%M on %Y-%m-%d", time.localtime(expires_at(action)))
                change = {"status": DONE, "outcome": "cancelled",
                          "message": f"{action.get('title') or 'The action'} expired: it waited for your OK until "
                                     f"{when}, so it was not sent."}
            else:
                continue
            if change is None:
                continue
            action.update(change, decided=now, message=change["message"][:500])
            action.pop("owner", None)
            changed.append(dict(action))
        if changed:
            doc.data = _pruned(doc.data)
    for action in changed:
        log.info("pending action %s recovered: %s (%s)", action.get("id"), action["status"], action["outcome"])
        _tell(action)
    return changed


_EVENTS = {"done": "certified", "failed": "failed", "cancelled": "cancelled"}
_HEADINGS = {"done": "Done", "failed": "Not sent", "cancelled": "Expired", UNCERTAIN: "May have sent"}


def _tell(action: dict) -> None:
    aid, cid, title = action["id"], str(action.get("conversation") or ""), str(action.get("title") or "")
    if cid:
        from . import conversations
        if action["status"] == UNCERTAIN:
            card = uncertain_card(action)
        else:
            card = {"kind": "result", "task_id": f"{ACTION}{aid}", "event": _EVENTS[action["outcome"]],
                    "text": action["message"]}
        try:
            conversations.add_card(cid, card)
        except Exception:  # noqa: BLE001 -- the notification still goes out
            log.warning("pending action %s: card not added", aid, exc_info=True)
    try:
        _notify(f"{_HEADINGS[action['outcome']]}: {title}", action["message"], cid)
    except Exception:  # noqa: BLE001
        log.warning("pending action %s: notification not sent", aid, exc_info=True)


def _notify(title: str, body: str, conversation: str) -> None:
    from . import agenda
    agenda.notify(title, body, urgent=title.startswith(_HEADINGS[UNCERTAIN]), conversation=conversation)
