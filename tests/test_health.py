"""systemd notify/watchdog and crash-loop safe mode (core/health.py)."""

import asyncio
import os
from pathlib import Path
import secrets
import socket
import tempfile
import threading
import time
import unittest
from unittest import mock

from omarchy_ai.core import health


class NotifyTests(unittest.TestCase):
    def _receiver(self, address):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        sock.bind(address)
        sock.settimeout(2)
        self.addCleanup(sock.close)
        return sock

    def test_sends_to_a_path_socket(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "notify")
            sock = self._receiver(path)
            self.assertTrue(
                health.sd_notify("READY=1\nSTATUS=Running", {"NOTIFY_SOCKET": path})
            )
            self.assertEqual(sock.recv(4096), b"READY=1\nSTATUS=Running")

    def test_sends_to_an_abstract_socket(self):
        name = f"omarchy-ai-test-{secrets.token_hex(6)}"
        sock = self._receiver("\0" + name)
        self.assertTrue(health.sd_notify("WATCHDOG=1", {"NOTIFY_SOCKET": "@" + name}))
        self.assertEqual(sock.recv(4096), b"WATCHDOG=1")

    def test_no_op_outside_systemd(self):
        self.assertFalse(health.sd_notify("READY=1", {}))
        self.assertFalse(health.sd_notify("READY=1", {"NOTIFY_SOCKET": "vsock:2:1"}))

    def test_unreachable_socket_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"NOTIFY_SOCKET": os.path.join(tmp, "gone")}
            self.assertFalse(health.sd_notify("READY=1", env))

    def test_watchdog_seconds(self):
        self.assertEqual(health.watchdog_seconds({"WATCHDOG_USEC": "30000000"}), 30.0)
        mine = {"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": str(os.getpid())}
        self.assertEqual(health.watchdog_seconds(mine), 30.0)
        other = {"WATCHDOG_USEC": "30000000", "WATCHDOG_PID": str(os.getpid() + 1)}
        self.assertIsNone(health.watchdog_seconds(other))
        self.assertIsNone(health.watchdog_seconds({}))
        self.assertIsNone(health.watchdog_seconds({"WATCHDOG_USEC": "0"}))
        self.assertIsNone(health.start_watchdog([], env={}))


class WatchdogTests(unittest.TestCase):
    def test_pings_only_while_every_check_passes(self):
        sent = []
        state = {"problem": None}
        dog = health.Watchdog(
            [lambda: None, lambda: state["problem"]], interval=1, notify=sent.append
        )
        self.assertTrue(dog.beat())
        state["problem"] = "agenda heartbeat silent"
        self.assertFalse(dog.beat())
        self.assertEqual(dog.failing, "agenda heartbeat silent")
        state["problem"] = None
        self.assertTrue(dog.beat())
        self.assertEqual(sent, ["WATCHDOG=1", "WATCHDOG=1"])

    def test_a_raising_check_counts_as_failing(self):
        sent = []

        def broken():
            raise RuntimeError("boom")

        dog = health.Watchdog([broken], interval=1, notify=sent.append)
        self.assertFalse(dog.beat())
        self.assertEqual(sent, [])

    def test_heartbeat_stops_while_the_event_loop_is_blocked(self):
        loop = asyncio.new_event_loop()
        thread = threading.Thread(target=loop.run_forever, daemon=True)
        thread.start()
        self.addCleanup(loop.close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(loop.call_soon_threadsafe, loop.stop)

        pings = []
        lock = threading.Lock()

        def notify(message):
            with lock:
                pings.append(time.monotonic())
            return True

        dog = health.Watchdog(
            [health.loop_check(loop, limit=0.2)], interval=0.05, notify=notify
        )
        dog.start()
        self.addCleanup(dog.stop)
        time.sleep(0.3)
        with lock:
            self.assertTrue(pings, "no ping while the loop was responsive")

        release = threading.Event()
        loop.call_soon_threadsafe(release.wait, 5)  # a synchronous call blocks the loop
        time.sleep(0.3)  # the check in flight finishes failing
        with lock:
            before = len(pings)
        time.sleep(0.8)
        with lock:
            during = len(pings)
        self.assertEqual(during, before, "pinged while the event loop was blocked")
        self.assertIsNotNone(dog.failing)

        release.set()
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with lock:
                if len(pings) > during:
                    break
            time.sleep(0.05)
        with lock:
            self.assertGreater(len(pings), during, "no ping after the loop recovered")

    def test_closed_loop_fails(self):
        loop = asyncio.new_event_loop()
        loop.close()
        self.assertEqual(health.loop_check(loop)(), "event loop closed")


class RunnerCheckTests(unittest.TestCase):
    def test_responsive_runner(self):
        from omarchy_ai.execution.runner import ToolRunner

        self.assertIsNone(ToolRunner().responsive(timeout=2))

    def test_stuck_lock_is_reported(self):
        from omarchy_ai.execution.runner import ToolRunner

        runner = ToolRunner()
        held, release = threading.Event(), threading.Event()

        def hold():
            with runner._lock:
                held.set()
                release.wait(5)

        threading.Thread(target=hold, daemon=True).start()
        held.wait(2)
        try:
            self.assertEqual(runner.responsive(timeout=0.1), "tool runner lock held")
        finally:
            release.set()


class SafeModeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "health.json"

    def start(self, now):
        return health.on_start(now=now, pid=4242, target=self.path)

    def crash_loop(self, start, count, gap=30.0):
        """`count` starts that each end in a crash (no clean stop)."""
        result = None
        for n in range(count):
            result = self.start(start + n * gap)
        return result

    def test_first_start_is_normal(self):
        result = self.start(1000.0)
        self.assertFalse(result["safe_mode"])
        self.assertFalse(result["entered"])
        self.assertEqual(result["crashes"], 0)

    def test_three_crashes_in_five_minutes_enter_safe_mode(self):
        # Start 1 is clean; starts 2-4 each follow a crash.
        result = self.crash_loop(1000.0, 3)
        self.assertEqual(result["crashes"], 2)
        self.assertFalse(result["safe_mode"])
        result = self.start(1090.0)
        self.assertEqual(result["crashes"], 3)
        self.assertTrue(result["safe_mode"])
        self.assertTrue(result["entered"])
        self.assertEqual(result["since"], 1090.0)
        # Another crash in safe mode: still in it, not "entered" again.
        again = self.start(1120.0)
        self.assertTrue(again["safe_mode"])
        self.assertFalse(again["entered"])

    def test_crashes_outside_the_window_do_not_count(self):
        self.start(0.0)
        self.start(100.0)  # crash 1
        self.start(200.0)  # crash 2
        result = self.start(
            100.0 + health.CRASH_WINDOW + 1
        )  # crash 3; crash 1 has aged out
        self.assertEqual(result["crashes"], 2)
        self.assertFalse(result["safe_mode"])

    def test_a_clean_stop_is_not_a_crash(self):
        for n in range(6):
            result = self.start(1000.0 + n * 10)
            health.on_clean_stop(target=self.path)
        self.assertEqual(result["crashes"], 0)
        self.assertFalse(result["safe_mode"])

    def test_clean_stops_between_crashes(self):
        self.start(1000.0)
        self.start(1010.0)  # crash 1
        health.on_clean_stop(target=self.path)
        self.start(1020.0)  # a restart on purpose
        self.start(1030.0)  # crash 2
        result = self.start(1040.0)  # crash 3
        self.assertEqual(result["crashes"], 3)
        self.assertTrue(result["entered"])

    def test_stable_uptime_clears_safe_mode(self):
        self.crash_loop(1000.0, 4)
        self.assertTrue(health.status(self.path)["safe_mode"])
        self.assertTrue(health.mark_stable(target=self.path))
        status = health.status(self.path)
        self.assertFalse(status["safe_mode"])
        self.assertEqual(status["crashes"], 0)
        self.assertFalse(health.mark_stable(target=self.path))
        # A clean restart after that starts normally.
        health.on_clean_stop(target=self.path)
        self.assertFalse(self.start(2000.0)["safe_mode"])

    def test_safe_mode_persists_across_a_clean_restart_until_stable(self):
        self.crash_loop(1000.0, 4)
        health.on_clean_stop(target=self.path)
        result = self.start(1200.0)
        self.assertTrue(result["safe_mode"])
        self.assertFalse(result["entered"])

    def test_notification_is_urgent(self):
        with mock.patch("omarchy_ai.core.outbox._desktop") as notify:
            health.notify_safe_mode(3)
        title, body, urgent = notify.call_args.args
        self.assertEqual(title, health.SAFE_MODE_TITLE)
        self.assertIn("3 times", body)
        self.assertTrue(urgent)


class DaemonHeartbeatCheckTests(unittest.TestCase):
    def daemon(self, beat_age, enabled=True):
        from omarchy_ai.core.daemon import OmaDaemon

        d = OmaDaemon.__new__(OmaDaemon)
        d.config = mock.Mock(heartbeat_enabled=enabled, heartbeat_seconds=60)
        d._heart_beat_at = time.monotonic() - beat_age
        return d

    def test_recent_heartbeat_passes(self):
        self.assertIsNone(self.daemon(30)._heartbeat_check())

    def test_silent_heartbeat_fails(self):
        from omarchy_ai.core.daemon import OmaDaemon

        problem = self.daemon(60 + OmaDaemon.HEARTBEAT_GRACE + 5)._heartbeat_check()
        self.assertIn("agenda heartbeat silent", problem)

    def test_not_checked_when_disabled_or_in_safe_mode(self):
        self.assertIsNone(self.daemon(10_000, enabled=False)._heartbeat_check())
        with mock.patch.object(health, "active", True):
            self.assertIsNone(self.daemon(10_000)._heartbeat_check())


class UnitTemplateTests(unittest.TestCase):
    def sections(self):
        template = (
            Path(__file__).resolve().parents[1] / "systemd" / "omarchy-ai.service"
        )
        sections, current = {}, None
        for line in template.read_text().splitlines():
            line = line.strip()
            if line.startswith("[") and line.endswith("]"):
                current = sections.setdefault(line[1:-1], {})
            elif line and not line.startswith("#") and current is not None:
                key, _, value = line.partition("=")
                current.setdefault(key, []).append(value)
        return sections

    def test_notify_watchdog_and_restart_policy(self):
        sections = self.sections()
        unit, service = sections["Unit"], sections["Service"]
        self.assertEqual(unit["StartLimitIntervalSec"], ["120"])
        self.assertEqual(unit["StartLimitBurst"], ["5"])
        for key, value in {
            "Type": "notify",
            "NotifyAccess": "main",
            "WatchdogSec": "30",
            "Restart": "always",
            "RestartSec": "2",
        }.items():
            self.assertEqual(service[key], [value], key)
        self.assertIn("@VENV@/bin/python -m omarchy_ai", service["ExecStart"])
        self.assertEqual(service["WorkingDirectory"], ["@PROJECT_DIR@"])

    def test_no_option_that_breaks_sudo_or_bwrap(self):
        service = self.sections()["Service"]
        for key in (
            "NoNewPrivileges",
            "RestrictSUIDSGID",
            "LockPersonality",
            "PrivateTmp",
            "ProtectKernelTunables",
            "ProtectKernelModules",
            "RestrictNamespaces",
            "ProtectSystem",
        ):
            self.assertNotIn(key, service)


if __name__ == "__main__":
    unittest.main()
