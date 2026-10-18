"""Provider health survives a restart (core/provider_health.py).

A "restart" here is: write the file, wipe the owner's in-memory state, then
disable() + enable() as the daemon does at startup. Every test uses its own
temporary file, so nothing reaches the real STATE_DIR even without conftest.
"""

import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core import jev, provider_health, quota, statefile
from omarchy_ai.runtime import llm
from omarchy_ai.runtime.executors import coding_agents
from omarchy_ai.runtime.llm import WorkerModel, WorkerModelError
from omarchy_ai.voice import fallback

SRC = str(Path(__file__).resolve().parents[1] / "src")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="provider-health-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.file = self.dir / "provider_health.json"
        provider_health.disable()
        self.addCleanup(provider_health.disable)
        provider_health.enable(self.file)

    def restart(self):
        provider_health.flush()
        provider_health.disable()
        self.wipe()
        provider_health.enable(self.file)

    def wipe(self):
        pass

    def stored(self) -> dict:
        provider_health.flush()
        return json.loads(self.file.read_text())["entries"]


class WorkerBackendTests(Base):
    def setUp(self):
        llm._cooldown.clear()
        self.addCleanup(llm._cooldown.clear)
        super().setUp()
        patch(
            "omarchy_ai.config.load_config",
            return_value=SimpleNamespace(
                task_worker_backends=["claude", "codex", "api"], task_agent_model="auto"
            ),
        ).start()
        patch.object(llm, "_cli_ready", return_value=True).start()
        self.addCleanup(patch.stopall)

    def wipe(self):
        llm._cooldown.clear()

    def test_backend_failed_a_minute_ago_is_still_skipped_after_restart(self):
        wm = WorkerModel()
        wm._order = ["claude", "codex", "api"]
        wm._setup = lambda: None
        with patch.object(
            llm,
            "_cli_complete",
            side_effect=[WorkerModelError("claude timed out after 20s"), {"ok": True}],
        ):
            self.assertEqual(wm.complete("s", "u"), {"ok": True})
        entry = self.stored()["worker_backend:claude"]
        self.assertEqual((entry["state"], entry["kind"]), ("failed", "worker_backend"))
        self.assertAlmostEqual(
            entry["until"], time.time() + llm.FAILURE_COOLDOWN, delta=5
        )

        # It failed a minute ago; then the daemon restarts.
        data = json.loads(self.file.read_text())
        data["entries"]["worker_backend:claude"]["until"] -= 60
        self.file.write_text(json.dumps(data))
        self.restart()

        wm = WorkerModel()
        wm._order = ["claude", "codex", "api"]
        wm._setup = lambda: None
        self.assertEqual(wm.backends(), ["codex", "api"])
        calls = []
        with patch.object(
            llm,
            "_cli_complete",
            side_effect=lambda b, *a: calls.append(b) or {"ok": True},
        ):
            started = time.monotonic()
            self.assertEqual(wm.complete("s", "u"), {"ok": True})
        self.assertEqual(calls, ["codex"])  # claude is never asked: no timeout paid
        self.assertLess(time.monotonic() - started, 1)

    def test_quota_cooldown_persists_with_its_state(self):
        llm._bench("codex", llm.LIMIT_COOLDOWN, "quota", "usage limit reached")
        self.restart()
        self.assertGreater(llm._cooldown["codex"], time.time() + 3000)
        self.assertEqual(self.stored()["worker_backend:codex"]["state"], "quota")


class CodingAgentTests(Base):
    def setUp(self):
        coding_agents._EXHAUSTED.clear()
        self.addCleanup(coding_agents._EXHAUSTED.clear)
        super().setUp()

    def wipe(self):
        coding_agents._EXHAUSTED.clear()

    def test_usage_limit_survives_restart(self):
        until = coding_agents.exhausted_until(
            "claude", "usage limit reached|%d" % (time.time() + 7200)
        )
        self.restart()
        self.assertEqual(coding_agents._EXHAUSTED["claude"], until)


class QuotaTests(Base):
    def setUp(self):
        quota._last.clear()
        self.addCleanup(quota._last.clear)
        super().setUp()
        p = patch.object(quota, "_alert")
        p.start()
        self.addCleanup(p.stop)

    def wipe(self):
        quota._last.clear()

    def test_background_alert_is_not_repeated_after_restart(self):
        self.assertTrue(quota.report("openai", "credit_balance_exhausted"))
        entry = self.stored()["quota:openai"]
        self.assertEqual(entry["state"], "exhausted")
        self.assertIn("credit_balance_exhausted", entry["reason"])
        self.restart()
        self.assertFalse(quota.report("openai", "again"))  # within ALERT_EVERY
        self.assertTrue(
            quota.report("gemini", "quota")
        )  # another provider still alerts
        # The user's own action alerts again after the short debounce.
        quota._last["openai"] -= quota.USER_DEBOUNCE + 1
        self.assertTrue(quota.report("openai", "again", user_initiated=True))


class VoiceFallbackTests(Base):
    def setUp(self):
        fallback._down.clear()
        self.addCleanup(fallback._down.clear)
        super().setUp()

    def wipe(self):
        fallback._down.clear()

    def test_failed_voice_model_is_skipped_after_restart(self):
        fallback.mark_down("gemini-3.8-live", RuntimeError("1011 Internal error"))
        self.restart()
        self.assertTrue(fallback.is_down("gemini-3.8-live"))
        self.assertFalse(fallback.is_down("gemini-3.1-flash-live-preview"))
        entry = self.stored()["voice_model:gemini-3.8-live"]
        self.assertEqual(entry["state"], "down")
        self.assertIn("1011", entry["reason"])


class BreakerTests(Base):
    def setUp(self):
        self.addCleanup(setattr, jev, "BREAKER", jev.BREAKER)
        jev.BREAKER = jev._Breaker()
        super().setUp()

    def wipe(self):
        jev.BREAKER = jev._Breaker()

    def test_open_breaker_survives_restart_and_closes_on_success(self):
        for _ in range(jev._Breaker.THRESHOLD):
            jev.BREAKER.failure()
        self.restart()
        self.assertGreater(jev.BREAKER.open_for(), 20)
        jev.BREAKER.success()
        self.assertEqual(jev.BREAKER.open_for(), 0)
        provider_health.flush()
        self.assertNotIn("breaker:gateway", self.stored())

    def test_restored_breaker_reopens_on_a_failed_probe(self):
        provider_health.record("breaker", "gateway", "open", time.time() + 0.5)
        self.restart()
        self.assertGreater(jev.BREAKER.open_for(), 0)
        time.sleep(0.6)
        self.assertEqual(jev.BREAKER.open_for(), 0)
        jev.BREAKER.failure()  # the probe after the cooldown fails
        self.assertGreater(jev.BREAKER.open_for(), 20)


class FileTests(Base):
    def test_expired_entries_are_dropped_on_load_and_write(self):
        now = time.time()
        self.file.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": {
                        "voice_model:old": {
                            "provider": "old",
                            "kind": "voice_model",
                            "state": "down",
                            "until": now - 1,
                            "reason": "",
                            "since": now - 601,
                        },
                        "voice_model:new": {
                            "provider": "new",
                            "kind": "voice_model",
                            "state": "down",
                            "until": now + 300,
                            "reason": "x",
                            "since": now - 300,
                        },
                    },
                }
            )
        )
        fallback._down.clear()
        self.addCleanup(fallback._down.clear)
        provider_health.disable()
        provider_health.enable(self.file)
        self.assertTrue(fallback.is_down("new"))
        self.assertFalse(fallback.is_down("old"))
        self.assertEqual(
            [r["provider"] for r in provider_health.snapshot(self.file)], ["new"]
        )
        provider_health.record("breaker", "gateway", "open", now + 30)
        provider_health.flush()
        self.assertEqual(sorted(self.stored()), ["breaker:gateway", "voice_model:new"])

    def test_corrupt_file_is_quarantined_and_replaced(self):
        self.file.write_text("{not json")
        provider_health.disable()
        provider_health.enable(self.file)  # does not raise
        self.assertEqual(
            [p.name.split(".corrupt-")[0] for p in statefile.corrupt_files(self.dir)],
            ["provider_health.json"],
        )
        self.assertEqual(provider_health.snapshot(self.file), [])
        provider_health.record("voice_model", "m", "down", time.time() + 60)
        provider_health.flush()
        self.assertIn("voice_model:m", self.stored())

    def test_newer_schema_is_left_alone(self):
        self.file.write_text(json.dumps({"schema_version": 99, "entries": {}}))
        provider_health.disable()
        provider_health.enable(self.file)
        self.assertEqual(provider_health.snapshot(self.file), [])
        provider_health.record("voice_model", "m", "down", time.time() + 60)
        provider_health.flush()
        self.assertEqual(json.loads(self.file.read_text())["schema_version"], 99)

    def test_snapshot_shape_and_order(self):
        now = time.time()
        provider_health.record(
            "worker_backend", "claude", "quota", now + 3600, "usage limit"
        )
        provider_health.record("voice_model", "m", "down", now + 60, "1011")
        pending = provider_health.snapshot()  # before the write: this process's changes
        provider_health.flush()
        rows = provider_health.snapshot(self.file)
        self.assertEqual(rows, pending)
        self.assertEqual([r["provider"] for r in rows], ["m", "claude"])
        self.assertEqual(set(rows[0]), {"provider", "kind", "state", "until", "reason"})
        self.assertEqual(rows[1]["reason"], "usage limit")

    def test_writes_are_coalesced_and_never_block_the_caller(self):
        provider_health.COALESCE_SECONDS, old = 0.2, provider_health.COALESCE_SECONDS
        self.addCleanup(setattr, provider_health, "COALESCE_SECONDS", old)
        holding, release = threading.Event(), threading.Event()

        def hold():
            with statefile.locked(self.file):
                holding.set()
                release.wait(5)

        holder = threading.Thread(target=hold)
        holder.start()
        holding.wait(5)
        writes = []
        real = statefile.atomic_write_text
        with patch.object(
            statefile,
            "atomic_write_text",
            side_effect=lambda p, t: writes.append(p) or real(p, t),
        ):
            started = time.monotonic()
            for n in range(50):
                provider_health.record(
                    "voice_model", f"m{n % 5}", "down", time.time() + 60
                )
            self.assertLess(
                time.monotonic() - started, 0.1
            )  # the file lock is held elsewhere
            release.set()
            holder.join()
            deadline = time.monotonic() + 5
            while not self.file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            time.sleep(0.1)
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(self.stored()), 5)

    def test_disabled_reads_and_writes_nothing(self):
        provider_health.disable()
        fallback._down.clear()
        self.addCleanup(fallback._down.clear)
        fallback.mark_down("m", RuntimeError("1011"))
        provider_health.flush()
        self.assertFalse(self.file.exists())
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_clear_keeps_an_entry_another_process_recorded_later(self):
        provider_health.record("breaker", "gateway", "open", time.time() + 30)
        provider_health.flush()
        provider_health.clear("breaker", "gateway")
        data = json.loads(self.file.read_text())
        data["entries"]["breaker:gateway"]["since"] = (
            time.time() + 1
        )  # the other one, after our clear
        self.file.write_text(json.dumps(data))
        provider_health.flush()
        self.assertIn("breaker:gateway", self.stored())


WRITER = """
import sys, time
from pathlib import Path
from omarchy_ai.core import provider_health
provider_health.enable(Path(sys.argv[1]))
for n in range(int(sys.argv[3])):
    provider_health.record("voice_model", sys.argv[2] + str(n % 7), "down", time.time() + 600, str(n))
    if n % 3 == 0:
        provider_health.flush()
provider_health.flush()
"""


class CrossProcessTests(Base):
    def test_concurrent_processes_keep_each_others_entries(self):
        provider_health.disable()
        env = dict(
            os.environ, PYTHONPATH=SRC + os.pathsep + os.environ.get("PYTHONPATH", "")
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", WRITER, str(self.file), f"p{i}-", "60"], env=env
            )
            for i in range(4)
        ]
        for proc in procs:
            self.assertEqual(proc.wait(60), 0)
        entries = self.stored()
        self.assertEqual(len(entries), 4 * 7)
        self.assertEqual(statefile.corrupt_files(self.dir), [])
        self.assertEqual(len(provider_health.snapshot(self.file)), 28)


if __name__ == "__main__":
    unittest.main()
