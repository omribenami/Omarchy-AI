"""Named points where a test can make the process die (tests/chaos/).

`crashpoint("name")` does nothing unless the environment variable
OMARCHY_CRASHPOINTS names it, so production pays one dict lookup on an
empty dict. A chaos test arms points with a comma-separated spec:

    OMARCHY_CRASHPOINTS="pending.after_claim=exit,agenda.before_save=raise"

- `exit`: the process ends on the spot with os._exit(CRASH_EXIT_CODE) --
  no finally blocks, no atexit, no buffered writes flushed, as close to a
  kill -9 as Python gets from inside.
- `raise`: CrashpointHit is raised. It is a BaseException so the broad
  `except Exception` handlers around tool calls cannot swallow it.

A bare name means `exit`. The spec is read once at import; tests running
in-process use arm() to change it.
"""

from __future__ import annotations

import os

ENV = "OMARCHY_CRASHPOINTS"
CRASH_EXIT_CODE = 86
MODES = ("exit", "raise")


class CrashpointHit(BaseException):
    def __init__(self, name: str) -> None:
        super().__init__(f"crashpoint {name}")
        self.name = name


def parse(spec: str) -> dict[str, str]:
    armed = {}
    for item in (spec or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, _, mode = item.partition("=")
        name, mode = name.strip(), (mode.strip() or "exit")
        if not name or mode not in MODES:
            raise ValueError(f"bad {ENV} entry {item!r}: use name=exit or name=raise")
        armed[name] = mode
    return armed


_armed: dict[str, str] = parse(os.environ.get(ENV, ""))


def arm(spec: str | dict[str, str] = "") -> None:
    """Replace the armed points (a spec string or {name: mode}); empty disarms all."""
    global _armed
    _armed = (
        parse(spec)
        if isinstance(spec, str)
        else parse(",".join(f"{k}={v}" for k, v in spec.items()))
    )


def crashpoint(name: str) -> None:
    if not _armed:
        return
    mode = _armed.get(name)
    if mode is None:
        return
    # Unbuffered, so the harness sees it even after os._exit.
    os.write(2, f"crashpoint {name} fired ({mode})\n".encode())
    if mode == "exit":
        os._exit(CRASH_EXIT_CODE)
    raise CrashpointHit(name)
