import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

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
        self.notify = patch.object(agenda, "notify").start()
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
                                 "terminal": "ben-ami@Jarvis-HQ:~", "condition": "the transfer finished"}, now=self.now)
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

    def test_expired_watch_stops(self):
        job = self.watch(expires_in_hours=1)
        after = self.run_watch(job["id"], FakeGateway(lambda *a: 0.0), now=self.now + 3601)
        self.assertEqual(after["status"], "done")

    def test_recurring_reminder_uses_cron_and_notifies(self):
        job = agenda.create({"title": "Check CI", "kind": "remind", "cron": "0 9 * * 1-5"}, now=self.now)
        agenda.run_job(job, Jev(FakeGateway(lambda *a: 0)), job["next_run"])
        after = agenda._load()[0]
        self.notify.assert_called_once_with("Reminder", "Check CI")
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
