"""Agenda runs held by a lease in agenda.json, not a set in one process's
memory: the daemon and a CLI process never run the same job twice at once,
and a run whose process died is reported, never re-run (core/agenda.py)."""

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.core import agenda, statefile

SRC = str(Path(__file__).resolve().parents[1] / "src")
TESTS = str(Path(__file__).resolve().parent)

# The child processes save jobs too, and saving redacts with the keyring's
# passwords: they load the test guard (tests/no_live_desktop.py) as well.
PRELUDE = textwrap.dedent(f"""
    import sys, time
    sys.path[:0] = [{SRC!r}, {TESTS!r}]
    import no_live_desktop
    from pathlib import Path
    from omarchy_ai.core import agenda
    base = Path(sys.argv[1])
    agenda.JOBS_PATH, agenda.INBOX_PATH = base / "agenda.json", base / "inbox.jsonl"
""")


def _env(root: Path) -> dict:
    return {**os.environ, "XDG_STATE_HOME": str(root), "XDG_CONFIG_HOME": str(root)}


def _job(job_id="t-abc123", **extra):
    job = {
        "id": job_id,
        "title": "turn on the fan",
        "kind": "action",
        "created_at": 1.0,
        "status": "active",
        "runs": 0,
        "schedule": {"every_minutes": 60},
        "next_run": time.time() - 5,
        "steps": [],
        "request": "turn on the fan",
    }
    job.update(extra)
    return job


class AgendaLeaseTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        patch.object(agenda, "JOBS_PATH", self.base / "agenda.json").start()
        patch.object(agenda, "INBOX_PATH", self.base / "inbox.jsonl").start()
        self.notify = patch.object(agenda, "notify").start()
        patch.object(agenda, "_refresh_routines_hud").start()
        self.addCleanup(patch.stopall)

    def write(self, *jobs):
        agenda.JOBS_PATH.write_text(json.dumps(list(jobs), indent=1))

    def on_disk(self, job_id="t-abc123"):
        return next(
            j for j in json.loads(agenda.JOBS_PATH.read_text()) if j["id"] == job_id
        )

    def other_process(self):
        """A live process that is not this one (it holds a lease in a test)."""
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(proc.wait)
        self.addCleanup(proc.kill)
        for _ in range(100):
            from omarchy_ai.runtime.task import _proc_start

            start = _proc_start(proc.pid)
            if start:
                return proc, {"pid": proc.pid, "start_time": start}
            time.sleep(0.01)
        self.fail("no start time for the helper process")

    def test_a_legacy_agenda_without_leases_loads_and_its_due_job_runs(self):
        self.write(_job())
        self.assertEqual([j["id"] for j in agenda.due(time.time())], ["t-abc123"])
        claimed = agenda._claim("t-abc123", due_by=time.time())
        self.assertEqual(claimed["running_by"]["pid"], os.getpid())
        agenda._release("t-abc123")
        self.assertNotIn("running_by", self.on_disk())
        self.assertNotIn("running_by", agenda.summary(self.on_disk()))

    def test_a_job_another_live_process_holds_is_not_due_and_cannot_be_claimed(self):
        _, owner = self.other_process()
        self.write(_job(running_by={**owner, "lease_until": time.time() + 600}))
        self.assertEqual(agenda.due(time.time()), [])
        self.assertIsNone(agenda._claim("t-abc123"))
        self.assertIsNone(agenda.seconds_until_next())
        with patch.object(agenda, "_pool") as pool:
            self.assertEqual(agenda.tick(), [])
        pool.submit.assert_not_called()
        agenda._release("t-abc123")  # not this process's lease: left alone
        self.assertEqual(self.on_disk()["running_by"]["pid"], owner["pid"])

    def test_a_lease_whose_process_is_gone_does_not_block_the_job(self):
        proc, owner = self.other_process()
        proc.kill()
        proc.wait()
        self.write(_job(running_by={**owner, "lease_until": time.time() + 600}))
        self.assertEqual([j["id"] for j in agenda.due(time.time())], ["t-abc123"])

    def test_a_lease_held_past_its_time_by_another_live_process_lapses(self):
        _, owner = self.other_process()
        self.write(_job(running_by={**owner, "lease_until": time.time() - 1}))
        self.assertIsNotNone(agenda._claim("t-abc123"))

    def test_a_job_another_process_already_ran_is_not_claimed_from_a_stale_list(self):
        self.write(_job())
        listed = agenda.due(time.time())
        self.write(
            _job(next_run=time.time() + 3600, runs=1)
        )  # the other process ran it meanwhile
        self.assertEqual(len(listed), 1)
        self.assertIsNone(agenda._claim(listed[0]["id"], due_by=time.time()))

    def test_run_now_does_not_start_a_second_run_of_a_held_job(self):
        _, owner = self.other_process()
        self.write(_job(running_by={**owner, "lease_until": time.time() + 600}))
        with patch.object(agenda, "run_job") as run:
            self.assertEqual(agenda.run_now("t-abc123")["id"], "t-abc123")
        run.assert_not_called()

    def test_a_run_ends_its_lease_even_when_it_crashes(self):
        self.write(_job())
        job = agenda._claim("t-abc123")

        def crash(*_):
            raise RuntimeError("boom")

        with (
            patch.dict(agenda.RUNNERS, {"action": crash}),
            self.assertLogs("omarchy_ai.core.agenda", "ERROR"),
        ):
            agenda.run_job(job, jev=object(), now=time.time())
        self.assertNotIn("running_by", self.on_disk())
        self.assertIn("crashed", self.on_disk()["last_result"])

    def test_an_interrupted_run_is_reported_once_and_not_while_its_process_lives(self):
        started = time.time() - agenda.STALE_RUN_SECONDS - 60
        _, owner = self.other_process()
        self.write(
            _job(
                started_at=started,
                next_run=time.time() + 3600,
                running_by={**owner, "lease_until": time.time() + 600},
            )
        )
        agenda._report_interrupted(time.time())
        self.notify.assert_not_called()  # still running elsewhere
        self.write(_job(started_at=started, next_run=time.time() + 3600))
        agenda._report_interrupted(time.time())
        agenda._report_interrupted(time.time())
        self.assertEqual(self.notify.call_count, 1)
        self.assertEqual(self.on_disk()["last_result"], "interrupted")
        self.assertIsNone(self.on_disk()["started_at"])
        self.assertEqual([e["outcome"] for e in agenda._inbox_read()], ["interrupted"])

    def test_two_processes_claiming_at_once_never_both_win(self):
        self.write(*[_job(f"t-job{n:03d}") for n in range(50)])
        script = PRELUDE + textwrap.dedent("""
            won = [f"t-job{n:03d}" for n in range(50) if agenda._claim(f"t-job{n:03d}", due_by=time.time())]
            print(" ".join(won))
            time.sleep(1)  # still alive while the other one tries
        """)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.base)],
                stdout=subprocess.PIPE,
                text=True,
                env=_env(self.base),
            )
            for _ in range(2)
        ]
        won = [p.communicate(timeout=120)[0].split() for p in procs]
        self.assertEqual(sorted(won[0] + won[1]), [f"t-job{n:03d}" for n in range(50)])
        self.assertEqual(set(won[0]) & set(won[1]), set())

    def test_two_processes_updating_jobs_1000_times_lose_no_update(self):
        self.write(_job())
        script = PRELUDE + textwrap.dedent("""
            for i in range(500):
                with agenda._jobs_locked():
                    jobs = agenda._load()
                    jobs[0]["runs"] += 1
                    jobs[0].setdefault("seen", []).append(sys.argv[2] + str(i))
                    agenda._save(jobs)
        """)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.base), name],
                env=_env(self.base),
            )
            for name in "ab"
        ]
        for p in procs:
            self.assertEqual(p.wait(timeout=180), 0)
        job = self.on_disk()
        self.assertEqual(job["runs"], 1000)
        self.assertEqual(len(set(job["seen"])), 1000)

    def test_two_processes_queueing_the_same_task_result_queue_it_once(self):
        script = PRELUDE + textwrap.dedent("""
            for i in range(30):
                agenda.task_result(f"task{i}", "fix the build", "certified", "Done.")
        """)
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(self.base)], env=_env(self.base)
            )
            for _ in range(2)
        ]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        self.assertEqual(
            sorted(e["task_id"] for e in agenda._inbox_read()),
            sorted(f"task{i}" for i in range(30)),
        )

    def test_a_corrupt_agenda_is_quarantined_not_replaced_by_the_next_job(self):
        agenda.JOBS_PATH.write_text('[{"id": "t-abc123", "title": "tur')
        with self.assertLogs("omarchy_ai.core.statefile", "WARNING"):
            self.assertEqual(agenda._load(), [])
        kept = statefile.corrupt_files(self.base)
        self.assertEqual(
            [p.read_text() for p in kept], ['[{"id": "t-abc123", "title": "tur']
        )

    def test_inbox_lines_that_do_not_parse_survive_marking_others_delivered(self):
        agenda.INBOX_PATH.write_text(
            json.dumps({"id": "e1", "at": time.time(), "delivered": False})
            + "\n{torn\n"
        )
        with self.assertLogs("omarchy_ai.core.statefile", "WARNING"):
            agenda.mark_delivered({"e1"})
        self.assertTrue(agenda._inbox_read()[0]["delivered"])
        self.assertEqual(statefile.corrupt_files(self.base)[0].read_text(), "{torn\n")


class ConversationFileTests(unittest.TestCase):
    def setUp(self):
        from omarchy_ai.core import conversations

        self.conversations = conversations
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.dir = Path(temp.name) / "conversations"
        patch.object(conversations, "DIR", self.dir).start()
        self.addCleanup(patch.stopall)
        conversations._cache.clear()
        self.addCleanup(conversations._cache.clear)

    def test_an_existing_conversation_file_loads_and_keeps_its_shape(self):
        self.dir.mkdir()
        cid = "20261001-120000-abcdef"
        legacy = {
            "id": cid,
            "title": "",
            "source": "phone",
            "created": 1.0,
            "updated": 1.0,
            "lines": [],
            "titled_at": 0,
        }
        (self.dir / f"{cid}.json").write_text(json.dumps(legacy, ensure_ascii=False))
        self.conversations.add_card(cid, {"kind": "note", "text": "hi"})
        saved = json.loads((self.dir / f"{cid}.json").read_text())
        self.assertEqual(set(saved), set(legacy))
        self.assertEqual(saved["lines"][0]["text"], "hi")

    def test_a_corrupt_conversation_is_quarantined_not_hidden(self):
        self.dir.mkdir()
        cid = "20261001-120000-abcdef"
        (self.dir / f"{cid}.json").write_text('{"id": "20261001')
        with self.assertLogs("omarchy_ai.core.statefile", "WARNING"):
            self.assertFalse(self.conversations.exists(cid))
        self.assertEqual(
            statefile.corrupt_files(self.dir)[0].name.split(".corrupt-")[0],
            f"{cid}.json",
        )
        self.assertEqual(
            [p.name for p in self.dir.parent.iterdir() if p.name.endswith(".lock")],
            ["conversations.lock"],
        )


if __name__ == "__main__":
    unittest.main()
