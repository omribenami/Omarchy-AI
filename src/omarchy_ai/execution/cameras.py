"""The user's cameras, through their own program (config.camera_snapshot_command).

What the cameras are and how a frame is fetched is the user's setup, so it
lives in their program, not here. Its contract: `<command> list` prints a
JSON list of camera names, and `<command> snapshot <name> <file>` writes the
current frame to <file>.

Optional: `<command> wait <name> <seconds>` blocks until that camera's motion
sensor reports movement (exit 0) or the time passes without any (exit 3).
Any other exit means the program cannot wait on that camera, and a watch on
it looks on a timer instead. `wait <name> 0` only answers whether it can.

Optional: `<command> focus <name> <file>` writes a close-up of the thing the
camera's detector is tracking right now (a person, a car) to <file> (exit 0),
or exits 3 when nothing is being tracked. A watch judges that close-up next
to the full frame, since someone walking up the drive is a few dozen pixels
of it, and keeps looking every few seconds while something is tracked.
"""
from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

from ..config import CACHE_DIR

FRAME_DIR = CACHE_DIR / "camera_frames"
_TIMEOUT = 30
MOTION, QUIET = 0, 3  # exit codes of `wait`


def _run(config, *args: str) -> str:
    command = getattr(config, "camera_snapshot_command", None)
    if not command:
        raise LookupError("no cameras are set up (camera_snapshot_command is not configured)")
    try:
        proc = subprocess.run([*shlex.split(command), *args], capture_output=True, text=True,
                              timeout=_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        raise LookupError("the camera program timed out") from None
    if proc.returncode != 0:
        raise LookupError((proc.stderr or proc.stdout or "the camera program failed").strip()[:300])
    return proc.stdout


def names(config) -> list[str]:
    try:
        found = json.loads(_run(config, "list"))
    except ValueError:
        raise LookupError("the camera program did not print a JSON list of names") from None
    if not isinstance(found, list):
        raise LookupError("the camera program did not print a JSON list of names")
    return [str(name) for name in found if isinstance(name, str) and name.strip()][:100]


def resolve(name: str, config) -> str:
    """The camera's exact name, so a watch cannot be created on a guess.

    A partial, Hebrew or misspelled name resolves when exactly one camera
    fits it (execution/naming.py): live 2026-10-26, "Door", "הדלת" and
    "הדלת, הרינג" each failed against a camera called "front door"."""
    known = names(config)
    said = name.strip().casefold()
    spoken = said.removeprefix("the ").removesuffix(" camera").strip()  # "the driveway camera"
    match = next((k for k in known if k.casefold() in (said, spoken)), None)
    if match is not None:
        return match
    from . import naming
    match, options = naming.match(name, {k: [k] for k in known})
    if match is not None:
        return match
    if len(options) > 1:
        raise ValueError(f"{name!r} could be any of these cameras: {', '.join(options)}. Ask the user which one, "
                         "then call again with that exact name.")
    raise ValueError(f"no camera named {name!r}; the cameras are: {', '.join(known) or 'none'}. "
                     "Call again with one of these exact names.")


def snapshot(name: str, config, key: str) -> Path:
    """The camera's current frame, kept as FRAME_DIR/<key> until the next one."""
    from .vision import _valid_image

    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    path = FRAME_DIR / key
    path.unlink(missing_ok=True)  # a failed fetch must not leave the last frame to be judged again
    _run(config, "snapshot", name, str(path))
    if not _valid_image(path):
        raise LookupError(f"camera {name!r} returned no image")
    return path


def focus(name: str, config, key: str) -> Path | None:
    """A close-up of what the camera's detector tracks now, kept as
    FRAME_DIR/<key>; None when nothing is tracked or the program has no
    `focus`."""
    from .vision import _valid_image

    command = getattr(config, "camera_snapshot_command", None)
    if not command:
        return None
    FRAME_DIR.mkdir(parents=True, exist_ok=True)
    path = FRAME_DIR / key
    path.unlink(missing_ok=True)
    try:
        proc = subprocess.run([*shlex.split(command), "focus", name, str(path)], capture_output=True, text=True,
                              timeout=_TIMEOUT, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0 or not _valid_image(path):
        path.unlink(missing_ok=True)
        return None
    return path


def motion_process(name: str, config, seconds: int) -> subprocess.Popen:
    """The running `wait`; its exit code is MOTION, QUIET or a failure. The
    caller owns it, so a cancelled watch can end the wait at once."""
    command = getattr(config, "camera_snapshot_command", None)
    if not command:
        raise LookupError("no cameras are set up (camera_snapshot_command is not configured)")
    return subprocess.Popen([*shlex.split(command), "wait", name, str(int(seconds))], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def can_wait(name: str, config) -> bool:
    try:
        return motion_process(name, config, 0).wait(timeout=_TIMEOUT) in (MOTION, QUIET)
    except (OSError, LookupError, ValueError, subprocess.SubprocessError):
        return False
