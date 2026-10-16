"""Self-improving skills: procedures the assistant saved because they worked.

Storage follows the SKILL.md convention used by Claude Code, OpenClaw and
Hermes: CONFIG_DIR/skills/<name>/SKILL.md with name/description front
matter, so a skill can be read, edited or copied by hand.

Two writers. The live model saves and revises a skill when asked or
corrected. The Task Runtime saves or updates one, unasked, when a multi-step
job ends certified (learn_from_task), from the task's own record: the
commands that ran, word for word, never a retelling. A coding agent runs its
commands inside its own CLI, where the harness records none of them (the
four certified tasks in the store on 2026-10-12 held only "[executor]" lines
and verification runs), so it is asked to end its report with them
(COMMANDS_MARKER) and the skill says that they are its own report.
2026-10-12: a skill
saved only because the user asked held a paraphrase of a job the Task
Runtime had done, not the commands that had worked. There Jev decides three
things: whether the job is a procedure worth keeping, which commands
produced the result, and whether it is a skill already saved.

Reading a skill runs nothing. Carrying one out is a Task Runtime job whose
goal run_goal builds (execution/actions.py run_skill); same day, asked to
start a saved five-step procedure, the live model read it, said "on it" and
made no further call.

Jev picks skills, following TypeSafe's skill-suggestion cookbook
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
RUN_P = 0.8               # the request asks for the skill to be carried out now
RUN_PREFIX = "Carry out the saved skill "   # first line of a goal that runs a skill (run_goal)
_RUN_REQUEST = "The user's request:\n"
GOAL_CHARS = 3900         # start_task refuses a goal over 4000
LEARN_MIN_COMMANDS = 2    # fewer successful commands is not a procedure
LEARN_MAX_COMMANDS = 20   # the latest ones Jev is asked about
LEARN_P = 0.6             # worth saving; the same procedure as a saved skill
KEEP_P = 0.5              # a command produced the result
COMMANDS_HEADING = "Commands that worked (task "   # the harness ran and recorded them
REPORTED_HEADING = "Commands the worker reported (not observed by the harness; task "
_SECTION = re.compile("|".join(re.escape(h) for h in (COMMANDS_HEADING, REPORTED_HEADING)))
# Last section of a coding agent's report (runtime/executors/coding_agents.py).
COMMANDS_MARKER = "COMMANDS THAT DID IT:"
# First line under a learned skill's commands: they are one run, not the procedure's fixed values.
VALUES_NOTE = ("The values in these commands (durations, counts, times, names, messages, targets) came from that "
               "request; a new request's values replace them, and repeated steps scale to it.")
REPORTED_MAX, REPORTED_CHARS = 30, 600
_NEW = "new"              # the "none of the saved ones" option; a skill of that very name is not offered
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


def save(name: str, description: str, instructions: str, conversation: str | None = None) -> tuple[dict, bool]:
    """Create or replace (returns (skill, created)). Replacing is how a
    skill improves: the live model rewrites it with what it learned. The
    talk it happened in gets a note (`conversation`; None: the current one)."""
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
    try:
        from . import conversations
        conversations.add_note(conversations.CURRENT.get() if conversation is None else conversation,
                               f"Skill created: {name}" if existing is None else
                               f"Skill updated: {name} (revision {revision})")
    except Exception:  # noqa: BLE001 -- the skill is saved either way
        log.debug("could not note the saved skill in its conversation", exc_info=True)
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
        # The cookbook's wording, "does it do the specific thing asked?", read
        # a changed number as a different thing: on 2026-10-12 a request for a
        # 6-minute countdown scored 0.21-0.28 against the saved 5-minute one,
        # under the bar, so the skill was never offered. This wording scored
        # those requests 0.84-0.95, and one for a single part of it 0.57.
        narrow["fits::" + n] = boolean(
            f"Is the user's request asking for the job the skill '{n}' does, even if a number, a duration or the "
            "wording differs? A request for only one part of it, or for something else, is not. The skill is "
            f"described as: {by_name[n]['description']}")
    # What each one does, not only how it was described when it was saved.
    state = {**state, "skills": {n: {"description": by_name[n]["description"],
                                     "steps": by_name[n]["body"][:EXCERPT_CHARS]} for n in shortlist}}
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


def run_goal(skill: dict, request: str) -> str:
    """The start_task goal that carries `skill` out for `request`. Its first
    line names the skill, so the task is recognized later (skill_of_goal);
    the body is clipped so the goal always fits."""
    request = " ".join(str(request or "").split())[:1000] or f"run {skill['name']}"
    head = (f"{RUN_PREFIX}'{skill['name']}'.\n\n{_RUN_REQUEST}{request}\n\n"
            "The steps below are a procedure that worked before. Its concrete values (durations, counts, times, "
            "names, messages, targets, devices) are those of the run it was learned from: take every value from "
            "the request above, and use a saved one only where the request gives none. Scale repeated steps to "
            "the request (an announcement every minute over N minutes is N announcements, whatever N the saved "
            "run used), and leave out the parts the request does not want. A wait between steps is a real wait: "
            "let that time pass before the next step.\n\n")
    body, room = str(skill.get("body") or ""), GOAL_CHARS - len(head)
    return head + (body if len(body) <= room else body[:room - 1] + "…")


_RUN_GOAL = re.compile(re.escape(RUN_PREFIX) + "'(" + NAME.pattern + r")'\.\n")


def skill_of_goal(goal: str) -> str | None:
    """The skill a goal built by run_goal carries out."""
    match = _RUN_GOAL.match(str(goal or ""))
    return match.group(1) if match else None


def request_of_goal(goal: str) -> str | None:
    """The user's own request inside a run_goal goal: what a task is titled."""
    goal = str(goal or "")
    if not skill_of_goal(goal) or _RUN_REQUEST not in goal:
        return None
    return goal.split(_RUN_REQUEST, 1)[1].split("\n\n", 1)[0].strip() or None


def wants_run(request: str, skill: dict, jev: Jev | None = None, asked: str = "") -> bool:
    """The user asked for this skill to be carried out, not read (voice path:
    run it off the event loop). `asked` is how the live model worded its own
    lookup, when that is not the request itself. False when Jev is
    unavailable: then the model gets the skill's text and the note that
    nothing ran."""
    state = {"user_latest_request": str(request or "")[-600:],
             **({"assistant_lookup": asked[-300:]} if asked else {}),
             "saved_procedure": {"name": skill.get("name", ""), "description": skill.get("description", ""),
                                 "steps": str(skill.get("body") or "")[:EXCERPT_CHARS]}}
    question = {"run_now": boolean(
        "Does the user's latest request ask for this saved procedure to be carried out now, as opposed to reading "
        "it, describing it, editing it, or checking what it says? Judge the meaning, in any language.")}
    try:
        p = (jev or Jev()).ask(state, question, timeout=3, retries=0, fail_fast=True)["run_now"]["p"]
    except JevError as exc:
        log.info("skills: run check unavailable for %s: %s", skill.get("name"), str(exc)[:120])
        return False
    log.info("skills: run %s now p=%.2f for %r", skill.get("name"), p, str(request or "")[:120])
    return p >= RUN_P


# What redaction (execution/passwords.py) cannot know: a credential that was
# never saved or spoken. A command that still shows one is left out of the
# skill; a skill is a plain file that is read back into prompts.
_SECRET = re.compile(
    r"(?i)(?:pass(?:word|wd|phrase)?|secret|token|api[_-]?key|access[_-]?key|credential)s?[\"']?\s*[=:]\s*"
    r"[\"']?[^\s\"'$<{\[]{4,}"
    r"|--?(?:password|passwd|token|secret|api-key)[= ]\s*[^\s$<{\[-]\S{3,}"
    r"|\bbearer\s+[\w.~+/-]{8,}|\bauthorization\s*:\s*(?:\w+\s+)?[^\s\"'$<{\[]{8,}"
    r"|://[^/\s:@]+:[^/\s:@$]{3,}@"
    r"|\b(?:gh[pousr]_|sk-|xox[abp]-|AKIA)[A-Za-z0-9_-]{10,}"
    r"|-----BEGIN [A-Z ]*PRIVATE KEY")


def _same_question(text: str, others: dict) -> dict:
    return choice(text, {**others, _NEW: "None of these: a procedure that is not saved yet"})


def existing_for(name: str, description: str, instructions: str, jev: Jev | None = None) -> str | None:
    """The saved skill that already does the job a new skill is about to be
    saved for, so that one is brought up to date and no second copy appears
    under another name (the user's request, 2026-10-12). Jev decides; None
    when nothing saved does it, or Jev is unavailable: then it is saved as
    asked, since a duplicate can be merged and a lost skill cannot."""
    others = {s["name"]: s["description"] for s in roster() if s["name"] != _NEW}
    if not others:
        return None
    state = {"new_skill": {"name": str(name)[:60], "description": str(description)[:300],
                           "steps": str(instructions)[:EXCERPT_CHARS]}}
    question = {"same": _same_question(
        "Is `new_skill` the same procedure as one of these saved skills, i.e. does it do the same job for the same "
        "thing, even if it is named or worded differently or has more detail? A procedure for a different job, "
        "device or target is not the same.", others)}
    try:
        answer = (jev or Jev()).ask(state, question, timeout=3, retries=0, fail_fast=True)["same"]
    except JevError as exc:
        log.info("skills: duplicate check unavailable for %r: %s", str(name)[:60], str(exc)[:120])
        return None
    log.info("skills: %r is %s (p=%.2f)", str(name)[:60], answer["choice"], answer["p"])
    return answer["choice"] if answer["choice"] != _NEW and answer["p"] >= LEARN_P else None


def merged(existing: dict, instructions: str) -> str:
    """New text for a skill being rewritten, keeping the commands a task
    recorded for it when the new text brings none: they are what worked."""
    instructions = str(instructions or "").strip()
    found = _SECTION.search(existing.get("body") or "")
    if not found or _SECTION.search(instructions):
        return instructions
    section = existing["body"][found.start():].strip()
    return instructions[:8000 - len(section) - 2].rstrip() + "\n\n" + section


def _worked(task) -> list[str]:
    """The task's successful work commands, in order: allowed, exit 0, and
    neither a verification run nor a worker CLI's own invocation."""
    roles = {s.get("n"): s.get("role") for s in task.steps}
    return [str(c.get("command") or "") for c in task.commands
            if c.get("decision") == "allow" and c.get("exit_code") == 0 and not c.get("timed_out")
            and c.get("role") != "test" and roles.get(c.get("step")) not in ("test", "review")
            and c.get("command") and not str(c["command"]).startswith("[executor]")]


_MARKER_LINE = re.compile(r"^[ \t>*#]*" + re.escape(COMMANDS_MARKER.rstrip(":")) + r":?[ \t*]*$", re.I | re.M)
_REPORT_LINE = re.compile(r"[A-Z][A-Z ]{3,}:")  # SUMMARY:, RISKS:, VERDICT: -- the report went on


def reported_commands(text: str) -> list[str]:
    """The commands a coding agent listed under COMMANDS_MARKER at the end of
    its report, read from its whole output (a step's claim is clipped)."""
    found = list(_MARKER_LINE.finditer(str(text or "")))
    if not found:
        return []
    commands = []
    for line in text[found[-1].end():].splitlines():
        line = line.strip()
        if line.startswith("```") or (not line and not commands):
            if line and commands:
                break  # the closing fence
            continue
        if not line or _REPORT_LINE.match(line):
            break
        line = re.sub(r"^(?:\$ |[-*] |\d+[.)] )", "", line).strip("`").strip()
        if line.lower().strip("<>().") in ("", "none", "nothing", "empty", "n/a") or line[:1] + line[-1:] == "<>":
            continue  # nothing was needed, or the prompt's own placeholder came back
        commands.append(line[:REPORTED_CHARS])
    return commands[:REPORTED_MAX]


def _reported(task) -> list[str]:
    """What the latest finished work step's own report listed (step meta)."""
    for step in reversed(task.steps):
        commands = (step.get("meta") or {}).get("commands")
        if step.get("outcome") == "done" and step.get("role") not in ("test", "review") and commands:
            return [str(c) for c in commands if c]
    return []


def learn_from_task(task, jev: Jev | None = None) -> dict | None:
    """Save or update a skill from a certified task's record:
    {"skill", "created"}, or None when nothing was saved. Code counts the
    commands and builds every word of the text from the record; Jev decides
    whether it is worth keeping, which commands made the result, and whether
    it is a skill already saved. Jev unavailable: nothing is saved."""
    from ..execution.passwords import redact
    worked, heading = _worked(task), COMMANDS_HEADING
    if len(worked) < LEARN_MIN_COMMANDS:
        # A coding agent did the job: the harness saw none of its commands.
        worked, heading = _reported(task), REPORTED_HEADING
    if len(worked) < LEARN_MIN_COMMANDS:
        return None
    shown = [c for c in (redact(c, speech=True) for c in worked[-LEARN_MAX_COMMANDS:]) if not _SECRET.search(c)]
    if not shown:
        return None
    commands = {f"c{n}": c for n, c in enumerate(shown, 1)}
    request = " ".join(redact(task.title, speech=True).split())
    objective = " ".join(redact(str(task.objective or ""), speech=True).split())
    if not objective or task.goal.startswith(str(task.objective)):
        objective = request  # no plan was made: the objective is the goal, handoff text and all
    claim = next((s["claim"] for s in reversed(task.steps) if s.get("outcome") == "done" and s.get("claim")
                  and s.get("role") not in ("test", "review")), "")  # the work's own account, not the checks'
    claim = " ".join(redact(claim, speech=True).split())
    claim = "" if _SECRET.search(claim) else claim if len(claim) <= 600 else claim[:599] + "…"
    own = load(skill_of_goal(task.goal) or "")
    others = {s["name"]: s["description"] for s in roster() if s["name"] != _NEW} if own is None else {}
    questions = {"worth": boolean(
        "Is this a reusable procedure the user is likely to ask for again (an operation on their machine, home or "
        "accounts), rather than a one-off question, an investigation, or a source-code change in a project?")}
    for key in commands:
        questions["kept::" + key] = boolean(
            f"Was command {key} part of producing the result, as opposed to exploring, looking something up that "
            "the procedure does not need next time, or checking afterwards?")
    if others:
        questions["same"] = _same_question("Did this task carry out the same procedure as one of these saved "
                                           "skills?", others)
    state = {"request": request[:600], "objective": objective[:400], "result": claim[:400],
             "commands": {key: c[:300] for key, c in commands.items()}}
    try:
        answers = (jev or Jev()).ask(state, questions, timeout=8, retries=1)
    except JevError as exc:
        log.info("skills: nothing learned from task %s, Jev unavailable: %s", task.id, str(exc)[:120])
        return None
    if answers["worth"]["p"] < LEARN_P:
        log.info("skills: task %s not kept as a skill (worth p=%.2f)", task.id, answers["worth"]["p"])
        return None
    kept = [c for key, c in commands.items() if answers["kept::" + key]["p"] >= KEEP_P] or list(commands.values())
    existing = own
    if existing is None and others and answers["same"]["choice"] != _NEW and answers["same"]["p"] >= LEARN_P:
        existing = load(answers["same"]["choice"])
    section = (f"{heading}{task.id}, {datetime.now():%Y-%m-%d}):\n{VALUES_NOTE}\n" + "\n".join(kept)
               + (f"\n\nResult: {claim}" if claim else ""))[:6000]
    if existing is not None:
        # Hand-written steps stay; only the commands a task recorded are replaced.
        name, description = existing["name"], existing.get("description", "")
        head = _SECTION.split(existing["body"], 1)[0].rstrip()[:8000 - len(section) - 2]
        instructions = (head + "\n\n" if head else "") + section
    else:
        try:
            name = slug(" ".join(request.split()[:6]))
        except ValueError:  # a request with no Latin letters or digits
            name = slug(f"task-{task.id}")
        if load(name) is not None:  # Jev said it is none of the saved ones: never replace that one
            name = slug(f"{name[:24]}-{task.id}")
        description = objective
        instructions = f"Request: {request[:1000]}\n\n{section}"
    if len(description) < 10:
        description = f"Saved procedure: {description or request or name}"
    if len(description) > 300:
        description = description[:299] + "…"
    try:
        skill, created = save(name, description, instructions,
                              conversation=str(getattr(task, "conversation", "") or ""))
    except ValueError as exc:
        log.warning("skills: task %s not saved as a skill: %s", task.id, exc)
        return None
    return {"skill": skill, "created": created}
