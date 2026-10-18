import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.core import outbox
from omarchy_ai.core import agenda, skills
from omarchy_ai.core.jev import Jev
from omarchy_ai.execution.actions import ActionResult


class FakeGateway:
    """Answers in the real Gateway shape; `answer(name, question, state)`
    returns a probability (boolean) or an option key (choice)."""

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, state, questions, timeout=8):
        self.calls.append((json.loads(state), questions))
        answers = {}
        for name, q in questions.items():
            value = self.answer(name, q, json.loads(state))
            if q["type"] == "boolean":
                answers[name] = {"type": "boolean", "probability": value}
            else:
                answers[name] = {"type": "choice", "choice": value,
                                 "probabilities": {k: (1.0 if k == value else 0.0) for k in q["criteria"]}}
        return {"answers": answers}


class AgendaTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        for name, value in (("JOBS_PATH", base / "agenda.json"), ("INBOX_PATH", base / "inbox.jsonl")):
            p = patch.object(agenda, name, value)
            p.start()
            self.addCleanup(p.stop)
        self.notify = patch.object(outbox, "_desktop").start()  # the desktop notice (presence unknown in tests)
        self.hud_refresh = patch.object(agenda, "_refresh_routines_hud").start()
        patch.object(agenda, "CONFIRM_DELAY_SECONDS", 0).start()
        self.address_for = patch("omarchy_ai.execution.tile_logs.address_for", return_value="0xc1a").start()
        self.addCleanup(patch.stopall)
        agenda._briefed.clear()
        self.now = 1_790_000_000.0

    def watch(self, **extra):
        return agenda.create({"title": "Claude needs me", "kind": "watch", "window": "claude",
                              "condition": "Claude Code is waiting for the user's input", **extra}, now=self.now)

    def run_watch(self, job_id, gateway, now=None, output="building...\nDo you want to proceed? (y/n)"):
        job = next(j for j in agenda._load() if j["id"] == job_id)
        with patch("omarchy_ai.execution.tile_logs.read_log", return_value="Source: x.log\n" + output):
            agenda.run_job(job, Jev(gateway), now or self.now)
        return next(j for j in agenda._load() if j["id"] == job_id)

    def test_validation_rejects_ambiguous_or_incomplete_tasks(self):
        for bad in ({"title": "x", "kind": "watch", "condition": "c"},                      # no source
                    {"title": "x", "kind": "watch", "window": "a", "path": "b", "condition": "c"},
                    {"title": "x", "kind": "remind"},                                         # no time
                    {"title": "x", "kind": "remind", "in_minutes": 5, "cron": "* * * * *"},
                    {"title": "x", "kind": "command", "in_minutes": 5},
                    {"title": "x", "kind": "bogus", "in_minutes": 5},
                    {"title": "x", "kind": "remind", "at": "2020-01-01T00:00"}):
            with self.assertRaises(ValueError, msg=bad):
                agenda.create(bad, now=self.now)

    def test_watch_pins_the_window_address_and_fixes_a_window_passed_as_terminal(self):
        # 2026-09-24: the scp watch named the user's window as `terminal`, and
        # ssh renamed that window's title mid-session.
        self.assertEqual(self.watch()["window"], "0xc1a")
        with patch("omarchy_ai.execution.workbench.exists", return_value=False):
            job = agenda.create({"title": "scp done -> start the Minecraft container", "kind": "watch",
                                 "terminal": "user@desktop:~", "condition": "the transfer finished"}, now=self.now)
        self.assertEqual((job.get("window"), job.get("terminal")), ("0xc1a", None))
        self.address_for.return_value = None
        with self.assertRaises(ValueError):
            self.watch()

    def test_watch_fires_once_with_real_evidence_line_and_finishes(self):
        job = self.watch()
        self.assertEqual(job["next_run"], self.now)  # first look is immediate
        lines = ["building...", "Do you want to proceed? (y/n)"]
        gateway = FakeGateway(lambda n, q, s: 0.97 if n == "met" else
                              "urgent" if n == "urgency" else str(lines.index("Do you want to proceed? (y/n)")))
        done = self.run_watch(job["id"], gateway)
        self.assertEqual(done["status"], "done")
        self.notify.assert_called_once_with("Claude needs me", "Do you want to proceed? (y/n)", True)
        state = gateway.calls[0][0]
        self.assertNotIn("Source:", state["observation"]["output_tail"])
        self.assertEqual(state["condition"], "Claude Code is waiting for the user's input")
        self.assertEqual(agenda.briefing()[0]["outcome"], "condition_met")

    def test_uncertain_watch_keeps_waiting_and_unchanged_output_skips_jev(self):
        job = self.watch()
        gateway = FakeGateway(lambda n, q, s: 0.6 if n == "met" else "normal" if n == "urgency" else "none")
        after = self.run_watch(job["id"], gateway)
        self.assertEqual(after["status"], "active")
        self.assertEqual(after["next_run"], self.now + 120)
        self.notify.assert_not_called()
        self.run_watch(job["id"], gateway, now=self.now + 120)
        self.assertEqual(len(gateway.calls), 1, "identical output must not cost another Jev call")

    def test_repeating_watch_alerts_on_each_rising_edge_only(self):
        job = self.watch(repeat=True)
        state = {"p": 0.95}
        gateway = FakeGateway(lambda n, q, s: state["p"] if n == "met" else "normal" if n == "urgency" else "none")
        self.run_watch(job["id"], gateway, output="a")
        self.run_watch(job["id"], gateway, now=self.now + 120, output="b")      # still true: no second alert
        state["p"] = 0.1
        self.run_watch(job["id"], gateway, now=self.now + 240, output="c")
        state["p"] = 0.95
        after = self.run_watch(job["id"], gateway, now=self.now + 360, output="d")
        self.assertEqual(self.notify.call_count, 2)
        self.assertEqual(after["status"], "active")

    def test_jev_outage_never_fires_and_retries(self):
        job = self.watch()
        def down(state, questions, timeout=8):
            raise OSError("offline")
        after = self.run_watch(job["id"], down)
        self.assertEqual(after["status"], "active")
        self.assertNotIn("last_digest", after)
        self.notify.assert_not_called()

    def camera_watch(self, motion=False, **extra):
        with patch("omarchy_ai.execution.cameras.names", return_value=["Front Door", "garden"]), \
                patch("omarchy_ai.execution.cameras.can_wait", return_value=motion):
            return agenda.create({"title": "Parcel", "kind": "watch", "camera": "front door", "for_minutes": 1440,
                                  "condition": "a parcel is on the doorstep", **extra}, now=self.now)

    def look(self, job_id, p, now=None, snapshot=None):
        job = next(j for j in agenda._load() if j["id"] == job_id)
        answers = {"met": {"p": p}, "urgency": {"choice": "normal", "p": 1.0}}
        with patch("omarchy_ai.execution.cameras.snapshot", snapshot or (lambda name, config, key: Path("/frames") / key)), \
                patch("omarchy_ai.execution.vision.judge_image", return_value=answers) as judge:
            agenda.run_job(job, None, now or self.now)
        return next(j for j in agenda._load() if j["id"] == job_id), judge

    def test_camera_watch_takes_only_a_real_camera_and_keeps_its_exact_name(self):
        self.assertEqual(self.camera_watch()["camera"], "Front Door")
        with patch("omarchy_ai.execution.cameras.names", return_value=["Front Door", "garden"]), \
                patch("omarchy_ai.execution.cameras.can_wait", return_value=False):
            with self.assertRaisesRegex(ValueError, "the cameras are: Front Door, garden"):
                agenda.create({"title": "x", "kind": "watch", "camera": "roof", "condition": "c"}, now=self.now)
            with self.assertRaises(ValueError):  # two sources
                agenda.create({"title": "x", "kind": "watch", "camera": "garden", "screen": True, "condition": "c"},
                              now=self.now)
        self.assertEqual(agenda.summary(self.camera_watch())["camera"], "Front Door")

    def test_camera_watch_judges_every_frame_and_fires_on_the_picture(self):
        job = self.camera_watch()
        waiting, judge = self.look(job["id"], 0.2)
        self.assertEqual(waiting["status"], "active")
        self.assertIn("a parcel is on the doorstep", judge.call_args.args[1]["met"]["instructions"])
        _, again = self.look(job["id"], 0.2, now=self.now + 120)
        again.assert_called_once()  # no "unchanged" shortcut for pictures
        done, _ = self.look(job["id"], 0.97, now=self.now + 240)
        self.assertEqual(done["status"], "done")
        self.notify.assert_called_once_with("Parcel", "a parcel is on the doorstep", False)
        self.assertIn("/frames/" + job["id"], agenda._inbox_read()[-1]["detail"])

    def test_a_picture_alerts_only_when_a_second_frame_agrees(self):
        job = self.camera_watch()
        first = {"met": {"p": 0.97}, "urgency": {"choice": "normal", "p": 1.0}}
        later = {"met": {"p": 0.3}, "urgency": {"choice": "normal", "p": 1.0}}
        with patch("omarchy_ai.execution.cameras.snapshot", lambda name, config, key: Path("/frames") / key), \
                patch("omarchy_ai.execution.vision.judge_image", side_effect=[first, later]) as judge:
            agenda.run_job(next(j for j in agenda._load() if j["id"] == job["id"]), None, self.now)
        after = next(j for j in agenda._load() if j["id"] == job["id"])
        self.assertEqual(judge.call_count, 2)
        self.assertEqual((after["status"], after["last_met"]), ("active", False))
        self.notify.assert_not_called()
        _, judge = self.look(job["id"], 0.97, now=self.now + 120)
        self.assertEqual(judge.call_count, 2)  # the first frame and the one confirming it
        self.notify.assert_called_once()

    def test_camera_that_gives_no_frame_keeps_the_watch_waiting(self):
        job = self.camera_watch()
        def offline(name, config, key):
            raise LookupError("camera 'Front Door' returned no image")
        after, judge = self.look(job["id"], 0.99, snapshot=offline)
        self.assertEqual(after["status"], "active")
        self.assertIn("source unavailable", after["last_result"])
        judge.assert_not_called()
        self.notify.assert_not_called()

    def test_camera_with_a_motion_sensor_is_not_polled_unless_a_timing_is_given(self):
        plain = self.camera_watch()
        self.assertNotIn("trigger", plain)
        self.assertEqual(plain["schedule"], {"every_minutes": agenda.WATCH_DEFAULT_MINUTES})
        moving = self.camera_watch(motion=True)
        self.assertEqual((moving["trigger"], moving["schedule"]),
                         ("motion", {"every_minutes": agenda.MOTION_FALLBACK_MINUTES}))
        self.assertEqual(moving["next_run"], self.now)  # the first look is still at once
        self.assertEqual(self.camera_watch(motion=True, every_minutes=1)["schedule"], {"every_minutes": 1})

    def test_a_picture_is_watched_for_the_time_frame_the_user_gave_and_never_a_default(self):
        with self.assertRaisesRegex(ValueError, "ask them how long"):
            self.camera_watch(for_minutes=None)
        with self.assertRaisesRegex(ValueError, "ask them how long"):
            agenda.create({"title": "x", "kind": "watch", "screen": True, "condition": "c"}, now=self.now)
        short = self.camera_watch(for_minutes=20)
        self.assertEqual((short["schedule"], short["expires_at"]), ({"every_minutes": 1.0}, self.now + 1200))
        self.assertEqual(self.camera_watch(motion=True, for_minutes=20)["schedule"], {"every_minutes": 1.0})
        self.assertEqual(self.camera_watch(motion=True, for_minutes=240)["schedule"], {"every_minutes": 8.0})
        self.assertEqual(self.camera_watch(for_minutes=45)["schedule"], {"every_minutes": 1.5})
        self.assertEqual(self.camera_watch(for_minutes=20, every_minutes=5)["schedule"], {"every_minutes": 5})
        self.assertIn("until", agenda.summary(short))

    def test_a_time_framed_watch_alerts_on_each_change_and_ends_on_time_with_a_count(self):
        job = self.camera_watch(for_minutes=20, every_minutes=7, repeat=True)
        self.look(job["id"], 0.97)
        self.look(job["id"], 0.1, now=self.now + 420)
        after, _ = self.look(job["id"], 0.97, now=self.now + 840)
        self.assertEqual((after["status"], after["fired"], self.notify.call_count), ("active", 2, 2))
        self.assertEqual(after["next_run"], self.now + 1200)  # the end of the time frame, not the next look
        ended, judge = self.look(job["id"], 0.97, now=self.now + 1200)
        judge.assert_not_called()
        self.assertEqual(ended["status"], "done")
        self.assertIn("2 time(s)", self.notify.call_args.args[1])

    def test_watch_camera_starts_a_repeating_watch_or_sends_back_the_question_for_the_user(self):
        from omarchy_ai.execution import actions
        with patch("omarchy_ai.execution.cameras.names", return_value=["driveway"]), \
                patch("omarchy_ai.execution.cameras.can_wait", return_value=False), \
                patch.object(agenda, "tick"):
            asked = actions.watch_camera({"camera": "Driveway", "condition": "a child is in danger"})
            self.assertFalse(asked.ok)
            self.assertIn("ask them how long", asked.message)
            self.assertEqual(agenda._load(), [])
            started = actions.watch_camera({"camera": "Driveway", "condition": "a child is in danger",
                                            "for_minutes": 30})
        self.assertTrue(started.ok, started.message)
        job = agenda._load()[0]
        self.assertEqual((job["camera"], job["repeat"], job["schedule"], job["title"]),
                         ("driveway", True, {"every_minutes": 1.0}, "Watching the Driveway camera"))

    def test_motion_listener_exists_only_while_its_watch_does_and_looks_on_movement(self):
        class Wait:  # the user's `wait`: movement once, then nothing until terminated
            calls, terminated = 0, threading.Event()

            def __init__(self):
                Wait.calls += 1
                self.first = Wait.calls == 1

            def wait(self, timeout=None):
                if self.first:
                    return 0
                Wait.terminated.wait(5)
                return -15

            def poll(self):
                return None

            def terminate(self):
                Wait.terminated.set()

        looked = threading.Semaphore(0)
        self.addCleanup(lambda: [listener.stop() for listener in agenda._motion.values()])
        with patch("omarchy_ai.execution.cameras.motion_process", lambda name, config, seconds: Wait()), \
                patch.object(agenda, "MOTION_LOOKS", (0, 0)), \
                patch.object(agenda, "run_now", lambda job_id: looked.release()):
            agenda._sync_motion()
            self.assertEqual(agenda._motion, {})  # no camera watch: nothing listens
            self.camera_watch()                   # a camera without a sensor: still nothing
            agenda._sync_motion()
            self.assertEqual(agenda._motion, {})
            job = self.camera_watch(motion=True)
            agenda._sync_motion()
            listener = agenda._motion[job["id"]]
            self.assertTrue(looked.acquire(timeout=5) and looked.acquire(timeout=5))  # one look per pause
            agenda.cancel(job["id"])
            self.assertEqual(agenda._motion, {})
            self.assertTrue(Wait.terminated.wait(5))  # the wait is ended, not left to run out
            listener.join(5)
            self.assertFalse(listener.is_alive())
            self.assertFalse(looked.acquire(blocking=False))

    def test_screen_watch_deletes_each_screenshot_it_judged(self):
        job = agenda.create({"title": "Render", "kind": "watch", "screen": True, "for_minutes": 60,
                             "condition": "the render progress bar is full"}, now=self.now)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as shot:
            path = Path(shot.name)
        self.addCleanup(path.unlink, missing_ok=True)
        with patch("omarchy_ai.execution.vision._capture", return_value=path):
            done, _ = self.look(job["id"], 0.95)
        self.assertEqual(done["status"], "done")
        self.assertFalse(path.exists())

    def test_expired_watch_stops(self):
        job = self.watch(expires_in_hours=1)
        after = self.run_watch(job["id"], FakeGateway(lambda *a: 0.0), now=self.now + 3601)
        self.assertEqual(after["status"], "done")

    def test_recurring_reminder_uses_cron_and_notifies(self):
        job = agenda.create({"title": "Check CI", "kind": "remind", "cron": "0 9 * * 1-5"}, now=self.now)
        agenda.run_job(job, Jev(FakeGateway(lambda *a: 0)), job["next_run"])
        after = agenda._load()[0]
        self.notify.assert_called_once_with("Reminder", "Check CI", False)
        self.assertGreater(after["next_run"], job["next_run"])
        self.assertEqual(after["status"], "active")

    def test_assistant_task_is_handed_to_the_next_conversation(self):
        job = agenda.create({"title": "Summarize PRs", "kind": "assistant", "in_minutes": 1,
                             "request": "Summarize my open GitHub PRs"}, now=self.now)
        agenda.run_job(job, Jev(FakeGateway(lambda *a: 0)), self.now + 60)
        [item] = agenda.briefing()
        self.assertEqual(item["outcome"], "due")
        self.assertIn("Summarize my open GitHub PRs", item["detail"])
        self.assertEqual(agenda._load()[0]["status"], "done")
        self.assertEqual(self.hud_refresh.call_count, 2, "create and finish must both refresh an open HUD")

    def test_briefing_is_read_only_until_the_session_confirms(self):
        agenda._inbox_add({"id": "t-1", "title": "x", "kind": "remind"}, "reminded", "d")
        self.assertEqual(len(agenda.briefing()), 1)
        self.assertEqual(len(agenda.briefing()), 1, "building a prompt must not consume results")
        agenda.mark_briefed()
        self.assertEqual(agenda.briefing(), [])

    def test_desktop_goal_waits_while_a_conversation_holds_the_jev_loop(self):
        job = agenda.create({"title": "Dim", "kind": "desktop", "in_minutes": 1, "goal": "brightness 30%"}, now=self.now)
        busy = ActionResult(False, "Another desktop task is running; wait for its result.")
        with patch("omarchy_ai.execution.desktop_jev.desktop_task", return_value=busy):
            agenda.run_job(job, Jev(FakeGateway(lambda *a: 0)), self.now + 60)
        after = agenda._load()[0]
        self.assertEqual((after["status"], after["next_run"]), ("active", self.now + 120))
        self.notify.assert_not_called()

    def test_desktop_goal_reports_only_verified_completion(self):
        job = agenda.create({"title": "Dim", "kind": "desktop", "in_minutes": 1, "goal": "brightness 30%"}, now=self.now)
        handoff = ActionResult(False, json.dumps({"status": "handoff", "reason": "uncertain", "verified_steps": []}))
        with patch("omarchy_ai.execution.desktop_jev.desktop_task", return_value=handoff):
            agenda.run_job(job, Jev(FakeGateway(lambda *a: 0)), self.now + 60)
        self.assertEqual(self.notify.call_args.args[0], "Couldn't finish")
        self.assertEqual(agenda.briefing()[0]["outcome"], "needs_you")

    def test_command_task_runs_stored_command_and_reports_exit(self):
        job = agenda.create({"title": "Nightly pull", "kind": "command", "in_minutes": 1,
                             "command": "echo hi; exit 3"}, now=self.now)
        agenda.run_job(job, Jev(FakeGateway(lambda *a: 0)), self.now + 60)
        self.assertEqual(agenda._load()[0]["last_result"], "failed (exit 3)")
        self.notify.assert_called_once_with("Nightly pull", "Command failed (exit 3)", True)

    def test_a_crashing_job_is_recorded_not_raised(self):
        job = agenda.create({"title": "x", "kind": "remind", "in_minutes": 1}, now=self.now)
        with patch.dict(agenda.RUNNERS, remind=lambda *a: 1 / 0):
            agenda.run_job(job, None, self.now + 60)
        self.assertIn("crashed", agenda._load()[0]["last_result"])


class SkillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        p = patch.object(skills, "SKILLS_DIR", Path(temp.name) / "skills")
        p.start()
        self.addCleanup(p.stop)
        skills.save("relay-to-claude", "Send a prompt to Claude Code running in a terminal.",
                    "1. focus_window the claude terminal\n2. type_text the prompt verbatim\n3. press_key Return")
        skills.save("restart-waybar", "Fix a frozen or missing top bar by restarting it.",
                    "Run omarchy-restart-waybar; check the bar reappears.")

    def test_save_revises_in_place_and_round_trips(self):
        skill, created = skills.save("Relay to Claude!", "Send a prompt to Claude Code running in a terminal.",
                                     "1. focus_window\n2. type_text verbatim (multi-line pastes)\n3. press_key Return")
        self.assertFalse(created)
        self.assertEqual(skill["name"], "relay-to-claude")
        self.assertEqual(skill["revision"], "2")
        self.assertIn("multi-line pastes", skill["body"])
        self.assertEqual(len(skills.roster()), 2)

    def test_a_new_name_for_a_saved_job_updates_that_skill(self):
        from omarchy_ai.execution import actions
        skills.save("restart-waybar", "Fix a frozen or missing top bar by restarting it.",
                    "Run omarchy-restart-waybar.\n\n" + skills.COMMANDS_HEADING + "t1, 2026-10-12):\nomarchy-restart-waybar")
        gateway = FakeGateway(lambda name, q, state: "restart-waybar")
        with patch.object(skills, "Jev", lambda: Jev(gateway)):
            result = actions.run_action("save_skill", {
                "name": "bring the bar back", "description": "Restart the top bar when it has disappeared.",
                "instructions": "1. Run the bar restart command\n2. Check the bar is back"})
        self.assertTrue(result.ok)
        self.assertIn("updated skill restart-waybar (revision 3)", result.message)
        self.assertIn("instead of creating 'bring-the-bar-back'", result.message)
        self.assertEqual(len(skills.roster()), 2)
        body = skills.load("restart-waybar")["body"]
        self.assertIn("2. Check the bar is back", body)
        self.assertIn(skills.COMMANDS_HEADING + "t1", body)  # what a task recorded is kept
        state, questions = gateway.calls[0]
        self.assertEqual(state["new_skill"]["name"], "bring-the-bar-back")
        self.assertEqual(set(questions["same"]["criteria"]), {"relay-to-claude", "restart-waybar", "new"})

    def test_a_new_job_or_no_jev_creates_the_skill_and_a_known_name_is_not_asked(self):
        from omarchy_ai.execution import actions
        new = {"name": "water-the-plants", "description": "Water the plants for five minutes.",
               "instructions": "1. Open the valve\n2. Wait five minutes\n3. Close the valve"}
        gateway = FakeGateway(lambda name, q, state: "new")
        with patch.object(skills, "Jev", lambda: Jev(gateway)):
            self.assertIn("created skill water-the-plants", actions.run_action("save_skill", new).message)
            actions.run_action("save_skill", new)  # its own name: rewritten, nobody asked
        self.assertEqual(len(gateway.calls), 1)

        def down(state, questions, timeout=8):
            raise RuntimeError("HTTP 503")
        with patch.object(skills, "Jev", lambda: Jev(down)):
            result = actions.run_action("save_skill", {**new, "name": "plants-again"})
        self.assertIn("created skill plants-again", result.message)

    def test_start_task_runs_a_goal_as_the_saved_skill_that_does_it(self):
        from omarchy_ai.runtime import service

        def answer(fits):
            def reply(name, q, state):
                if name == "which":
                    return "restart-waybar"
                if name.startswith("gate::"):
                    return 0.1 if name.endswith("prose_suffices") else 0.9
                return fits if name == "fits::restart-waybar" else 0.05
            return Jev(FakeGateway(reply))
        goal = service._skill_for("the top bar is gone, bring it back", answer(0.9))
        self.assertEqual(skills.skill_of_goal(goal), "restart-waybar")
        self.assertEqual(skills.request_of_goal(goal), "the top bar is gone, bring it back")
        self.assertIn("Run omarchy-restart-waybar", goal)
        # A loose fit is enough to show the worker a skill as reference, not to hand it its steps as the job.
        loose = service._skill_for("make the bar taller", answer(0.5))
        self.assertIsNone(skills.skill_of_goal(loose))
        self.assertTrue(loose.startswith("make the bar taller\n\nREFERENCE ONLY"), loose)
        self.assertIsNone(service._skill_for(goal, answer(0.9)))  # already a skill run

        def down(state, questions, timeout=8):
            raise RuntimeError("HTTP 503")
        self.assertIsNone(service._skill_for("the top bar is gone", Jev(down)))

    def test_a_related_skill_reaches_the_worker_as_reference_only(self):
        # 2026-10-15 20:47: a request to lock one computer fitted a countdown that ends by locking it 0.57;
        # the countdown rightly did not run, but the worker never saw the command that locks that computer.
        from omarchy_ai.runtime import service
        skills.save("countdown-lock", "Announce a countdown on a speaker, then lock the kids' computers.",
                    "1. Announce on media_player.example_room\n2. Wait\n3. omarchy-ai-tool run home_assistant "
                    "script.turn_on '{\"entity_id\": \"script.lock_child_b_pc\"}'")

        def answer(fits):
            def reply(name, q, state):
                if name == "which":
                    return "countdown-lock"
                if name.startswith("gate::"):
                    return 0.1 if name.endswith("prose_suffices") else 0.9
                return fits if name == "fits::countdown-lock" else 0.05
            return Jev(FakeGateway(reply))
        request = "Lock child B's computer immediately."
        goal = service._skill_for(request, answer(0.57))
        self.assertTrue(goal.startswith(request + "\n\n"), goal)
        self.assertIsNone(skills.skill_of_goal(goal))  # not a run of the skill
        reference = goal[len(request):]
        self.assertIn("REFERENCE ONLY", reference)
        self.assertIn("NOT this job", reference)
        self.assertIn("'countdown-lock'", reference)
        self.assertIn("Announce a countdown on a speaker", reference)  # its description
        self.assertIn("script.lock_child_b_pc", reference)  # the fact the worker needs
        self.assertIsNone(service._skill_for(request, answer(0.1)))  # unrelated: the goal stays as asked
        run = service._skill_for("give the kids a 3 minute countdown", answer(0.85))
        self.assertEqual(skills.skill_of_goal(run), "countdown-lock")
        self.assertNotIn("REFERENCE ONLY", run)
        # Clipped to fit, and left out when the goal has no room for it.
        skills.save("countdown-lock", "Announce a countdown on a speaker, then lock the kids' computers.",
                    "step\n" * 1000)
        clipped = service._skill_for(request, answer(0.57))
        self.assertLessEqual(len(clipped) - len(request), service.REFERENCE_CHARS + 400)
        self.assertTrue(clipped.endswith("…"))
        self.assertIsNone(service._skill_for(request + " x" * 1900, answer(0.57)))

    def test_suggest_two_stage_picks_the_fitting_skill(self):
        def answer(name, q, state):
            if name == "which":
                return "relay-to-claude"
            if name.startswith("gate::"):
                return 0.1 if name.endswith("prose_suffices") else 0.9
            return 0.95 if name == "fits::relay-to-claude" else 0.05
        gateway = FakeGateway(answer)
        picked = skills.suggest("tell claude to run the tests", Jev(gateway))
        self.assertEqual(picked["skill"], "relay-to-claude")
        self.assertEqual(len(gateway.calls), 2)
        self.assertIn("focus_window", json.dumps(gateway.calls[1][1]))  # second call reads the body

    def test_suggest_stays_quiet_for_chat_and_for_no_fit(self):
        chat = FakeGateway(lambda n, q, s: "restart-waybar" if n == "which" else
                           (0.95 if n.endswith("prose_suffices") else 0.05))
        self.assertIsNone(skills.suggest("what is a monad?", Jev(chat))["skill"])
        self.assertEqual(len(chat.calls), 1)
        nofit = FakeGateway(lambda n, q, s: "restart-waybar" if n == "which" else
                            (0.1 if n.endswith("prose_suffices") else 0.9) if n.startswith("gate::") else 0.1)
        self.assertIsNone(skills.suggest("post this to Mastodon", Jev(nofit))["skill"])

    def test_bad_input_is_refused(self):
        with self.assertRaises(ValueError):
            skills.save("x", "short", "also too short")
        with self.assertRaises(ValueError):
            skills.slug("!!!")
        self.assertFalse(skills.delete("../../etc"))

    def test_reading_a_skill_says_that_nothing_ran(self):
        from omarchy_ai.execution import actions
        loaded = json.loads(actions.run_action("load_skill", {"name": "restart-waybar"}).message)
        self.assertIn("omarchy-restart-waybar", loaded["body"])
        self.assertIn("nothing has been run", loaded["note"])
        self.assertIn("run_skill(name='restart-waybar'", loaded["note"])
        with patch.object(skills, "suggest", return_value={"skill": "restart-waybar", "reason": "fits"}):
            found = json.loads(actions.run_action("find_skill", {"request": "the bar is gone"}).message)
        self.assertIn("run_skill(name='restart-waybar'", found["note"])
        with patch.object(skills, "suggest", return_value={"skill": None, "reason": "No saved skill does this."}):
            self.assertNotIn("note", json.loads(actions.run_action("find_skill", {"request": "sing"}).message))

    def test_a_run_goal_names_its_skill_and_always_fits(self):
        skill = skills.load("relay-to-claude")
        goal = skills.run_goal(skill, "tell claude to run\nthe tests")
        self.assertTrue(goal.startswith(skills.RUN_PREFIX + "'relay-to-claude'."))
        self.assertEqual(skills.skill_of_goal(goal), "relay-to-claude")
        self.assertEqual(skills.request_of_goal(goal), "tell claude to run the tests")
        self.assertLess(goal.index("tell claude"), goal.index("1. focus_window"))
        self.assertIn("worked before", goal)
        self.assertIn("real wait", goal)
        # The saved values are one run's: the request's values win, and repeated steps scale to it.
        self.assertIn("take every value from the request", goal)
        self.assertIn("use a saved one only where the request gives none", goal)
        self.assertIn("Scale repeated steps to the request", goal)
        long = skills.run_goal({"name": "long-one", "body": "step\n" * 3000}, "do the long one " * 200)
        self.assertLessEqual(len(long), 3900)
        self.assertEqual(skills.skill_of_goal(long), "long-one")
        for other in ("", "Escalated from the voice assistant (x).", "Carry out the saved skill 'Not A Slug'.\n"):
            self.assertIsNone(skills.skill_of_goal(other))

    def test_a_skill_task_is_titled_by_the_users_request(self):
        from omarchy_ai.runtime.service import _job_text
        from omarchy_ai.runtime.task import request_text
        goal = skills.run_goal(skills.load("restart-waybar"), "bring the top bar back")
        self.assertEqual(request_text(goal), "bring the top bar back")
        self.assertEqual(_job_text(goal), "bring the top bar back (saved skill restart-waybar)")

    def test_run_skill_starts_a_task_and_returns_its_result(self):
        from omarchy_ai.execution import actions
        started = ActionResult(True, json.dumps({"task_id": "20261012-090000-abc123", "status": "started"}))
        with patch("omarchy_ai.runtime.service.start_task", return_value=started) as start:
            result = actions.run_action("run_skill", {"name": "restart-waybar", "request": "bring the top bar back"})
        self.assertIs(result, started)
        goal = start.call_args.args[0]["goal"]
        self.assertEqual(skills.skill_of_goal(goal), "restart-waybar")
        self.assertIn("bring the top bar back", goal)
        self.assertIn("omarchy-restart-waybar", goal)

    def test_run_skill_without_a_name_lets_jev_pick_and_refuses_when_nothing_fits(self):
        from omarchy_ai.execution import actions
        started = ActionResult(True, json.dumps({"task_id": "20261012-090000-abc123", "status": "started"}))
        with patch("omarchy_ai.runtime.service.start_task", return_value=started) as start:
            with patch.object(skills, "suggest", return_value={"skill": "relay-to-claude", "reason": "fits"}):
                self.assertTrue(actions.run_action("run_skill", {"request": "tell claude to run the tests"}).ok)
            self.assertEqual(skills.skill_of_goal(start.call_args.args[0]["goal"]), "relay-to-claude")
            start.reset_mock()
            with patch.object(skills, "suggest", return_value={"skill": None, "reason": "No saved skill does this."}):
                missed = actions.run_action("run_skill", {"request": "post this to Mastodon"})
            unknown = actions.run_action("run_skill", {"name": "no-such-skill", "request": "go"})
            empty = actions.run_action("run_skill", {})
        self.assertFalse(missed.ok)
        self.assertIn("nothing was run", missed.message)
        self.assertFalse(unknown.ok or empty.ok)
        start.assert_not_called()

    def test_wants_run_is_jevs_reading_of_the_request(self):
        skill = skills.load("restart-waybar")
        run = FakeGateway(lambda n, q, s: 0.93)
        self.assertTrue(skills.wants_run("bring the top bar back", skill, Jev(run)))
        state, questions = run.calls[0]
        self.assertEqual(state["user_latest_request"], "bring the top bar back")
        self.assertEqual(state["saved_procedure"]["name"], "restart-waybar")
        self.assertEqual(list(questions), ["run_now"])
        self.assertFalse(skills.wants_run("what does the bar skill say?", skill, Jev(FakeGateway(lambda n, q, s: 0.2))))
        self.assertFalse(skills.wants_run("bring the top bar back", skill, Jev(FakeGateway(lambda n, q, s: 0.79))))

        def down(state, questions, timeout=8):
            raise RuntimeError("no gateway in tests")
        self.assertFalse(skills.wants_run("bring the top bar back", skill, Jev(down)))


class LearnFromTaskTests(unittest.TestCase):
    """A certified multi-step task becomes a skill made of its own record."""

    REQUEST = "water the plants every minute for five minutes"

    def setUp(self):
        from omarchy_ai.execution import passwords
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        for p in (patch.object(skills, "SKILLS_DIR", Path(temp.name) / "skills"),
                  patch.object(passwords, "known_secrets", lambda: [])):  # no keyring in tests
            p.start()
            self.addCleanup(p.stop)

    def task(self, commands, goal=None, task_id="20261012-101010-abc123", objective="Water the plants once a minute."):
        from omarchy_ai.runtime.task import Task
        task = Task(id=task_id, goal=goal or self.REQUEST, workspace="/nowhere", objective=objective)
        task.finish_step(task.add_step("SYSTEM_AGENT", "do it", "work"), "done", "Watered five times, a minute apart.")
        task.add_step("TESTER", "check it", "test")
        for command in commands:
            record = {"command": command, "step": 1, "role": "", "decision": "allow", "exit_code": 0}
            task.add_command({**record, **command} if isinstance(command, dict) else record)
        return task

    @staticmethod
    def jev(worth=0.9, kept=lambda command: 0.9, same="new"):
        def answer(name, q, state):
            if name == "worth":
                return worth
            if name == "same":
                return same
            return kept(state["commands"][name.removeprefix("kept::")])
        gateway = FakeGateway(answer)
        return Jev(gateway), gateway

    def test_a_single_command_is_not_a_procedure_and_jev_is_not_asked(self):
        jev, gateway = self.jev()
        self.assertIsNone(skills.learn_from_task(self.task(["valve open"]), jev))
        only_one_worked = self.task(["valve open", {"command": "valve close", "exit_code": 1},
                                     {"command": "rm -rf /", "decision": "deny", "exit_code": None},
                                     {"command": "valve status", "role": "test"},
                                     {"command": "valve status", "step": 2},
                                     {"command": "[executor] claude -p …"}])
        self.assertIsNone(skills.learn_from_task(only_one_worked, jev))
        self.assertEqual(gateway.calls, [])
        self.assertEqual(skills.roster(), [])

    def test_a_job_jev_does_not_find_reusable_is_not_saved(self):
        jev, gateway = self.jev(worth=0.3)
        self.assertIsNone(skills.learn_from_task(self.task(["valve open", "valve close"]), jev))
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(skills.roster(), [])

    def test_a_new_skill_holds_only_the_commands_that_made_the_result(self):
        jev, gateway = self.jev(kept=lambda command: 0.1 if command.startswith(("ls", "valve status")) else 0.9)
        task = self.task(["ls ~/garden", "valve open", {"command": "valve open --force", "exit_code": 1},
                          "sleep 60", "valve close", "valve status", {"command": "valve status", "role": "test"}])
        learned = skills.learn_from_task(task, jev)
        self.assertTrue(learned["created"])
        skill = learned["skill"]
        self.assertEqual(skill["name"], "water-the-plants-every-minute-for")
        self.assertEqual(skill["description"], "Water the plants once a minute.")
        self.assertIn(f"Request: {self.REQUEST}", skill["body"])
        self.assertIn(f"Commands that worked (task {task.id}, ", skill["body"])
        # The commands carry that request's values; a new request's replace them.
        self.assertIn(f"):\n{skills.VALUES_NOTE}\nvalve open", skill["body"])
        self.assertIn("came from that request", skills.VALUES_NOTE)
        self.assertIn("valve open\nsleep 60\nvalve close\n", skill["body"])
        self.assertIn("Result: Watered five times, a minute apart.", skill["body"])
        for left_out in ("ls ~/garden", "--force", "valve status"):
            self.assertNotIn(left_out, skill["body"])
        self.assertEqual(len(gateway.calls), 1)  # one ask holds every decision
        state, questions = gateway.calls[0]
        self.assertEqual(list(state["commands"].values()), ["ls ~/garden", "valve open", "sleep 60", "valve close",
                                                            "valve status"])
        self.assertNotIn("same", questions)  # nothing saved yet to compare with

    def test_when_jev_keeps_no_command_all_that_worked_are_saved(self):
        jev, _ = self.jev(kept=lambda command: 0.2)
        body = skills.learn_from_task(self.task(["valve open", "valve close"]), jev)["skill"]["body"]
        self.assertIn("valve open\nvalve close", body)

    def test_an_existing_skill_keeps_its_hand_written_steps_and_gains_the_commands(self):
        skills.save("water-plants", "Water the plants in rounds, a minute apart.",
                    "1. Open the valve.\n2. Wait a minute.\n3. Close it. Never leave it open.")
        jev, gateway = self.jev(same="water-plants")
        learned = skills.learn_from_task(self.task(["valve open", "sleep 60", "valve close"]), jev)
        self.assertFalse(learned["created"])
        self.assertIn("same", gateway.calls[0][1])
        skill = skills.load("water-plants")
        self.assertEqual(skill["description"], "Water the plants in rounds, a minute apart.")
        self.assertTrue(skill["body"].startswith("1. Open the valve.\n2. Wait a minute.\n3. Close it. Never leave it open."))
        self.assertIn("valve open\nsleep 60\nvalve close", skill["body"])
        # The next run replaces the recorded commands, never the steps.
        again = self.task(["valve open --zone 2", "sleep 60", "valve close --zone 2"], task_id="20261013-080000-def456")
        skills.learn_from_task(again, jev)
        body = skills.load("water-plants")["body"]
        self.assertIn("Never leave it open.", body)
        self.assertIn("valve open --zone 2", body)
        self.assertNotIn("valve open\nsleep", body)
        self.assertEqual(body.count(skills.COMMANDS_HEADING), 1)
        self.assertEqual(len(skills.roster()), 1)

    def test_a_task_that_ran_a_skill_updates_that_skill_without_asking_which(self):
        skills.save("water-plants", "Water the plants in rounds, a minute apart.", "1. Open the valve, wait, close it.")
        skills.save("feed-the-cat", "Fill the cat feeder.", "Run the feeder for ten seconds.")
        jev, gateway = self.jev(same="feed-the-cat")  # would be the wrong answer; it is never asked
        goal = skills.run_goal(skills.load("water-plants"), self.REQUEST)
        learned = skills.learn_from_task(self.task(["valve open", "sleep 60", "valve close"], goal=goal), jev)
        self.assertEqual((learned["skill"]["name"], learned["created"]), ("water-plants", False))
        self.assertNotIn("same", gateway.calls[0][1])
        self.assertEqual(gateway.calls[0][0]["request"], self.REQUEST)
        self.assertIn("valve open", skills.load("water-plants")["body"])
        self.assertNotIn("valve", skills.load("feed-the-cat")["body"])

    def test_a_command_showing_a_credential_is_left_out(self):
        import secrets
        value = secrets.token_hex(12)  # made here: no secret-looking literal in the repo
        jev, gateway = self.jev()
        task = self.task([f"curl -H 'Authorization: Bearer {value}' https://valve.invalid/open", "sleep 60",
                          f"valve close --token {value}", "valve close"])
        body = skills.learn_from_task(task, jev)["skill"]["body"]
        self.assertNotIn(value, body)
        self.assertNotIn(value, json.dumps(gateway.calls))
        self.assertIn("sleep 60\nvalve close", body)

    def test_a_saved_password_is_masked_in_what_is_saved(self):
        import secrets
        from omarchy_ai.execution import passwords
        value = secrets.token_hex(8)
        jev, _ = self.jev()
        with patch.object(passwords, "known_secrets", lambda: [value]):
            body = skills.learn_from_task(self.task([f"valve login {value}", "valve open"]), jev)["skill"]["body"]
        self.assertNotIn(value, body)
        self.assertIn(f"valve login {passwords.MASK}", body)

    def coding_agent_task(self, reported):
        """Shaped like the real records: the agent's commands ran inside its
        own CLI, so the harness holds its invocation and the checks only."""
        from omarchy_ai.runtime.task import Task
        task = Task(id="20261012-111111-abc123", goal=self.REQUEST, workspace="/nowhere",
                    objective="Water the plants once a minute.")
        work = task.add_step("CODEX", "do it", "work")
        task.finish_step(work, "done", "SUMMARY: watered five times.")
        work["meta"] = {"exit_code": 0, "mode": "write", **({"commands": reported} if reported else {})}
        task.finish_step(task.add_step("CODEX", "design the checks", "test"), "done", "TEST_COMMAND: valve status")
        task.steps[-1]["meta"] = {"commands": ["valve status"]}  # never read from a test step
        task.add_command({"command": "[executor] codex exec --json …", "step": 1, "decision": "allow", "exit_code": 0})
        task.add_command({"command": "[executor] codex:write:/nowhere", "step": 1, "decision": "allow"})
        task.add_command({"command": "valve status", "step": 2, "role": "test", "decision": "allow", "exit_code": 0})
        return task

    def test_a_coding_agents_job_is_saved_from_the_commands_it_reported(self):
        jev, gateway = self.jev()
        task = self.coding_agent_task(["valve open", "sleep 60", "valve close"])
        self.assertEqual(skills._worked(task), [])
        learned = skills.learn_from_task(task, jev)
        self.assertTrue(learned["created"])
        body = learned["skill"]["body"]
        self.assertIn(f"Commands the worker reported (not observed by the harness; task {task.id}, ", body)
        self.assertNotIn(skills.COMMANDS_HEADING, body)
        self.assertIn("valve open\nsleep 60\nvalve close", body)
        self.assertNotIn("valve status", body)
        self.assertNotIn("[executor]", body)
        self.assertIn("Result: SUMMARY: watered five times.", body)  # the work step's account, not the check's
        self.assertEqual(list(gateway.calls[0][0]["commands"].values()), ["valve open", "sleep 60", "valve close"])

    def test_a_coding_agents_job_with_no_reported_commands_saves_nothing(self):
        jev, gateway = self.jev()
        self.assertIsNone(skills.learn_from_task(self.coding_agent_task([]), jev))
        self.assertIsNone(skills.learn_from_task(self.coding_agent_task(["valve open"]), jev))  # one is no procedure
        self.assertEqual(gateway.calls, [])
        self.assertEqual(skills.roster(), [])

    def test_a_reported_command_showing_a_credential_is_dropped_and_a_fetched_one_is_kept(self):
        import secrets
        value = secrets.token_hex(12)
        jev, _ = self.jev()
        task = self.coding_agent_task([f"valve login --token {value}", "valve login --token $(vault get valve)",
                                       "valve open", "valve close"])
        body = skills.learn_from_task(task, jev)["skill"]["body"]
        self.assertNotIn(value, body)
        self.assertIn("valve login --token $(vault get valve)\nvalve open\nvalve close", body)

    def test_either_kind_of_recorded_commands_is_replaced_and_the_steps_kept(self):
        skills.save("water-plants", "Water the plants in rounds, a minute apart.", "1. Open the valve, wait, close it.")
        jev, _ = self.jev(same="water-plants")
        skills.learn_from_task(self.coding_agent_task(["valve open", "valve close"]), jev)
        self.assertIn(skills.REPORTED_HEADING, skills.load("water-plants")["body"])
        skills.learn_from_task(self.task(["valve open --zone 2", "valve close --zone 2"]), jev)  # the harness ran these
        body = skills.load("water-plants")["body"]
        self.assertTrue(body.startswith("1. Open the valve, wait, close it."))
        self.assertNotIn(skills.REPORTED_HEADING, body)
        self.assertEqual(body.count(skills.COMMANDS_HEADING), 1)
        skills.learn_from_task(self.coding_agent_task(["valve open", "valve close"]), jev)
        body = skills.load("water-plants")["body"]
        self.assertNotIn(skills.COMMANDS_HEADING, body)
        self.assertNotIn("--zone 2", body)
        self.assertTrue(body.startswith("1. Open the valve, wait, close it."))

    def test_the_reported_section_is_read_from_the_end_of_a_report(self):
        marker = skills.COMMANDS_MARKER
        report = f"SUMMARY: watered.\nRISKS: none\n{marker}\nvalve open\nsleep 60\nvalve close\n"
        self.assertEqual(skills.reported_commands(report), ["valve open", "sleep 60", "valve close"])
        self.assertEqual(skills.reported_commands("SUMMARY: watered.\nRISKS: none"), [])  # no section
        for empty in (f"RISKS: none\n{marker}", f"RISKS: none\n{marker}\n\n", f"{marker}\nnone\n",
                      f"{marker}\n<last: the exact shell commands>\n"):
            self.assertEqual(skills.reported_commands(empty), [], empty)
        fenced = f"x\n**{marker}**\n```sh\n$ valve open\n- valve close\n```\nThat is all I did."
        self.assertEqual(skills.reported_commands(fenced), ["valve open", "valve close"])
        self.assertEqual(skills.reported_commands(f"{marker}\n\nvalve open\nvalve close\n\nLet me know.\n"),
                         ["valve open", "valve close"])
        self.assertEqual(skills.reported_commands(f"{marker}\nvalve open\nRISKS: none\nvalve close"), ["valve open"])
        self.assertEqual(skills.reported_commands(f"{marker}\nold\n\nlater\n{marker}\nvalve open"), ["valve open"])
        many = skills.reported_commands(marker + "\n" + "\n".join(f"echo {n} " + "x" * 900 for n in range(50)))
        self.assertEqual((len(many), max(map(len, many))), (30, 600))

    def test_jev_unavailable_saves_nothing(self):
        def down(state, questions, timeout=8):
            raise RuntimeError("no gateway in tests")
        self.assertIsNone(skills.learn_from_task(self.task(["valve open", "valve close"]), Jev(down)))
        self.assertEqual(skills.roster(), [])

    def test_names_fall_back_to_the_task_and_never_take_over_another_skill(self):
        jev, _ = self.jev()
        unnamed = self.task(["valve open", "valve close"], goal="¡¿ … ?!", objective="Open and close the valve.")
        self.assertEqual(skills.learn_from_task(unnamed, jev)["skill"]["name"], f"task-{unnamed.id}")
        first = skills.learn_from_task(self.task(["valve open", "valve close"]), jev)["skill"]["name"]
        other = self.task(["feeder on", "feeder off"], task_id="20261013-080000-def456")
        second = skills.learn_from_task(other, jev)  # Jev: not the same procedure as any saved one
        self.assertTrue(second["created"])
        self.assertNotEqual(second["skill"]["name"], first)
        self.assertTrue(second["skill"]["name"].endswith(other.id))
        self.assertIn("valve open", skills.load(first)["body"])


if __name__ == "__main__":
    unittest.main()


class DaemonWakeTests(unittest.TestCase):
    def daemon(self, state, session=None):
        import threading
        from omarchy_ai.config import Config
        from omarchy_ai.core.daemon import OmaDaemon
        d = OmaDaemon.__new__(OmaDaemon)
        d.config, d._state, d._session = Config(provider="gemini"), state, session
        d._pending_announcements, d._manual, d._listen_stop = [], False, threading.Event()
        return d

    def test_idle_assistant_is_woken_to_report(self):
        d = self.daemon("listening")
        d._on_agenda_result({"id": "r1", "title": "Claude is done"})
        self.assertTrue(d._listen_stop.is_set())       # breaks out of wake-word listening
        self.assertTrue(d._manual)
        self.assertEqual(d._pending_announcements[0]["id"], "r1")

    def test_open_conversation_hears_it_without_a_second_session(self):
        from unittest.mock import Mock
        session = Mock()
        d = self.daemon("active", session)
        d._on_agenda_result({"id": "r2", "title": "Build finished"})
        session.announce.assert_called_once()
        self.assertFalse(d._listen_stop.is_set())
