import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.core import agenda, schedule
from omarchy_ai.execution import actions
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.verified_input import writes_externally


def _schema(name, props, required=()):
    return {"name": name, "description": name, "parameters": {
        "type": "object", "properties": props, "required": list(required)}}


REGISTRY = {
    "device": _schema("device", {"operation": {"type": "string", "enum": ["ask", "call"]},
                                 "text": {"type": "string"}}, ["operation"]),
    "volume_set": _schema("volume_set", {"percent": {"type": "integer"}}, ["percent"]),
    "nightlight_toggle": _schema("nightlight_toggle", {}),
    "battery_status": _schema("battery_status", {}),
}


def at(hour, minute=0, day=30):
    return datetime(2026, 9, day, hour, minute).timestamp()


class ClockTests(unittest.TestCase):
    def test_clock_times_in_every_spoken_shape(self):
        now = at(17, 33)
        self.assertEqual(schedule.parse_at("18:30", now), at(18, 30))
        self.assertEqual(schedule.parse_at("6:30 pm", now), at(18, 30))
        self.assertEqual(schedule.parse_at("6:30PM", now), at(18, 30))
        self.assertEqual(schedule.parse_at("today 18:30", now), at(18, 30))
        self.assertEqual(schedule.parse_at("tomorrow 9am", now), datetime(2026, 10, 1, 9).timestamp())
        self.assertEqual(schedule.parse_at("2026-09-30T18:30", now), at(18, 30))
        self.assertEqual(schedule.parse_at("2026-09-30 18:30:00", now), at(18, 30))
        self.assertEqual(schedule.parse_at("12:15 am", now), datetime(2026, 10, 1, 0, 15).timestamp())
        # A bare time already gone is tomorrow's; one said a moment ago is now.
        self.assertEqual(schedule.parse_at("09:00", now), datetime(2026, 10, 1, 9).timestamp())
        self.assertEqual(schedule.parse_at("17:33", now + 20), at(17, 33))
        for bad in ("25:00", "13:00 pm", "18", "soon"):
            with self.assertRaises(ValueError):
                schedule.parse_at(bad, now)

    def test_when_names_the_day_so_a_wrong_one_is_heard(self):
        now = at(17, 33)
        self.assertEqual(schedule.describe_when(at(18, 30), now), "today 18:30 (in 57 min)")
        self.assertTrue(schedule.describe_when(datetime(2026, 10, 1, 9).timestamp(), now).startswith("tomorrow 09:00"))
        self.assertTrue(schedule.describe_when(datetime(2026, 10, 6, 16, 30).timestamp(), now)
                        .startswith("Tue 2026-10-06 16:30 (in 6 days)"))


class ActionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        patch.object(agenda, "JOBS_PATH", base / "agenda.json").start()
        patch.object(agenda, "INBOX_PATH", base / "inbox.jsonl").start()
        patch.object(agenda, "_registry", return_value=REGISTRY).start()
        patch.object(agenda, "STEP_RETRY_DELAYS", (0.0, 0.0)).start()
        self.notify = patch.object(agenda, "notify").start()
        patch.object(agenda, "_refresh_routines_hud").start()
        self.run_action = patch("omarchy_ai.execution.actions.run_action").start()
        self.addCleanup(patch.stopall)
        self.heard = []
        agenda.listeners.append(self.heard.append)
        self.addCleanup(agenda.listeners.remove, self.heard.append)
        self.now = at(17, 33)

    def job(self, job_id):
        return next(j for j in agenda._load() if j["id"] == job_id)

    def action(self, **extra):
        args = {"title": "turn on the fan", "kind": "action", "at": "18:30", "tool": "device",
                "args": {"operation": "ask", "text": "turn on the fan"}, **extra}
        return agenda.create(args, now=self.now)

    def test_runs_the_exact_call_at_its_time_and_tells_the_user(self):
        job = self.action()
        self.assertEqual(job["next_run"], at(18, 30))
        self.assertEqual(agenda.due(at(18, 29)), [])
        self.run_action.return_value = ActionResult(True, json.dumps({"answer": "Turned on the fan",
                                                                      "type": "action_done"}))
        self.assertEqual(agenda.tick(at(18, 30), wait=True), [job["id"]])
        self.run_action.assert_called_once_with("device", {"operation": "ask", "text": "turn on the fan"})
        done = self.job(job["id"])
        self.assertEqual((done["status"], done["runs"], done["next_run"]), ("done", 1, None))
        self.notify.assert_called_once_with("Done: turn on the fan", "Turned on the fan")
        self.assertEqual([e["outcome"] for e in self.heard], ["completed"])

    def test_a_tool_that_exits_ok_but_reports_an_error_is_a_failure_and_is_retried(self):
        job = self.action()
        self.run_action.side_effect = [
            ActionResult(True, '{"answer": "Sorry, there are multiple devices called fan", "type": "error"}'),
            ActionResult(False, "device timed out"),
            ActionResult(True, '{"answer": "Sorry, I am not aware of any device called fan", "type": "error"}')]
        agenda.run_job(self.job(job["id"]), now=at(18, 30))
        self.assertEqual(self.run_action.call_count, 3)
        title, body = self.notify.call_args.args
        self.assertEqual(title, "Failed: turn on the fan")
        self.assertIn("not aware of any device", body)
        self.assertTrue(self.notify.call_args.kwargs["urgent"])
        self.assertEqual(self.heard[-1]["outcome"], "failed")
        self.assertTrue(self.heard[-1]["urgent"])

    def test_a_retry_that_succeeds_is_done(self):
        job = self.action()
        self.run_action.side_effect = [ActionResult(False, "HTTP 503"), ActionResult(True, '{"ok": true}')]
        agenda.run_job(self.job(job["id"]), now=at(18, 30))
        self.assertEqual(self.heard[-1]["outcome"], "completed")

    def test_a_toggle_is_never_repeated(self):
        job = agenda.create({"title": "night light", "kind": "action", "in_minutes": 5,
                             "tool": "nightlight_toggle", "args": {}}, now=self.now)
        self.run_action.return_value = ActionResult(False, "failed")
        agenda.run_job(self.job(job["id"]), now=self.now + 300)
        self.assertEqual(self.run_action.call_count, 1)

    def test_a_routine_runs_every_step_in_order_and_stops_at_a_failure(self):
        job = agenda.create({"title": "evening", "kind": "action", "at": "18:30", "steps": [
            {"tool": "volume_set", "args": {"percent": 20}},
            {"tool": "device", "args": {"operation": "ask", "text": "lights off"}},
            {"tool": "battery_status", "args": {}}]}, now=self.now)
        self.run_action.side_effect = [ActionResult(True, "Volume 20%"), ActionResult(False, "unreachable"),
                                       ActionResult(False, "unreachable"), ActionResult(False, "unreachable")]
        agenda.run_job(self.job(job["id"]), now=at(18, 30))
        self.assertEqual([c.args[0] for c in self.run_action.call_args_list], ["volume_set"] + ["device"] * 3)
        self.assertIn("step 2 of 3", self.heard[-1]["detail"])

    def test_a_recurring_routine_moves_to_its_next_occurrence(self):
        job = agenda.create({"title": "morning volume", "kind": "action", "cron": "0 9 * * *",
                             "tool": "volume_set", "args": {"percent": 30}}, now=self.now)
        self.assertEqual(job["next_run"], datetime(2026, 10, 1, 9).timestamp())
        self.run_action.return_value = ActionResult(True, "ok")
        agenda.run_job(self.job(job["id"]), now=job["next_run"])
        after = self.job(job["id"])
        self.assertEqual((after["status"], after["next_run"]), ("active", datetime(2026, 10, 2, 9).timestamp()))

    def test_far_too_late_is_reported_as_missed_and_never_run(self):
        job = self.action()
        agenda.run_job(self.job(job["id"]), now=at(18, 30) + agenda.LATE_GRACE_SECONDS + 60)
        self.run_action.assert_not_called()
        self.assertTrue(self.notify.call_args.args[0].startswith("Missed: "))
        self.assertEqual(self.job(job["id"])["status"], "done")
        self.assertEqual(self.heard[-1]["outcome"], "missed")

    def test_a_missed_one_off_ends_even_if_its_next_run_was_moved(self):
        job = self.action()
        agenda._update(job["id"], next_run=at(17, 0))  # e.g. run_scheduled_task_now, then asleep
        agenda.run_job(self.job(job["id"]), now=at(17, 0) + agenda.LATE_GRACE_SECONDS + 60)
        self.assertEqual((self.job(job["id"])["status"], self.job(job["id"])["next_run"]), ("done", None))
        self.run_action.assert_not_called()

    def test_a_little_late_still_runs(self):
        job = self.action()
        self.run_action.return_value = ActionResult(True, "ok")
        agenda.run_job(self.job(job["id"]), now=at(18, 30) + 90)
        self.run_action.assert_called_once()

    def test_a_run_cut_off_by_a_crash_is_reported_once_and_not_repeated(self):
        job = self.action()
        agenda._update(job["id"], started_at=at(18, 30), next_run=None)
        agenda.tick(at(18, 30) + agenda.STALE_RUN_SECONDS + 1, wait=True)
        agenda.tick(at(18, 30) + agenda.STALE_RUN_SECONDS + 120, wait=True)
        self.run_action.assert_not_called()
        self.assertEqual([e["outcome"] for e in self.heard], ["interrupted"])

    def test_bad_calls_fail_while_the_user_is_still_listening(self):
        for bad, why in (({"tool": "nope", "args": {}}, "no tool named"),
                         ({"tool": "device", "args": {}}, "missing operation"),
                         ({"tool": "device", "args": {"operation": "dance"}}, "must be one of"),
                         ({"tool": "volume_set", "args": {"percent": 5, "x": 1}}, "unknown parameter x"),
                         ({"tool": "task_respond", "args": {}}, "cannot run unattended"),
                         ({}, "needs `tool`"),
                         ({"steps": [{"tool": "battery_status"}] * 11}, "at most")):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, why):
                agenda.create({"title": "x", "kind": "action", "in_minutes": 5, **bad}, now=self.now)
        self.assertEqual(agenda._load(), [])

    def test_steps_and_args_may_arrive_as_json_text(self):
        job = agenda.create({"title": "x", "kind": "action", "in_minutes": 5,
                             "steps": '[{"tool": "volume_set", "args": "{\\"percent\\": 10}"}]'}, now=self.now)
        self.assertEqual(job["steps"], [{"tool": "volume_set", "args": {"percent": 10}}])

    def test_a_past_time_is_refused_with_the_current_time(self):
        with self.assertRaisesRegex(ValueError, "already passed .*it is now Wed 2026-09-30 17:33"):
            self.action(at="2026-09-30T16:30")
        with self.assertRaisesRegex(ValueError, "already passed"):
            self.action(at="today 9:00")
        self.assertEqual(self.action(at="2026-09-30T17:32")["next_run"], self.now)  # said a moment ago

    def test_replace_moves_a_task_in_one_step(self):
        old = self.action(at="2026-10-06T16:30")
        new = self.action(replace_id=old["id"])
        self.assertEqual(self.job(old["id"])["status"], "cancelled")
        self.assertEqual((new["replaces"], new["next_run"]), (old["id"], at(18, 30)))
        with self.assertRaisesRegex(ValueError, "not an active task"):
            self.action(replace_id=old["id"], at="19:00")
        self.assertEqual([j["id"] for j in agenda._load() if j["status"] == "active"], [new["id"]])

    def test_the_heartbeat_wakes_for_the_next_due_job(self):
        agenda.changed.clear()
        self.action()
        self.assertTrue(agenda.changed.is_set())
        self.assertAlmostEqual(agenda.seconds_until_next(self.now), 57 * 60)


class ScheduleToolTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        patch.object(agenda, "JOBS_PATH", base / "agenda.json").start()
        patch.object(agenda, "INBOX_PATH", base / "inbox.jsonl").start()
        patch.object(agenda, "_registry", return_value=REGISTRY).start()
        patch.object(agenda, "_refresh_routines_hud").start()
        patch.object(agenda, "tick").start()
        self.addCleanup(patch.stopall)

    def test_an_assistant_task_one_tool_can_do_becomes_that_exact_call(self):
        step = {"tool": "device", "args": {"operation": "ask", "text": "turn on the fan"}}
        with patch.object(actions, "_resolve_scheduled", return_value=(step, "")):
            result = actions.schedule_task({"title": "fan", "kind": "assistant", "in_minutes": 30,
                                            "request": "turn on the fan"})
        out = json.loads(result.message)
        self.assertEqual((out["scheduled"]["kind"], out["scheduled"]["steps"]), ("action", [step]))
        self.assertIn("in 30 min", out["scheduled"]["when"])

    def test_an_assistant_task_no_tool_does_stays_one(self):
        with patch.object(actions, "_resolve_scheduled", return_value=(None, "No catalog tool does this.")):
            result = actions.schedule_task({"title": "talk", "kind": "assistant", "in_minutes": 30})
        self.assertEqual(json.loads(result.message)["scheduled"]["kind"], "assistant")

    def test_an_action_from_a_request_that_cannot_be_resolved_is_not_scheduled(self):
        with patch.object(actions, "_resolve_scheduled", return_value=(None, "Jev is not sure.")):
            result = actions.schedule_task({"title": "x", "kind": "action", "in_minutes": 5, "request": "x"})
        self.assertFalse(result.ok)
        self.assertIn("NOT scheduled", result.message)
        self.assertEqual(agenda._load(), [])

    def test_a_refusal_says_nothing_changed_and_the_time(self):
        result = actions.schedule_task({"title": "x", "kind": "action", "at": "2020-01-01T10:00",
                                        "tool": "battery_status", "args": {}})
        self.assertFalse(result.ok)
        self.assertIn("do not tell the user it was scheduled", result.message)
        self.assertIn("It is now", result.message)


class UseToolShapeTests(unittest.TestCase):
    def test_a_flat_schedule_call_keeps_the_later_calls_args(self):
        from omarchy_ai.execution import catalog
        tools = catalog.catalog()
        later = {"operation": "ask", "text": "turn on the fan"}
        name, given = catalog._normalize({"name": "schedule_task", "title": "fan", "kind": "action",
                                          "at": "18:30", "tool": "device", "args": later}, tools)
        self.assertEqual((name, given["args"], given["tool"], given["kind"]), ("schedule_task", later, "device", "action"))
        nested = {"title": "fan", "kind": "action", "at": "18:30", "tool": "device", "args": later}
        self.assertEqual(catalog._normalize({"name": "schedule_task", "args": nested}, tools)[1], nested)


class ScheduledWriteTests(unittest.TestCase):
    def test_a_scheduled_external_write_needs_the_same_confirmation(self):
        send = {"tool": "myapi_gmail_send", "args": {}}
        self.assertTrue(writes_externally("schedule_task", {"kind": "action", **send}))
        self.assertTrue(writes_externally("schedule_task", {"kind": "action", "steps": [{"tool": "volume_set"}, send]}))
        self.assertFalse(writes_externally("schedule_task", {"kind": "action", "tool": "volume_set"}))
        self.assertTrue(writes_externally("myapi_gmail_send", {}))


if __name__ == "__main__":
    unittest.main()
