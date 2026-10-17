"""Shared process helpers every action module runs commands through.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass, field

from ..desktop_env import desktop_env


_TIMEOUT = 10

@dataclass
class ActionResult:
    ok: bool
    message: str = ""
    # The harness's own words (a refusal, a block), never fenced as untrusted
    # content even from a tool whose results are (execution/provenance.py).
    harness: bool = field(default=False, compare=False, repr=False)


def _desktop_env() -> dict[str, str]:
    """Backward-compatible alias for callers that imported this helper."""
    return desktop_env()


def _run(argv: list[str], timeout: float = _TIMEOUT, cwd: str | None = None) -> ActionResult:
    # Output goes to temporary files, not pipes: a script that starts a
    # background program (omarchy-toggle-nightlight's `setsid uwsm-app --
    # hyprsunset &`) hands it the same descriptors, and a pipe would then
    # not reach EOF until that program exited -- the call would sit out the
    # whole timeout although the script itself finished at once. With files,
    # subprocess.run only waits for the process it started.
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err_file:
        try:
            proc = subprocess.run(argv, stdout=out, stderr=err_file, timeout=timeout, check=False, cwd=cwd,
                                  env=_desktop_env())
        except FileNotFoundError:
            return ActionResult(False, f"{argv[0]} is not installed")
        except subprocess.TimeoutExpired:
            return ActionResult(False, f"{argv[0]} timed out")
        out.seek(0)
        err_file.seek(0)
        stdout = out.read().decode(errors="replace")
        stderr = err_file.read().decode(errors="replace")
    if proc.returncode != 0:
        err = (stderr or stdout or "").strip().splitlines()
        return ActionResult(False, err[0] if err else f"{argv[0]} failed")
    return ActionResult(True, stdout.strip())


def _run_detached(argv: list[str]) -> ActionResult:
    """Launch and don't wait — omarchy-launch-* scripts exec into the app
    itself rather than forking, so waiting would hang until the user closed
    the launched window."""
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=_desktop_env(),
        )
    except FileNotFoundError:
        return ActionResult(False, f"{argv[0]} is not installed")
    return ActionResult(True, "")


def _clamp_percent(value) -> int:
    if value is None:
        value = 50
    return max(0, min(100, int(value)))


def _hyprctl_dispatch(lua_expr: str, classic_argv: list[str]) -> ActionResult:
    r = _run(["hyprctl", "dispatch", lua_expr])
    if r.ok:
        return r
    return _run(["hyprctl", "dispatch", *classic_argv])
