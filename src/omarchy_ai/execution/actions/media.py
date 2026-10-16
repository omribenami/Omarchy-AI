"""Audio output/input and MPRIS media-player controls.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

from .base import ActionResult, _clamp_percent, _run


# --- audio -------------------------------------------------------------

def volume_up(args: dict) -> ActionResult:
    return _run(["omarchy-audio-output-volume", "raise"])


def volume_down(args: dict) -> ActionResult:
    return _run(["omarchy-audio-output-volume", "lower"])


def volume_mute_toggle(args: dict) -> ActionResult:
    return _run(["omarchy-audio-output-volume", "mute-toggle"])


def volume_set(args: dict) -> ActionResult:
    pct = _clamp_percent(args.get("percent"))
    r = _run(["pactl", "set-sink-volume", "@DEFAULT_SINK@", f"{pct}%"])
    if not r.ok:
        return r
    _run(["pactl", "set-sink-mute", "@DEFAULT_SINK@", "0"])
    _run(["omarchy-osd", "-i", "volume-high", "-p", str(pct)])
    return ActionResult(True, f"volume set to {pct} percent")


def mic_mute_toggle(args: dict) -> ActionResult:
    return _run(["omarchy-audio-input-mute"])


# --- media (MPRIS on the session bus, through systemd's busctl) --------------

_MPRIS = "org.mpris.MediaPlayer2"
# Real bug (2026-10-10): "stop the music" went to playerctl, which was not
# installed, and the music was on a house speaker anyway. The message has to
# tell the model to look at the other device instead of retrying this one.
NO_MEDIA_PLAYER = (
    "nothing is playing on this computer (no media player is running here); "
    "if the music is on another device, control that device instead"
)


def _mpris_status(player: str) -> str:
    r = _run(
        ["busctl", "--user", "get-property", player, "/org/mpris/MediaPlayer2",
         f"{_MPRIS}.Player", "PlaybackStatus"],
        timeout=3,
    )
    parts = r.message.split('"')
    return parts[1] if r.ok and len(parts) > 2 else ""


def _mpris(method: str) -> ActionResult:
    listed = _run(["busctl", "--user", "list", "--no-legend"], timeout=3)
    if not listed.ok:
        return listed
    players = [line.split()[0] for line in listed.message.splitlines() if line.startswith(f"{_MPRIS}.")]
    if not players:
        return ActionResult(False, NO_MEDIA_PLAYER)
    status = {player: _mpris_status(player) for player in players}
    player = next(
        (p for wanted in ("Playing", "Paused") for p in players if status[p] == wanted), players[0]
    )
    r = _run(["busctl", "--user", "call", player, "/org/mpris/MediaPlayer2", f"{_MPRIS}.Player", method], timeout=5)
    if not r.ok:
        return r
    return ActionResult(True, f"{method} sent to {player.removeprefix(_MPRIS + '.')}")


def media_play_pause(args: dict) -> ActionResult:
    return _mpris("PlayPause")


def media_next(args: dict) -> ActionResult:
    return _mpris("Next")


def media_prev(args: dict) -> ActionResult:
    return _mpris("Previous")
