"""Tests never load the user's own voice extensions (~/.config/omarchy-ai/extensions)
nor touch their saved skills (~/.config/omarchy-ai/skills), state
(~/.local/state/omarchy-ai) or config (~/.config/omarchy-ai).

Module-level paths (config.STATE_DIR, agenda.JOBS_PATH, history.HISTORY_PATH, ...)
are computed from the XDG variables at import time, so those variables are
pointed at a throwaway directory here, before anything imports omarchy_ai.
XDG_RUNTIME_DIR is redirected too, so no test can reach the live daemon's socket.

Under CI (the CI environment variable is set), tests marked ``live`` (real
network services) or ``hardware`` (audio, Wayland, GPU, phone, TV) are skipped.
"""
import atexit
import os
import shutil
import tempfile
from pathlib import Path

_ISOLATED = Path(tempfile.mkdtemp(prefix="omarchy-ai-tests-"))
atexit.register(shutil.rmtree, _ISOLATED, ignore_errors=True)
for _var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_RUNTIME_DIR"):
    (_ISOLATED / _var).mkdir(mode=0o700)
    os.environ[_var] = str(_ISOLATED / _var)

import pytest  # noqa: E402

from omarchy_ai import config  # noqa: E402
from omarchy_ai.core import skills  # noqa: E402
from omarchy_ai.voice import extensions  # noqa: E402

_REAL_STATE = Path("~/.local/state/omarchy-ai").expanduser()
_REAL_CONFIG = Path("~/.config/omarchy-ai").expanduser()
assert config.STATE_DIR != _REAL_STATE and config.CONFIG_DIR != _REAL_CONFIG, (
    "omarchy_ai was imported before tests/conftest.py isolated the XDG directories"
)

_CI_SKIPPED_MARKERS = ("live", "hardware")


def pytest_collection_modifyitems(config, items):
    if not os.environ.get("CI"):
        return
    for item in items:
        for name in _CI_SKIPPED_MARKERS:
            if item.get_closest_marker(name):
                item.add_marker(pytest.mark.skip(reason=f"{name} test: skipped in CI"))
                break


@pytest.fixture(autouse=True)
def isolated_dirs(monkeypatch, tmp_path):
    """A fresh config and state directory per test, for code that reads them at call time."""
    state = tmp_path / "state" / "omarchy-ai"
    conf = tmp_path / "config" / "omarchy-ai"
    monkeypatch.setattr(config, "STATE_DIR", state)
    monkeypatch.setattr(config, "CONFIG_DIR", conf)
    monkeypatch.setattr(config, "USER_CONFIG_PATH", conf / "config.yaml")
    monkeypatch.setenv("XDG_STATE_HOME", str(state.parent))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(conf.parent))
    return state, conf


@pytest.fixture(autouse=True)
def no_user_extensions(monkeypatch):
    monkeypatch.setattr(extensions, "_modules", [])


@pytest.fixture(autouse=True)
def no_user_skills(monkeypatch, tmp_path):
    monkeypatch.setattr(skills, "SKILLS_DIR", tmp_path / "skills")


@pytest.fixture(autouse=True)
def no_jev_for_question_cards(monkeypatch):
    """A question card asks Jev whether it is a yes/no question (core/conversations.py)."""
    from omarchy_ai.core import conversations
    monkeypatch.setattr(conversations, "_open_question", lambda question: False)
