"""The assistant's own state: updates, agenda, HUDs, skills, issues, memory.

Split out of the former execution/actions.py; see execution/actions/__init__.py."""

from __future__ import annotations

import json
import logging
import time

from ...config import load_config
from .base import ActionResult

log = logging.getLogger("omarchy_ai.execution.actions")


def check_assistant_updates(args: dict) -> ActionResult:
    from ...core import updates
    result = updates.check_updates(force=True)
    return ActionResult(result["ok"], json.dumps(result))


def update_assistant(args: dict) -> ActionResult:
    from ...core import updates
    return ActionResult(*updates.request_update())


def get_update_status(args: dict) -> ActionResult:
    from ...core import updates
    return ActionResult(True, json.dumps(updates.update_status()))


def _resolve_scheduled(request: str) -> tuple[dict | None, str]:
    """({tool, args}, "") for a plain request, or (None, what to tell the
    model). Resolved now, while the user can still hear a read-back: the
    stored call is what runs, word for word, at the scheduled time."""
    from .. import catalog
    from ...myapi import client as myapi_client
    from ...voice.omarchy import GatewayClient
    config = load_config()

    def fill(tool, text, heard):
        return catalog.fill_args(tool, text, heard,
                                 lambda system, user: GatewayClient(config).complete_json(system, user, timeout=8))
    myapi_on = bool(getattr(config, "myapi_enabled", False)) and myapi_client.is_connected()
    pick = catalog.resolve({"request": request}, request, myapi_on=myapi_on, fill=fill)
    if pick.run and pick.tool not in catalog.CORE | {"schedule_task"}:
        return {"tool": pick.tool, "args": dict(pick.args)}, ""
    return None, pick.message


def schedule_task(args: dict) -> ActionResult:
    from ...core import agenda, schedule
    args = dict(args or {})
    kind = str(args.get("kind") or "").strip()
    args.pop("announce", None)  # only set here, from announce_on_speaker
    if args.pop("announce_on_speaker", None) is True:
        from ...core import conversations
        if conversations.source(conversations.CURRENT.get()) != "satellite":
            return ActionResult(False, "Task NOT scheduled: announce_on_speaker is only for a reminder asked "
                                       "through the speaker. Schedule it again without announce_on_speaker.")
        args["announce"] = "satellite"  # agenda.create checks it is a reminder due today
    has_calls = args.get("steps") not in (None, "", []) or bool(args.get("tool"))
    if kind == "action" and not has_calls:
        request = str(args.get("request") or args.get("title") or "").strip()
        step, why = _resolve_scheduled(request) if request else (None, "")
        if step is None:
            return ActionResult(False, "Task NOT scheduled: give the exact call as tool + args (or steps). "
                                       + (why or "No request to resolve."))
        args["steps"] = [step]
    elif kind == "assistant" and not has_calls:
        # 2026-09-30 17:33: "turn on <a device> at 18:30" became an
        # 'assistant' task, which at 18:30 only shows "start a conversation".
        # When one tool does the request, schedule that call instead.
        request = str(args.get("request") or args.get("title") or "").strip()
        try:
            step, _ = _resolve_scheduled(request) if request else (None, "")
        except Exception:  # noqa: BLE001 -- stays an assistant task
            log.warning("schedule_task: could not resolve %r", request[:120], exc_info=True)
            step = None
        if step is not None:
            args.update(kind="action", steps=[step])
    try:
        job = agenda.create(args)
    except ValueError as exc:
        return ActionResult(False, f"Task NOT scheduled: {exc}. Nothing changed; do not tell the user it was "
                                   f"scheduled. It is now {schedule.describe(time.time())}.")
    agenda.tick()  # a watch's first look happens now, not a heartbeat later
    summary = agenda.summary(job)
    note = ("Runs in the background even when no conversation is active; results arrive as desktop "
            "notifications and in the next conversation.")
    if job["kind"] == "action":
        note = ("At that time these exact tool calls run unattended, and the user is told the result (done or "
                "failed) by notification and by you. Read back what will happen and when (`when`).")
    out = {"scheduled": summary, "now": schedule.describe(time.time()), "note": note}
    if job.get("replaces"):
        out["replaced"] = job["replaces"] + " (cancelled; this task takes its place)"
    return ActionResult(True, json.dumps(out, ensure_ascii=False))


def watch_camera(args: dict) -> ActionResult:
    """A camera watch for the time frame the user gave (core/agenda.py judges
    each frame); a missing time frame comes back as a question for the user."""
    args = dict(args or {})
    camera = str(args.get("camera") or "").strip()
    return schedule_task({"kind": "watch", "camera": camera, "condition": args.get("condition"), "repeat": True,
                          "title": args.get("title") or f"Watching the {camera} camera",
                          "for_minutes": args.get("for_minutes"), "every_minutes": args.get("every_minutes")})


def list_scheduled_tasks(args: dict) -> ActionResult:
    from ...core import agenda, schedule
    return ActionResult(True, json.dumps({"now": schedule.describe(time.time()),
                                          "tasks": agenda.list_jobs(bool(args.get("include_finished")))},
                                         ensure_ascii=False))


def show_tasks_hud(args: dict) -> ActionResult:
    from ...display import assistant_huds
    assistant_huds.show_tasks()
    return ActionResult(True, f"Task HUD opened with {len(assistant_huds.task_items())} current tasks")


def hide_tasks_hud(args: dict) -> ActionResult:
    from ...display import assistant_huds
    assistant_huds.hide_tasks()
    return ActionResult(True, "Task HUD closed")


def show_routines_hud(args: dict) -> ActionResult:
    from ...display import assistant_huds
    assistant_huds.show_routines()
    return ActionResult(True, f"Routines HUD opened with {len(assistant_huds.routine_items())} active routines")


def hide_routines_hud(args: dict) -> ActionResult:
    from ...display import assistant_huds
    assistant_huds.hide_routines()
    return ActionResult(True, "Routines HUD closed")


def cancel_scheduled_task(args: dict) -> ActionResult:
    from ...core import agenda
    job = agenda.cancel(str(args.get("id") or ""))
    return ActionResult(bool(job), f"cancelled {job['id']}: {job['title']}" if job else "no task with that id")


def run_scheduled_task_now(args: dict) -> ActionResult:
    from ...core import agenda
    job = agenda._update(str(args.get("id") or ""), next_run=time.time())
    if not job or job.get("status") != "active":
        return ActionResult(False, "no active task with that id")
    agenda.tick()
    return ActionResult(True, f"started {job['id']} in the background; its result arrives as a notification "
                              "and in list_scheduled_tasks (last_result) — not verified yet")


def find_skill(args: dict) -> ActionResult:
    from ...core import skills
    request = str(args.get("request") or "").strip()
    if not request:
        return ActionResult(False, "request is required")
    picked = skills.suggest(request[:4000])
    if picked.get("skill"):
        skill = skills.load(picked["skill"])
        picked["instructions"] = skill["body"] if skill else ""
        picked["description"] = skill.get("description", "") if skill else ""
        picked["note"] = _SKILL_READ.format(name=picked["skill"])
    return ActionResult(True, json.dumps(picked, ensure_ascii=False))


def load_skill(args: dict) -> ActionResult:
    from ...core import skills
    skill = skills.load(str(args.get("name") or ""))
    if not skill:
        return ActionResult(False, "no skill with that name")
    return ActionResult(True, json.dumps({**skill, "note": _SKILL_READ.format(name=skill["name"])},
                                         ensure_ascii=False))


# 2026-10-12: asked to start a saved procedure of five timed steps, she read
# it with load_skill, said "on it" and made no other call; nothing ran, and
# two minutes later she said she was making sure it was running.
_SKILL_READ = ("This only read the skill: nothing has been run. To carry it out call run_skill(name='{name}', "
               "request=<the user's request>); until that returns, do not tell the user it has started.")


def run_skill(args: dict) -> ActionResult:
    """Carry a saved skill out as a Task Runtime job, so that doing it never
    depends on the live model following text it was handed."""
    from ...core import skills
    name, request = str(args.get("name") or "").strip(), " ".join(str(args.get("request") or "").split())
    if name:
        skill = skills.load(name)
        if not skill:
            return ActionResult(False, "no skill with that name; nothing was run")
    elif request:
        picked = skills.suggest(request[:4000])
        skill = skills.load(picked["skill"]) if picked.get("skill") else None
        if not skill:
            return ActionResult(False, f"No saved skill fits this request ({picked.get('reason')}), so nothing was "
                                       "run. Do it with your other tools or start_task.")
    else:
        return ActionResult(False, "run_skill needs the skill's name, or the user's request to pick one by")
    from ...runtime.service import start_task
    return start_task({"goal": skills.run_goal(skill, request)})


def save_skill(args: dict) -> ActionResult:
    from ...core import skills
    name, description, instructions = args.get("name", ""), args.get("description", ""), args.get("instructions", "")
    try:
        # A new name is not always a new skill: Jev says whether a saved one
        # already does this job, and then that one is rewritten instead.
        asked, existing = skills.slug(name), skills.load(name)
        if existing is None and (same := skills.existing_for(asked, description, instructions)):
            existing = skills.load(same)
        if existing is not None:
            instructions = skills.merged(existing, instructions)
        skill, created = skills.save(existing["name"] if existing else asked, description, instructions)
    except ValueError as exc:
        return ActionResult(False, f"Skill NOT saved: {exc}")
    note = (f" It already did this job, so it was updated instead of creating '{asked}'; tell the user that."
            if skill["name"] != asked else "")
    return ActionResult(True, f"{'created' if created else 'updated'} skill {skill['name']} "
                              f"(revision {skill.get('revision')}).{note}")


def list_skills(args: dict) -> ActionResult:
    from ...core import skills
    return ActionResult(True, json.dumps([{k: s.get(k) for k in ("name", "description", "updated", "revision")}
                                          for s in skills.roster()], ensure_ascii=False))


def delete_skill(args: dict) -> ActionResult:
    from ...core import skills
    ok = skills.delete(str(args.get("name") or ""))
    return ActionResult(ok, "deleted" if ok else "no skill with that name")


def get_release_notes(args: dict) -> ActionResult:
    from ...core import updates
    try:
        result = updates.release_notes(str(args.get("version") or "").strip() or None)
    except ValueError as exc:
        return ActionResult(False, str(exc))
    return ActionResult(result["ok"], json.dumps(result, ensure_ascii=False))


def report_issue(args: dict) -> ActionResult:
    from ...core import issues
    title = str(args.get("title") or "").strip()
    description = str(args.get("description") or "").strip()
    if not title or not description:
        return ActionResult(False, "title and description are both required")
    ok, result = issues.file_issue(title, description, args.get("repo"), use_gh=True)
    if not ok:
        return ActionResult(False, f"Issue NOT filed: {result}")
    return ActionResult(True, f"Filed: {result}")


def remember_preference(args: dict) -> ActionResult:
    text = (args.get("preference") or "").strip()
    if not text:
        return ActionResult(False, "no preference given")
    from ...core.memory import add_preference

    add_preference(text)
    return ActionResult(True, "remembered")


def run_mission(args: dict) -> ActionResult:
    # Executed inside the live session (it narrates through it); see
    # GeminiLiveSession._run_mission. Other providers cannot run it.
    return ActionResult(False, "run_mission needs a live Gemini session")
