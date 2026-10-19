"""Tests never load the user's own voice extensions (~/.config/omarchy-ai/extensions)
nor touch their saved skills (~/.config/omarchy-ai/skills), state
(~/.local/state/omarchy-ai) or config (~/.config/omarchy-ai).

Module-level paths (config.STATE_DIR, agenda.JOBS_PATH, history.HISTORY_PATH, ...)
are computed from the XDG variables at import time, so tests/_isolated_dirs points
those variables at a throwaway directory before anything imports omarchy_ai (the
same package isolates ``python -m unittest discover -s tests``).
XDG_RUNTIME_DIR is redirected too, so no test can reach the live daemon's socket,
and tests/no_live_desktop.py stops any test from running a real desktop command.

Under CI (the CI environment variable is set), tests marked ``live`` (real
network services) or ``hardware`` (audio, Wayland, GPU, phone, TV) are skipped.
"""
import os

import _isolated_dirs  # noqa: F401  (first: XDG directories point at a throwaway folder)
import pytest

import no_live_desktop  # noqa: F401  (no test runs a real desktop or device command)
from omarchy_ai import config
from omarchy_ai.core import skills
from omarchy_ai.voice import extensions

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


@pytest.fixture(autouse=True)
def no_background_summaries(monkeypatch):
    """A long test conversation would ask the Gateway for its summary (core/summarize.py);
    tests call summarize.refresh() themselves."""
    from omarchy_ai.core import summarize
    monkeypatch.setattr(summarize, "AUTO", False)
