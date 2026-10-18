"""Routines with catch-up policies and event triggers (core/agenda.py,
core/triggers.py): a laptop asleep across a due time is handled by the job's
policy, two processes never run one job twice, and an event trigger fires
once per event, debounced and quiet-hours aware."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.core import agenda, effects, outbox, triggers
from omarchy_ai.execution.actions import ActionResult

from fakes.effects_journal import isolate_journal  # noqa: E402
from fakes.myapi_server import FakeMyApi, fake_identity  # noqa: E402

setUpModule, tearDownModule = isolate_journal()

SRC = str(Path(__file__).resolve().parents[1] / "src")
TESTS = str(Path(__file__).resolve().parent)


def _schema(name, props=None, required=()):
    return {
        "name": name,
        "description": name,
        "parameters": {
            "type": "object",
            "properties": props or {},
            "required": list(required),
        },
    }


REGISTRY = {
    "nightlight_toggle": _schema("nightlight_toggle"),
    "battery_status": _schema("battery_status"),
    "volume_set": _schema("volume_set", {"percent": {"type": "integer"}}, ["percent"]),
}


def at(hour, minute=0, day=30):
    """Local time on 2026-09-<day> (31 is the next morning)."""
    return (datetime(2026, 9, 1, hour, minute) + timedelta(days=day - 1)).timestamp()


class Case(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        patch.object(agenda, "JOBS_PATH", self.base / "agenda.json").start()
        patch.object(agenda, "INBOX_PATH", self.base / "inbox.jsonl").start()
        patch.object(triggers, "STATE_PATH", self.base / "triggers.json").start()
        patch.object(outbox, "LEDGER_PATH", self.base / "outbox.json").start()
        patch.object(agenda, "_registry", return_value=REGISTRY).start()
        self.notify = patch.object(outbox, "_desktop").start()
        patch.object(agenda, "_refresh_routines_hud").start()
        self.run_action = patch(
            "omarchy_ai.execution.actions.run_action",
            return_value=ActionResult(True, "ok"),
        ).start()
        self.addCleanup(patch.stopall)
        self.heard = []
        agenda.listeners.append(self.heard.append)
        self.addCleanup(agenda.listeners.remove, self.heard.append)

    def job(self, job_id):
        return next(j for j in agenda._load() if j["id"] == job_id)

    def outcomes(self):
        return [e["outcome"] for e in agenda._inbox_read()]

    def tick(self, now):
        return agenda.tick(now, jev=object(), wait=True)


class CatchUpTests(Case):
    """The laptop was asleep from 18:00 to 21:00; each job was due at 18:30."""

    def make(self, **args):
        return agenda.create({"title": "t", "at": "18:30", **args}, now=at(17, 0))

    def test_policy_is_chosen_at_creation_and_shown(self):
        toggle = self.make(kind="action", tool="nightlight_toggle", args={})
        read = self.make(kind="action", tool="battery_status", args={})
        remind = self.make(kind="remind")
        chosen = self.make(kind="remind", catch_up="skip")
        self.assertEqual(
            [j["catch_up"] for j in (toggle, read, remind, chosen)],
            ["skip", "run_once_late", "run_once_late", "skip"],
        )
        self.assertTrue(agenda.summary(toggle)["if_missed"].startswith("skip:"))
        self.assertTrue(
            agenda.summary(remind)["if_missed"].startswith("run_once_late:")
        )

    def test_run_all_is_never_offered(self):
        for bad in ("run_all", "sometimes"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValueError, "catch_up"):
                self.make(kind="remind", catch_up=bad)
        self.assertEqual(agenda._load(), [])

    def test_an_irreversible_action_slept_through_is_reported_missed_not_run(self):
        job = self.make(kind="action", tool="nightlight_toggle", args={})
        self.tick(at(21, 0))
        self.run_action.assert_not_called()
        self.assertEqual(self.outcomes(), ["missed"])
        self.assertEqual(self.job(job["id"])["status"], "done")

    def test_a_read_slept_through_runs_once_late_and_says_so(self):
        job = self.make(kind="action", tool="battery_status", args={})
        self.tick(at(21, 0))
        self.tick(at(21, 1))
        self.run_action.assert_called_once()
        self.assertEqual(self.outcomes(), ["completed"])
        self.assertIn("late", agenda._inbox_read()[0]["detail"])
        self.assertEqual(self.job(job["id"])["status"], "done")

    def test_a_reminder_slept_through_is_said_once_late(self):
        self.make(kind="remind")
        self.tick(at(21, 0))
        self.assertEqual(self.outcomes(), ["reminded"])
        self.assertIn("late: was due", self.notify.call_args.args[1])

    def test_a_reminder_chosen_to_skip_is_reported_missed(self):
        self.make(kind="remind", catch_up="skip")
        self.tick(at(21, 0))
        self.assertEqual(self.outcomes(), ["missed"])

    def test_a_recurring_job_that_missed_many_runs_catches_up_once(self):
        job = agenda.create(
            {"title": "t", "kind": "remind", "every_minutes": 30}, now=at(17, 0)
        )
        self.tick(at(21, 0))  # 7 occurrences slept through
        self.assertEqual(self.outcomes(), ["reminded"])
        self.assertEqual(self.job(job["id"])["next_run"], at(21, 30))

    def test_on_time_runs_are_not_late(self):
        self.make(kind="remind")
        self.tick(at(18, 30))
        self.assertNotIn("late", agenda._inbox_read()[0]["detail"])


class ExistingJobTests(Case):
    """Jobs written before catch-up policies and triggers (shapes as in a real
    agenda.json, synthetic values) keep working unchanged."""

    def legacy(self):
        def job(job_id, kind, sched, **extra):
            return {
                "id": job_id,
                "title": f"job {job_id}",
                "kind": kind,
                "created_at": at(9),
                "status": "active",
                "runs": 0,
                "schedule": sched,
                "next_run": at(18, 30),
                **extra,
            }

        return [
            job(
                "t-aaa001",
                "action",
                {"cron": "30 18 * * *"},
                request="r",
                steps=[{"tool": "nightlight_toggle", "args": {}}],
            ),
            job(
                "t-aaa002",
                "action",
                {"at": at(18, 30)},
                request="r",
                steps=[{"tool": "battery_status", "args": {}}],
            ),
            job("t-aaa003", "remind", {"at": at(18, 30)}),
            dict(
                job(
                    "t-aaa004",
                    "watch",
                    {"every_minutes": 2},
                    camera="porch",
                    trigger="motion",
                    condition="c",
                    repeat=True,
                    expires_at=at(23),
                    last_digest=None,
                    last_met=False,
                ),
                status="done",
                next_run=None,
            ),
            dict(
                job("t-aaa005", "assistant", {"at": at(18, 30)}, request="r"),
                status="cancelled",
                next_run=None,
            ),
        ]

    def test_defaults_come_from_kind_and_effect_class_without_rewriting_the_file(self):
        jobs = self.legacy()
        agenda.JOBS_PATH.write_text(json.dumps(jobs, indent=1))
        self.assertEqual(
            [agenda.catch_up(j) for j in agenda._load()],
            [
                "skip",
                "run_once_late",
                "run_once_late",
                "run_once_late",
                "run_once_late",
            ],
        )
        for summary in agenda.list_jobs(include_finished=True):
            self.assertIn("schedule", summary)
        with patch.object(agenda, "_sync_motion"):
            self.tick(at(21, 0))
        after = {j["id"]: j for j in agenda._load()}
        self.assertEqual(set(after), {j["id"] for j in jobs})
        self.assertFalse(any("catch_up" in j or "on" in j for j in after.values()))
        self.assertEqual(
            after["t-aaa001"]["last_result"].split(":")[0], "missed"
        )  # the toggle: skipped
        self.assertEqual(
            after["t-aaa001"]["status"], "active"
        )  # recurring: on to tomorrow
        self.assertEqual(after["t-aaa002"]["status"], "done")  # the read: ran late
        self.assertEqual(after["t-aaa005"]["status"], "cancelled")


class TwoProcessTests(Case):
    def test_two_processes_ticking_at_once_run_a_due_job_once(self):
        marker = self.base / "ran.txt"
        job = agenda.create(
            {"title": "count", "kind": "remind", "in_minutes": 1}, now=time.time() - 120
        )
        script = textwrap.dedent(f"""
            import sys, time
            sys.path[:0] = [{SRC!r}, {TESTS!r}]
            import no_live_desktop
            from pathlib import Path
            from omarchy_ai.core import agenda, outbox, triggers
            base = Path(sys.argv[1])
            agenda.JOBS_PATH, agenda.INBOX_PATH = base / "agenda.json", base / "inbox.jsonl"
            triggers.STATE_PATH, outbox.LEDGER_PATH = base / "triggers.json", base / "outbox.json"
            outbox.send = lambda *a, **k: None
            agenda._refresh_routines_hud = lambda: None

            def run(job, now, jev):  # a slow run, so both processes overlap
                with open(base / "ran.txt", "a") as out:
                    out.write("x\\n")
                time.sleep(0.5)
                agenda._finish(job, now, "ok")
            agenda.RUNNERS["remind"] = run
            time.sleep(max(0.0, float(sys.argv[2]) - time.time()))
            print(agenda.tick(jev=object(), wait=True))
        """)
        start = time.time() + 1.5
        env = {
            **os.environ,
            "XDG_STATE_HOME": str(self.base),
            "XDG_CONFIG_HOME": str(self.base),
        }
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.base), str(start)],
                env=env,
                stdout=subprocess.PIPE,
                text=True,
            )
            for _ in range(2)
        ]
        outputs = [p.communicate(timeout=120)[0] for p in procs]
        self.assertEqual(sum(job["id"] in out for out in outputs), 1, outputs)
        self.assertEqual(marker.read_text(), "x\n")
        self.assertEqual(self.job(job["id"])["runs"], 1)


class InterruptedTests(Case):
    def test_reported_at_startup_with_each_steps_fate_from_the_journal(self):
        job = agenda.create(
            {
                "title": "evening",
                "kind": "action",
                "at": "18:30",
                "steps": [
                    {"tool": "volume_set", "args": {"percent": 20}},
                    {"tool": "battery_status", "args": {}},
                    {"tool": "volume_set", "args": {"percent": 5}},
                ],
            },
            now=at(17, 0),
        )
        started = at(18, 30)
        agenda._update(
            job["id"], started_at=started, next_run=None
        )  # its process died; no lease
        origin = f"agenda:{job['id']}:{started}"
        effects.perform(
            "agenda_step",
            "volume_set",
            {"percent": 20},
            lambda _k: ActionResult(True, "ok"),
            origin_id=origin + ":1",
        )
        self.tick(at(18, 31))  # the first heartbeat after a restart, a minute later
        entry = agenda._inbox_read()[0]
        self.assertEqual(entry["outcome"], "interrupted")
        self.assertIn("step 1 (volume_set): done", entry["detail"])
        self.assertIn("step 2 (battery_status): not started", entry["detail"])
        self.run_action.assert_not_called()


class _Clock:
    """Trigger checks at chosen times (the heartbeat's `now`)."""

    def __init__(self, case, job):
        self.case, self.job_id = case, job["id"]

    def check(self, now):
        agenda._update(self.job_id, next_run=now)  # due at this tick
        self.case.tick(now)
        return self.case.job(self.job_id)


class FileTriggerTests(Case):
    def setUp(self):
        super().setUp()
        self.path = self.base / "watched.txt"
        self.path.write_text("one")

    def make(self, **on):
        job = agenda.create(
            {
                "title": "file moved",
                "kind": "remind",
                "on": {"event": "file_changed", "path": str(self.path), **on},
            },
            now=at(12),
        )
        return job, _Clock(self, job)

    def touch(self, text):
        self.path.write_text(text)
        stamp = time.time() + len(text)  # every write a new mtime
        os.utime(self.path, (stamp, stamp))

    def test_its_first_state_is_the_baseline_then_a_change_fires_once(self):
        job, clock = self.make(debounce_minutes=0)
        clock.check(at(12))
        clock.check(at(12, 1))
        self.assertEqual(self.outcomes(), [])
        self.touch("two")
        clock.check(at(12, 2))
        clock.check(at(12, 3))
        self.assertEqual(self.outcomes(), ["reminded"])
        self.assertIn("changed", agenda._inbox_read()[0]["detail"])
        self.assertEqual(
            self.job(job["id"])["status"], "active"
        )  # repeats until cancelled

    def test_changes_close_together_are_debounced_into_one_firing(self):
        _, clock = self.make(debounce_minutes=3)
        clock.check(at(12))
        for minute, text in ((1, "a"), (2, "ab"), (3, "abc")):
            self.touch(text)
            clock.check(at(12, minute))
        clock.check(at(12, 5))
        self.assertEqual(self.outcomes(), [])  # still settling: last change at 12:03
        clock.check(at(12, 6))
        self.assertEqual(self.outcomes(), ["reminded"])
        clock.check(at(12, 10))
        self.assertEqual(self.outcomes(), ["reminded"])

    def test_quiet_hours_hold_an_event_until_they_end(self):
        _, clock = self.make(debounce_minutes=0, quiet_hours="22:00-07:00")
        clock.check(at(23))
        self.touch("night")
        clock.check(at(23, 30))
        clock.check(at(6, 59, day=31))
        self.assertEqual(self.outcomes(), [])
        clock.check(at(7, 0, day=31))
        self.assertEqual(self.outcomes(), ["reminded"])

    def test_quiet_hours_can_drop_events_instead(self):
        _, clock = self.make(
            debounce_minutes=0, quiet_hours="22:00-07:00", quiet="drop"
        )
        clock.check(at(23))
        self.touch("night")
        clock.check(at(23, 30))
        clock.check(at(7, 5, day=31))
        self.assertEqual(self.outcomes(), [])

    def test_bad_specs_fail_at_creation(self):
        for on, why in (
            ({"event": "nope"}, "on.event"),
            ({"event": "file_changed"}, "on.path"),
            ({"event": "email"}, "on.query"),
            ({"event": "device_online"}, "exactly one"),
            (
                {"event": "file_changed", "path": "x", "quiet_hours": "late"},
                "quiet_hours",
            ),
        ):
            with self.subTest(why=why), self.assertRaisesRegex(ValueError, why):
                agenda.create({"title": "t", "kind": "remind", "on": on}, now=at(12))
        with self.assertRaisesRegex(ValueError, "not both"):
            agenda.create(
                {
                    "title": "t",
                    "kind": "remind",
                    "at": "18:00",
                    "on": {"event": "file_changed", "path": "x"},
                },
                now=at(12),
            )
        self.assertEqual(agenda._load(), [])


class ProcessAndDeviceTriggerTests(Case):
    def test_process_exit_fires_once_and_ends_the_job(self):
        marker = f"omarchy-trigger-test-{os.getpid()}-{time.monotonic_ns()}"
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(120)", marker]
        )
        self.addCleanup(proc.kill)
        job = agenda.create(
            {
                "title": "done",
                "kind": "remind",
                "on": {"event": "process_exited", "process": marker},
            },
            now=at(12),
        )
        clock = _Clock(self, job)
        clock.check(at(12))
        self.assertEqual(self.outcomes(), [])
        proc.kill()
        proc.wait()
        after = clock.check(at(12, 1))
        self.assertEqual(self.outcomes(), ["reminded"])
        self.assertEqual(after["status"], "done")

    def test_device_coming_online_fires_on_the_transition(self):
        job = agenda.create(
            {
                "title": "tv",
                "kind": "remind",
                "on": {
                    "event": "device_online",
                    "host": "tv.local",
                    "debounce_minutes": 0,
                },
                "repeat": True,
            },
            now=at(12),
        )
        clock = _Clock(self, job)
        with patch.object(
            triggers, "_probe", side_effect=[False, False, True, True, False, True]
        ):
            for minute in range(6):
                clock.check(at(12, minute * 2))
        self.assertEqual(
            self.outcomes(), ["reminded", "reminded"]
        )  # offline->online twice


class MyApiTriggerTests(Case):
    def setUp(self):
        super().setUp()
        self.fake = FakeMyApi()
        self.addCleanup(self.fake.close)
        self.fake.patch().start()
        patch(
            "omarchy_ai.myapi.client._load_identity", side_effect=fake_identity
        ).start()
        patch("omarchy_ai.myapi.is_connected", return_value=True).start()
        patch.object(triggers, "MYAPI_MIN_GAP", 0.0).start()

    def inbox(self, *ids):
        self.fake.succeed(
            {
                "data": {
                    "messages": [
                        {
                            "messageId": i,
                            "sender": f"{i}@example.com",
                            "subject": f"invoice {i}",
                        }
                        for i in ids
                    ]
                }
            },
            match="/gmail/execute",
        )

    def test_an_email_trigger_fires_once_per_matching_message(self):
        job = agenda.create(
            {
                "title": "invoice",
                "kind": "remind",
                "on": {"event": "email", "query": "from:billing subject:invoice"},
            },
            now=at(12),
        )
        self.assertEqual(job["schedule"], {"every_minutes": triggers.POLL_FLOOR_MYAPI})
        clock = _Clock(self, job)
        self.inbox("m1")
        clock.check(at(12))  # m1 was already there: the baseline
        clock.check(at(12, 5))
        self.assertEqual(self.outcomes(), [])
        self.inbox("m2", "m1")
        clock.check(at(12, 10))
        clock.check(at(12, 15))
        self.inbox("m3", "m2", "m1")
        clock.check(at(12, 20))
        clock.check(at(12, 25))
        entries = agenda._inbox_read()
        self.assertEqual([e["outcome"] for e in entries], ["reminded", "reminded"])
        self.assertIn("invoice m2", entries[0]["detail"])
        self.assertIn("invoice m3", entries[1]["detail"])
        self.assertIn(
            "<untrusted", entries[0]["detail"]
        )  # an email's words are someone else's
        sent = [r for r in self.fake.requests if r.path.endswith("/execute")]
        self.assertEqual(len(sent), 6)
        self.assertEqual({r.body["method"] for r in sent}, {"GMAIL_FETCH_EMAILS"})
        self.assertEqual(
            {r.body["params"]["arguments"]["query"] for r in sent},
            {"from:billing subject:invoice"},
        )

    def test_mails_polled_faster_than_the_rate_limit_are_not_fetched(self):
        patch.object(triggers, "MYAPI_MIN_GAP", 3600.0).start()
        patch.object(triggers, "_myapi_last", [float("-inf")]).start()
        job = agenda.create(
            {
                "title": "x",
                "kind": "remind",
                "every_minutes": 1,
                "on": {"event": "email", "query": "q"},
            },
            now=at(12),
        )
        self.assertEqual(job["schedule"]["every_minutes"], triggers.POLL_FLOOR_MYAPI)
        self.inbox("m1")
        clock = _Clock(self, job)
        clock.check(at(12))
        clock.check(at(12, 5))
        self.assertEqual(
            len([r for r in self.fake.requests if r.path.endswith("/execute")]), 1
        )
        self.assertIn("rate-limited", self.job(job["id"])["last_result"])

    def test_a_calendar_event_starting_fires_once(self):
        start = datetime.fromtimestamp(at(12, 10)).astimezone().isoformat()
        self.fake.succeed(
            {
                "data": {
                    "items": [
                        {
                            "id": "ev1",
                            "summary": "standup",
                            "start": {"dateTime": start},
                        },
                        {
                            "id": "ev2",
                            "summary": "holiday",
                            "start": {"date": "2026-09-30"},
                        },
                    ]
                }
            },
            match="/googlecalendar/proxy",
        )
        job = agenda.create(
            {"title": "meeting", "kind": "remind", "on": {"event": "calendar"}},
            now=at(12),
        )
        clock = _Clock(self, job)
        clock.check(at(12))
        clock.check(at(12, 5))
        entries = agenda._inbox_read()
        self.assertEqual([e["outcome"] for e in entries], ["reminded"])
        self.assertIn("standup starts at 12:10", entries[0]["detail"])
        self.assertEqual(self.fake.writes, [])  # GET through the proxy: a read


if __name__ == "__main__":
    unittest.main()
