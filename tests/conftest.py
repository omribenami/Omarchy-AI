"""Tests never load the user's own voice extensions (~/.config/omarchy-ai/extensions)
nor touch their saved skills (~/.config/omarchy-ai/skills)."""
import pytest

from omarchy_ai.core import skills
from omarchy_ai.voice import extensions


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
