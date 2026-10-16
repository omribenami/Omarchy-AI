"""Run core code in a child process, let it die at a crashpoint, restart it.

    with ChaosHarness() as h:
        crashed, after = h.crash_then_restart(
            "chaos.targets:decide_action", "chaos.after_claim",
            restart="chaos.targets:pending_snapshot")
        assert crashed.crashed_at == "chaos.after_claim"
        assert after.result[...]["status"] == "running"

A target is "module:function" importable with tests/ and src/ on the path.
Its return value must be JSON-serializable; it comes back as `.result`.

Every child gets its own HOME and XDG config/state/cache/runtime dirs
under one temp root, and a minimal environment (no display, no D-Bus, no
audio server). The child refuses to run unless omarchy_ai.config resolves
every directory inside that root, so nothing can reach the user's files.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from omarchy_ai.core.crashpoints import CRASH_EXIT_CODE, ENV as CRASHPOINTS_ENV

TESTS_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = TESTS_DIR.parent / "src"
CHILD = Path(__file__).resolve().parent / "_child.py"
RESULT_PREFIX = "CHAOS-RESULT "
ISOLATION_EXIT_CODE = 3
_FIRED = re.compile(r"^crashpoint (\S+) fired", re.M)
_KEEP_ENV = ("PATH", "LANG", "LC_ALL", "TZ")


@dataclass
class ChildRun:
    returncode: int | None
    stdout: str
    stderr: str
    result: object = None
    timed_out: bool = False

    @property
    def crashed_at(self) -> str | None:
        """The crashpoint that ended the child, if one did."""
        if self.returncode != CRASH_EXIT_CODE:
            return None
        fired = _FIRED.findall(self.stderr)
        return fired[-1] if fired else None

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class ChaosHarness:
    def __init__(self, root: str | Path | None = None) -> None:
        self._own_root = root is None
        self.root = Path(root or tempfile.mkdtemp(prefix="omarchy-chaos-")).resolve()
        self.home = self.root / "home"
        self.config_home = self.root / "config"
        self.state_home = self.root / "state"
        self.cache_home = self.root / "cache"
        self.runtime_dir = self.root / "runtime"
        for d in (
            self.home,
            self.config_home,
            self.state_home,
            self.cache_home,
            self.runtime_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        self.runtime_dir.chmod(0o700)

    # omarchy_ai.config appends "omarchy-ai" to each XDG base.
    @property
    def state_dir(self) -> Path:
        return self.state_home / "omarchy-ai"

    @property
    def config_dir(self) -> Path:
        return self.config_home / "omarchy-ai"

    def env(self, crashpoints: str = "", myapi_url: str | None = None) -> dict:
        env = {k: os.environ[k] for k in _KEEP_ENV if k in os.environ}
        env.update(
            {
                "HOME": str(self.home),
                "XDG_CONFIG_HOME": str(self.config_home),
                "XDG_STATE_HOME": str(self.state_home),
                "XDG_CACHE_HOME": str(self.cache_home),
                "XDG_RUNTIME_DIR": str(self.runtime_dir),
                "XDG_DATA_HOME": str(self.home / ".local" / "share"),
                "PYTHONPATH": os.pathsep.join((str(SRC_DIR), str(TESTS_DIR))),
                "PYTHONDONTWRITEBYTECODE": "1",
                "OMARCHY_CHAOS_ROOT": str(self.root),
                CRASHPOINTS_ENV: crashpoints,
            }
        )
        if myapi_url:
            env["OMARCHY_CHAOS_MYAPI_URL"] = myapi_url
        return env

    def run(
        self,
        target: str,
        *args,
        crashpoints: str = "",
        myapi_url: str | None = None,
        timeout: float = 30,
        **kwargs,
    ) -> ChildRun:
        """Run target(*args, **kwargs) in a fresh child; crashpoints is an OMARCHY_CRASHPOINTS spec."""
        payload = json.dumps({"args": list(args), "kwargs": kwargs})
        proc = subprocess.Popen(
            [sys.executable, str(CHILD), target, payload],
            cwd=self.root,
            env=self.env(crashpoints, myapi_url),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.kill()
            stdout, stderr = proc.communicate()
            timed_out = True
        result = None
        for line in stdout.splitlines():
            if line.startswith(RESULT_PREFIX):
                result = json.loads(line[len(RESULT_PREFIX) :])
        if proc.returncode == ISOLATION_EXIT_CODE:
            raise RuntimeError(
                f"chaos child refused to run outside its temp root:\n{stderr}"
            )
        return ChildRun(proc.returncode, stdout, stderr, result, timed_out)

    def crash_then_restart(
        self,
        target: str,
        crashpoint: str,
        *,
        mode: str = "exit",
        restart: str | None = None,
        args: tuple = (),
        restart_args: tuple = (),
        myapi_url: str | None = None,
        timeout: float = 30,
    ) -> tuple[ChildRun, ChildRun]:
        """Run target with one crashpoint armed, then run restart (default: target again) unarmed
        against the same state. Fails loudly if the first run did not die at that point."""
        crashed = self.run(
            target,
            *args,
            crashpoints=f"{crashpoint}={mode}",
            myapi_url=myapi_url,
            timeout=timeout,
        )
        if crashed.crashed_at != crashpoint:
            raise AssertionError(
                f"expected a crash at {crashpoint}, got exit {crashed.returncode}:\n"
                f"{crashed.stderr[-2000:]}"
            )
        after = self.run(
            restart or target,
            *(restart_args if restart else args),
            myapi_url=myapi_url,
            timeout=timeout,
        )
        return crashed, after

    def state(self, name: str, default: object = None) -> object:
        """A file under the child's STATE_DIR, parsed: .json as JSON, .jsonl as a list, else text."""
        path = self.state_dir / name
        try:
            text = path.read_text()
        except FileNotFoundError:
            return default
        if path.suffix == ".json":
            return json.loads(text)
        if path.suffix == ".jsonl":
            return [json.loads(line) for line in text.splitlines() if line.strip()]
        return text

    def cleanup(self) -> None:
        if self._own_root:
            shutil.rmtree(self.root, ignore_errors=True)

    def __enter__(self) -> "ChaosHarness":
        return self

    def __exit__(self, *exc) -> None:
        self.cleanup()
