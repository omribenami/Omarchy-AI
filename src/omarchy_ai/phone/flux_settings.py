"""Turn on a Flux phone feature from the phone itself.

Several Flux features are off until a key in the computer's
~/.config/flux/config.toml turns them on. Flux's Omarchy AI button next to
such a feature asks for it here, and the change goes through fluxd's own
`settings.set` (the call the Flux desktop window uses): it saves
config.toml and applies the change live, then `state` confirms it.

Only these keys, and only on: a paired phone asked for exactly this, and
turning a feature off stays with Flux's own settings.
"""

from __future__ import annotations

from ..execution.flux_approve import Fluxd

# config.toml key -> fluxd's setting name.
KEYS = {
    "remote_input": "remoteInput",
    "remote_desktop": "remoteDesktop",
    "herdr": "herdr",
    "herdr_control": "herdrControl",
    "herdr_terminals": "herdrTerminals",
}


def enable(keys: list[str]) -> tuple[bool, str]:
    names = [KEYS.get(k) for k in keys]
    if not keys or None in names:
        return False, f"only these settings can be turned on here: {', '.join(KEYS)}"
    try:
        fluxd = Fluxd(timeout=3)
    except OSError as exc:
        return False, f"Flux is not running on this computer: {exc}"
    try:
        for name in names:
            fluxd.call("settings.set", {"key": name, "value": True}, timeout=5)
        settings = fluxd.call("state", {}, timeout=5).get("settings") or {}
    except (OSError, ValueError) as exc:
        return False, f"Flux did not take the change: {exc}"
    finally:
        fluxd.close()
    missing = [key for key, name in zip(keys, names) if settings.get(name) is not True]
    if missing:
        return False, f"Flux still reports {', '.join(missing)} as off"
    return True, f"turned on {', '.join(keys)}"
