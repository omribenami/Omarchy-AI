"""The phone-push transport of the outbox (core/outbox.py), through Flux.

Flux shows a notification on its paired phone for fluxd's `notify.send`
(the call behind `flux-cli notify TITLE BODY`). Nothing calls this module to
tell the user something: the outbox decides whether a message goes to the
phone (the user is away from the desk, the phone page is open, or it is
urgent) and calls deliver() off its caller's thread.

Best effort: without Flux, or with no phone connected, nothing happens.
`flux_notifications: false` in config.yaml turns it off.
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


def _masked(text: str) -> str:
    """A phone notification never carries a saved secret (execution/passwords.py)."""
    from ..execution.passwords import for_speech
    return for_speech(text)


def _titled(title: str, kind: str) -> str:
    """The title as the phone shows it (deliver adds the marker)."""
    marker = "Omarchy AI · Approval: " if kind == "approval" else "Omarchy AI · "
    return title if title.startswith(marker) else marker + title.removeprefix("Omarchy AI · ")


def deliver(title: str, body: str = "", kind: str = "other", devices: list[str] | None = None) -> int:
    """Sends now and returns how many phones got it (for tests and callers that wait)."""
    from ..execution.flux_approve import Fluxd
    title, body = _titled(_masked(title), kind), _masked(body)
    try:
        fluxd = Fluxd(timeout=2)
    except OSError:
        return 0  # no Flux on this computer
    sent = 0
    try:
        connected = fluxd.call("state", {}, timeout=5).get("devices") or []
        for device in connected:
            if not (device.get("paired") and device.get("online")) or (devices and device.get("id") not in devices):
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
