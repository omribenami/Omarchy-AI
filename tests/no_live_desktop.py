"""Tests never run a real desktop or device command.

Importing this module wraps ``subprocess.Popen``, which ``subprocess.run``,
``check_output``, ``call`` and ``asyncio.create_subprocess_exec`` all start
processes through. A test that would run one of the binaries below (Hyprland,
the Omarchy shell, input injection, audio, phone, notifications, systemd...)
fails with LiveDesktopCall instead of reaching the user's session: mock the
call, or point it at a fake binary. Fake binaries in a temporary directory or
in this repository still run, so tests of subprocess wrappers keep working.
It also keeps every test off the user's keyring (see _no_keyring below).

tests/conftest.py imports this for pytest; test_no_live_desktop.py imports it
during ``python -m unittest discover -s tests``, which loads every test module
before running any test.
"""

import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.execution import sudo_approval

# The desktop and device binaries src/omarchy_ai runs; every omarchy-* command too.
DESKTOP_BINARIES = frozenset(
    {
        "adb",
        "arecord",
        "avahi-browse",
        "bluetoothctl",
        "brave",
        "brightnessctl",
        "busctl",
        "chromium",
        "firefox",
        "google-chrome",
        "grim",
        "hyprctl",
        "loginctl",
        "nmcli",
        "notify-send",
        "omarchy",
        "pactl",
        "playerctl",
        "pw-cli",
        "pw-dump",
        "pw-metadata",
        "pw-play",
        "pw-record",
        "pw-top",
        "secret-tool",
        "slurp",
        "systemctl",
        "systemd-run",
        "tailscale",
        "tmux",
        "uwsm",
        "uwsm-app",
        "walker",
        "wl-copy",
        "wl-paste",
        "wpctl",
        "wtype",
        "xdg-open",
        "ydotool",
    }
)

_ALLOWED_ROOTS = (
    Path(tempfile.gettempdir()).resolve(),
    Path(__file__).resolve().parent.parent,
)


class LiveDesktopCall(BaseException):
    """A test tried to run a real desktop or device command.

    A BaseException, so the code under test's ``except Exception`` or
    ``except OSError`` fallbacks cannot swallow it.
    """


def _program(args, shell: bool) -> str:
    if isinstance(args, (str, bytes, os.PathLike)):
        args = os.fsdecode(args)
        return shlex.split(args)[0] if shell and args.strip() else args
    return os.fsdecode(args[0]) if args else ""


def _is_fake(program: str, env) -> bool:
    if os.sep in program:
        found = program
    else:
        found = shutil.which(
            program, path=(env if env is not None else os.environ).get("PATH")
        )
    if not found:
        return False
    path = Path(found).resolve()
    return any(path.is_relative_to(root) for root in _ALLOWED_ROOTS)


def check(args, shell: bool = False, env=None) -> None:
    """Raise LiveDesktopCall if ``args`` would run a real desktop or device binary."""
    if (
        isinstance(args, (list, tuple))
        and args
        and os.path.basename(os.fsdecode(args[0])) == "bwrap"
        and "--" in args
    ):
        # The sandbox (runtime/sandbox.py) runs harmless commands for real in
        # tests; what it runs inside is checked like any other command.
        inner = list(args[list(args).index("--") + 1 :])
        if inner:
            check(inner, env=env)
        return
    program = _program(args, shell)
    name = os.path.basename(program)
    if (name in DESKTOP_BINARIES or name.startswith("omarchy-")) and not _is_fake(
        program, env
    ):
        raise LiveDesktopCall(f"test tried to run {name!r} for real: {args!r}")


# No test reads or writes the user's keyring (the saved sudo and spoken
# passwords): secret-tool behaves as on a machine without libsecret, as in CI.
# Redaction reads the keyring on every saved job and log line, from any test.
def _no_keyring(args, *, password=None):
    raise OSError("no keyring in tests")


sudo_approval._run = _no_keyring


def without_session_probe():
    """Module fixtures for tests whose runtime commands would ask Hyprland for
    the live session's environment (hyprctl instances) in passing:

        setUpModule, tearDownModule = no_live_desktop.without_session_probe()

    Entered rather than started, so a test's patch.stopall() leaves it in place.
    """
    stack = contextlib.ExitStack()

    def set_up_module() -> None:
        target = "omarchy_ai.execution.actions.base.desktop_env"
        stack.enter_context(patch(target, lambda: dict(os.environ)))

    return set_up_module, stack.close


_real_init = subprocess.Popen.__init__


def _guarded_init(self, args, *pargs, **kwargs):
    check(args, shell=kwargs.get("shell", False), env=kwargs.get("env"))
    _real_init(self, args, *pargs, **kwargs)


subprocess.Popen.__init__ = _guarded_init  # type: ignore[method-assign]
