"""Self-improving skills: procedures the assistant saved because they worked.

Storage follows the SKILL.md convention used by Claude Code, OpenClaw and
Hermes: CONFIG_DIR/skills/<name>/SKILL.md with name/description front
matter, so a skill can be read, edited or copied by hand.

The live model writes and revises skills (it is the only text generator
here). Jev picks them, following TypeSafe's skill-suggestion cookbook
(docs.typesafe.ai/cookbooks/skill_suggestion), which measured the agent
loading the wrong skill 16.8% -> 7.3% and loading one when nothing fits
9.8% -> 4.0%:

1. One call: a Choice over every skill (name -> description) ranks the
   roster, plus boolean "gate" questions on whether the request wants a
   procedure at all. Mean gate below 0.30 -> suggest nothing.
2. Second call on the top three only, now with each description plus the
   opening of its instructions: a Choice among them plus one boolean per
   candidate ("does it do the specific thing asked?"). All of those below
   0.30 -> suggest nothing.
"""
from __future__ import annotations

from datetime import datetime
import logging
import re
import shutil
import threading

from ..config import CONFIG_DIR
from .jev import Jev, JevError, boolean, choice

log = logging.getLogger("omarchy_ai.core.skills")

SKILLS_DIR = CONFIG_DIR / "skills"
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")
MAX_SKILLS = 200  # one Choice holds 255 options; leave room
SHORTLIST = 3
EXCERPT_CHARS = 700
GATE_THRESHOLD = 0.30
FITS_THRESHOLD = 0.30
_lock = threading.Lock()

# Adapted from the cookbook's gate questions to a desktop assistant: they
# ask whether an action/procedure is wanted, not what the topic is.
GATE_QUESTIONS = {
    "acts_on_user_system": "Is the assistant being asked to act on the user's computer, files, terminals, "
                           "apps, accounts or devices, rather than only to explain or chat?",
    "follows_procedure": "Would a careful expert doing this follow a specific known procedure or set of "
                         "steps, rather than answer from general understanding?",
    "prose_suffices": "Could a knowledgeable generalist fully satisfy this request by just talking, with no "
                      "tools and no access to the user's computer?",
}
INVERTED = {"prose_suffices"}


def slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", str(name).strip().lower()).strip("-")[:48].strip("-")
    if not NAME.fullmatch(value or ""):
        raise ValueError("Skill name needs letters or digits")
    return value


def _parse(text: str) -> dict:
    meta, body = {}, text
    if text.startswith("---\n") and "\n---" in text[4:]:
        head, _, body = text[4:].partition("\n---")
        body = body.lstrip("-").lstrip("\n")
        for line in head.splitlines():
            key, sep, value = line.partition(":")
            if sep:
                meta[key.strip()] = value.strip()
    return {**meta, "body": body.strip()}


def _path(name: str):
    return SKILLS_DIR / name / "SKILL.md"


def load(name: str) -> dict | None:
    try:
        skill = _parse(_path(slug(name)).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    skill["name"] = slug(name)
    return skill


def roster() -> list[dict]:
    if not SKILLS_DIR.is_dir():
        return []
    skills = []
    for path in sorted(SKILLS_DIR.glob("*/SKILL.md")):
        skill = load(path.parent.name)
        if skill and skill.get("description"):
            skills.append(skill)
    return skills[:MAX_SKILLS]


def save(name: str, description: str, instructions: str) -> tuple[dict, bool]:
    """Create or replace (returns (skill, created)). Replacing is how a
    skill improves: the live model rewrites it with what it learned."""
    name = slug(name)
    description = " ".join(str(description or "").split())
    instructions = str(instructions or "").strip()
    if not 10 <= len(description) <= 300:
        raise ValueError("description must be 10-300 characters: what the skill does and when to use it")
    if not 20 <= len(instructions) <= 8000:
        raise ValueError("instructions must be 20-8000 characters of concrete steps")
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    with _lock:
        existing = load(name)
        if existing is None and len(roster()) >= MAX_SKILLS:
            raise ValueError(f"Already {MAX_SKILLS} skills; delete or merge some first")
        revision = int(existing.get("revision", "1")) + 1 if existing and existing.get("revision", "1").isdigit() else 1
        created = existing.get("created", now) if existing else now
        text = (f"---\nname: {name}\ndescription: {description}\ncreated: {created}\n"
                f"updated: {now}\nrevision: {revision}\n---\n\n{instructions}\n")
        path = _path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(path)
    log.info("skills: %s %s (revision %d)", "created" if existing is None else "updated", name, revision)
    return load(name), existing is None


def delete(name: str) -> bool:
    try:
        target = SKILLS_DIR / slug(name)
    except ValueError:
        return False
    if not (target / "SKILL.md").is_file():
        return False
    shutil.rmtree(target)
    return True


def index_text(limit: int = 40, width: int = 110) -> str:
    """Compact roster for the session prompt (names + short descriptions)."""
    lines = []
    for skill in roster()[:limit]:
        description = skill["description"]
        lines.append(f"- {skill['name']}: " + (description if len(description) <= width else description[:width - 3] + "..."))
    return "\n".join(lines)


def suggest(request: str, jev: Jev | None = None) -> dict:
    """{"skill": name|None, "reason", "gate", "fits"}; never raises for Jev trouble."""
    skills = roster()
    if not skills:
        return {"skill": None, "reason": "No skills saved yet."}
    jev = jev or Jev()
    by_name = {s["name"]: s for s in skills}
    state = {"request": request}
    wide = {"which": choice("Which of these skills, if any, is the right one to load to help with the "
                            "user's latest request?", {s["name"]: s["description"] for s in skills})}
    for key, text in GATE_QUESTIONS.items():
        wide["gate::" + key] = boolean(text)
    try:
        first = jev.ask(state, wide)
    except JevError as exc:
        return {"skill": None, "reason": f"Jev unavailable: {exc}"}
    gates = [(1 - first["gate::" + k]["p"]) if k in INVERTED else first["gate::" + k]["p"] for k in GATE_QUESTIONS]
    gate = sum(gates) / len(gates)
    if gate < GATE_THRESHOLD:
        return {"skill": None, "reason": "This request does not call for a saved procedure.", "gate": round(gate, 2)}
    ranked = sorted(first["which"]["probabilities"].items(), key=lambda kv: -kv[1])
    shortlist = [name for name, _ in ranked[:SHORTLIST]]
    narrow = {"which": choice("Exactly one of these skills is the right one to load for the user's latest "
                              "request. Which one? Read what each actually does, not just its name.",
                              {n: f"{by_name[n]['description']} -- {by_name[n]['body'][:EXCERPT_CHARS]}"
                               for n in shortlist})}
    for n in shortlist:
        narrow["fits::" + n] = boolean(f"Does the skill '{n}' do the specific thing the user's request asks "
                                       f"for? It is described as: {by_name[n]['description']}")
    try:
        second = jev.ask(state, narrow)
    except JevError as exc:
        return {"skill": None, "reason": f"Jev unavailable: {exc}"}
    fits = {n: round(second["fits::" + n]["p"], 2) for n in shortlist}
    if max(fits.values()) < FITS_THRESHOLD:
        return {"skill": None, "reason": "No saved skill does this.", "gate": round(gate, 2), "fits": fits}
    winner = second["which"]["choice"]
    if fits[winner] < FITS_THRESHOLD:
        winner = max(fits, key=fits.get)
    return {"skill": winner, "reason": "Best matching saved skill.", "gate": round(gate, 2), "fits": fits}
