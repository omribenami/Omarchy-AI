"""The user's cameras, through their own program (config.camera_snapshot_command).

What the cameras are and how a frame is fetched is the user's setup, so it
lives in their program, not here. Its contract: `<command> list` prints a
JSON list of camera names, and `<command> snapshot <name> <file>` writes the
current frame to <file>.

Optional: `<command> wait <name> <seconds>` blocks until that camera's motion
sensor reports movement (exit 0) or the time passes without any (exit 3).
Any other exit means the program cannot wait on that camera, and a watch on
it looks on a timer instead. `wait <name> 0` only answers whether it can.
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
    """The camera's exact name, so a watch cannot be created on a guess."""
    known = names(config)
    said = name.strip().casefold()
    spoken = said.removeprefix("the ").removesuffix(" camera").strip()  # "the driveway camera"
    match = next((k for k in known if k.casefold() in (said, spoken)), None)
    if match is None:
        raise ValueError(f"no camera named {name!r}; the cameras are: {', '.join(known) or 'none'}")
    return match


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
