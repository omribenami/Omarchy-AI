"""Omarchy AI's notifications on the phone, through Flux.

Flux shows a notification on its paired phone for fluxd's `notify.send`
(the call behind `flux-cli notify TITLE BODY`). Every desktop notification
Omarchy raises for the user -- a task that needs approval or has a question,
a finished or failed task, a routine, a provider out of credit, a tool that
asks to be installed -- is sent there too, so it reaches the user away
from the desk.

Best effort and off the caller's thread: without Flux, or with no phone
connected, nothing happens. `flux_notifications: false` in config.yaml
turns it off.
"""

from __future__ import annotations

import logging
import threading

log = logging.getLogger(__name__)


def _enabled() -> bool:
    try:
        from ..config import load_config
        return bool(getattr(load_config(), "flux_notifications", True))
    except Exception:  # noqa: BLE001 -- a notification must never break its caller
        return True


def send(title: str, body: str = "", *, kind: str = "other", conversation: str = "") -> None:
    """Show `title` and `body` on every paired phone that Flux has connected now.
    With a `conversation`, a tap on it opens that chat in Flux."""
    if not title or not _enabled():
        return
    if conversation:
        from ..core import conversations
        conversations.notified(_titled(title, kind)[:120], conversation)
    threading.Thread(target=deliver, args=(title, body, kind), daemon=True, name="flux-notify").start()


def _titled(title: str, kind: str) -> str:
    """The title as the phone shows it (deliver adds the marker)."""
    marker = "Omarchy AI · Approval: " if kind == "approval" else "Omarchy AI · "
    return title if title.startswith(marker) else marker + title.removeprefix("Omarchy AI · ")


def deliver(title: str, body: str = "", kind: str = "other") -> int:
    """Sends now and returns how many phones got it (for tests and callers that wait)."""
    from ..execution.flux_approve import Fluxd
    title = _titled(title, kind)
    try:
        fluxd = Fluxd(timeout=2)
    except OSError:
        return 0  # no Flux on this computer
    sent = 0
    try:
        devices = fluxd.call("state", {}, timeout=5).get("devices") or []
        for device in devices:
            if not (device.get("paired") and device.get("online")):
                continue
            try:
                fluxd.call("notify.send", {"device": device["id"], "title": title[:120], "body": body[:600]}, timeout=5)
                sent += 1
            except OSError as exc:
                log.info("flux notify: %s did not take it: %s", device.get("name"), exc)
    except (OSError, ValueError) as exc:
        log.info("flux notify: fluxd did not answer: %s", exc)
    finally:
        fluxd.close()
    return sent
