"""Structured logs and correlation IDs (core/logs.py), DoD-8: every tool
result is one JSONL record carrying its tool_call_id and session_id, the IDs
reach worker processes, every sink is redacted, the file rolls over, and the
journal lines read exactly as before."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from unittest.mock import AsyncMock, patch

import no_live_desktop  # noqa: F401 -- never a real desktop command
from fakes.effects_journal import isolate_journal
from google.genai import types as genai_types
from omarchy_ai.config import Config
from omarchy_ai.core import agenda, effects, logs
from omarchy_ai.core.daemon import LOG_FORMAT
from omarchy_ai.execution import passwords, runner, tools, user_tools
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.runtime import sandbox, shell
from omarchy_ai.runtime.permissions import Risk
from omarchy_ai.voice.gemini_live import GeminiLiveSession
from test_sandbox import FakeHome, needs_bwrap

setUpModule, tearDownModule = isolate_journal()

TESTS = Path(__file__).resolve().parent
ASCTIME = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} "


class LogCase(unittest.TestCase):
    """The sinks installed into a temp dir; removed again afterwards."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="omarchy-logs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        root = logging.getLogger()
        level, handlers = root.level, list(root.handlers)
        for handler in handlers:  # pytest's capture handler would see these too
            root.removeHandler(handler)

        def restore():
            logs.shutdown()
            root.setLevel(level)
            for handler in handlers:
                root.addHandler(handler)

        self.addCleanup(restore)
        self.canary = "cn-" + secrets.token_hex(8)  # a fresh "saved password"
        secret = mock.patch.object(passwords, "known_secrets", lambda: [self.canary])
        secret.start()
        self.addCleanup(secret.stop)
        self.stream = io.StringIO()
        self.path = self.tmp / "logs" / logs.FILE_NAME
        logs.setup(
            logging.INFO, fmt=LOG_FORMAT, log_dir=self.tmp / "logs", stream=self.stream
        )

    def records(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(line) for line in self.path.read_text().splitlines()]

    def tool_results(self) -> list[dict]:
        return [r for r in self.records() if r.get("event") == "tool_result"]


class ToolResultRecords(LogCase):
    def test_every_tool_result_carries_its_tool_call_and_session(self):
        release = threading.Event()
        self.addCleanup(release.set)

        def execute(name, args):
            logging.getLogger("tests.tool").info("running %s", args["how"])
            if args["how"] == "raise":
                raise RuntimeError("broken")
            if args["how"] == "hang":
                release.wait(5)
            return ActionResult(args["how"] == "ok", args["how"])

        tool_runner = runner.ToolRunner()
        with logs.bind(session_id="sess-1", turn_id="turn-1"):
            for how in ("ok", "fail", "raise", "hang"):
                tool_runner.run(
                    "list_windows",
                    {"how": how},
                    execute=execute,
                    tag=f"call-{how}" if how != "fail" else None,
                    deadline=0.3 if how == "hang" else None,
                )
        results = self.tool_results()
        self.assertEqual(
            [r["outcome"] for r in results], ["ok", "failed", "crashed", "timeout"]
        )
        for record in results:
            self.assertEqual(record["session_id"], "sess-1")
            self.assertEqual(record["turn_id"], "turn-1")
            self.assertTrue(record["tool_call_id"])
            self.assertEqual(record["tool"], "list_windows")
            self.assertIsInstance(record["duration_ms"], float)
            self.assertEqual(record["logger"], "omarchy_ai.metrics")
        self.assertEqual(results[0]["tool_call_id"], "call-ok")
        self.assertEqual(results[0]["error_class"], None)
        self.assertEqual(results[2]["error_class"], "crashed")
        self.assertGreaterEqual(results[3]["duration_ms"], 300)
        # The tool's own lines, on the runner's thread, carry the same IDs.
        inside = [r for r in self.records() if r["logger"] == "tests.tool"]
        self.assertEqual(len(inside), 4)
        self.assertEqual(inside[0]["tool_call_id"], "call-ok")
        self.assertEqual({r["session_id"] for r in inside}, {"sess-1"})
        # No metrics line in the journal.
        self.assertNotIn("tool result:", self.stream.getvalue())

    def test_a_gemini_call_is_logged_under_its_session_turn_and_call_id(self):
        adapter = GeminiLiveSession(Config())
        adapter._turn_id = "turn-g"
        session = SimpleNamespace(send_tool_response=AsyncMock())
        call = genai_types.FunctionCall(id="gem-call-1", name="list_windows", args={})
        with (
            patch(
                "omarchy_ai.voice.gemini_live.run_action",
                side_effect=lambda *a: ActionResult(True, "[]"),
            ),
            patch(
                "omarchy_ai.voice.gemini_live.LiveSession._current_window",
                return_value={},
            ),
        ):
            asyncio.run(adapter._run_call(session, call))
        results = [r for r in self.tool_results() if r["tool"] == "list_windows"]
        self.assertTrue(results)
        for record in results:
            self.assertEqual(record["session_id"], adapter._audit_session)
            self.assertEqual(record["turn_id"], "turn-g")
            self.assertEqual(record["tool_call_id"], "gem-call-1")
        started = [r for r in self.records() if "Gemini action started" in r["msg"]]
        self.assertEqual(started[0]["tool_call_id"], "gem-call-1")
        self.assertEqual(logs.current(), {})  # nothing stays bound after the call


class WorkerRecords(LogCase):
    def setUp(self):
        super().setUp()
        paths = [str(TESTS), os.environ.get("PYTHONPATH", "")]
        env = mock.patch.dict(
            os.environ,
            {
                "PYTHONPATH": os.pathsep.join(p for p in paths if p),
                "OMARCHY_TEST_CANARY": self.canary,
            },
        )
        env.start()
        self.addCleanup(env.stop)
        names = mock.patch.object(tools, "WORKER_TOOLS", frozenset({"log_env"}))
        names.start()
        self.addCleanup(names.stop)
        self.pool = runner.WorkerPool(
            [
                sys.executable,
                "-m",
                "omarchy_ai.execution.worker_main",
                "--actions",
                "fakes.log_worker_actions:ACTIONS",
            ]
        )
        self.addCleanup(self.pool.close)

    def test_ids_reach_the_worker_its_environment_and_its_redacted_log_lines(self):
        tool_runner = runner.ToolRunner(self.pool)
        with logs.bind(session_id="sess-w", task_id="task-w"):
            result = tool_runner.run(
                "log_env", {"say": f"password {self.canary}"}, deadline=20, tag="call-w"
            )
            again = tool_runner.run("log_env", {"say": "second"}, deadline=20)
        self.assertTrue(result.ok, result.message)
        reply = json.loads(result.message)
        self.assertNotEqual(reply["pid"], os.getpid())
        self.assertEqual(
            reply["env"],
            {
                "OMARCHY_LOG_SESSION_ID": "sess-w",
                "OMARCHY_LOG_TASK_ID": "task-w",
                "OMARCHY_LOG_TOOL_CALL_ID": "call-w",
            },
        )
        # The second call's environment is its own, not the first one's.
        second = json.loads(again.message)["env"]
        self.assertNotEqual(second["OMARCHY_LOG_TOOL_CALL_ID"], "call-w")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            lines = [r for r in self.records() if r["logger"] == "tests.log_worker"]
            if len(lines) >= 2:
                break
            time.sleep(0.05)
        self.assertEqual(len(lines), 2, self.records())
        first = lines[0]
        self.assertEqual(first["pid"], reply["pid"])
        self.assertEqual(
            (first["session_id"], first["task_id"], first["tool_call_id"]),
            ("sess-w", "task-w", "call-w"),
        )
        self.assertEqual(first["msg"], f"worker says password {passwords.MASK}")
        self.assertNotIn(self.canary, self.path.read_text())
        results = self.tool_results()
        self.assertEqual([r["worker"] for r in results], [True, True])


class Redaction(LogCase):
    def test_the_jsonl_sink_and_stderr_are_redacted_tracebacks_too(self):
        log = logging.getLogger("tests.redact")
        log.info("typed the password %s", self.canary)
        try:
            raise ValueError(f"login failed for {self.canary}")
        except ValueError:
            log.exception("could not log in")
        text = self.path.read_text()
        self.assertNotIn(self.canary, text)
        self.assertNotIn(self.canary, self.stream.getvalue())
        records = self.records()
        self.assertEqual(records[0]["msg"], f"typed the password {passwords.MASK}")
        self.assertIn(f"login failed for {passwords.MASK}", records[1]["exc"])
        self.assertIn(f"login failed for {passwords.MASK}", self.stream.getvalue())

    def test_a_worker_process_redacts_every_sink(self):
        handlers = logs.setup(
            "INFO",
            fmt="%(message)s",
            jsonl_path=self.path,
            rotate=False,
            stream=self.stream,
        )
        self.assertEqual(len(handlers), 2)
        for handler in handlers:
            self.assertTrue(
                any(isinstance(f, passwords.RedactingFilter) for f in handler.filters)
            )

    def test_a_record_is_masked_once_whatever_the_number_of_sinks(self):
        calls = []

        def counting():
            calls.append(1)
            return [self.canary]

        with mock.patch.object(passwords, "known_secrets", counting):
            logging.getLogger("tests.redact").info("x %s", self.canary)
        self.assertEqual(len(calls), 1)


class Rotation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="omarchy-logs-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / logs.FILE_NAME

    def emit(self, handler, n, text="x" * 80):
        logger = logging.getLogger(f"tests.rotation.{id(handler)}")
        logger.propagate = False
        logger.setLevel(logging.INFO)
        logger.addHandler(handler)
        self.addCleanup(logger.removeHandler, handler)
        for i in range(n):
            logger.info("%d %s", i, text)

    def lines(self) -> list[dict]:
        found = []
        for path in [*sorted(self.tmp.glob("omarchy-ai.*.jsonl")), self.path]:
            if path.exists():
                found += [json.loads(line) for line in path.read_text().splitlines()]
        return found

    def test_it_rolls_over_at_the_size_limit_and_loses_nothing(self):
        handler = logs.JsonlHandler(self.path, max_bytes=2000)
        self.addCleanup(handler.close)
        self.emit(handler, 60)
        rolled = handler.rolled()
        self.assertGreater(len(rolled), 3)
        for path in [*rolled, self.path]:
            self.assertLessEqual(path.stat().st_size, 2000)
        self.assertEqual(
            sorted(int(r["msg"].split()[0]) for r in self.lines()), list(range(60))
        )
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_rolled_files_older_than_fourteen_days_are_deleted(self):
        now = time.time()
        old = self.tmp / "omarchy-ai.20200101T000000.jsonl"
        recent = self.tmp / "omarchy-ai.20200102T000000.jsonl"
        for path, age in ((old, 15), (recent, 13)):
            path.write_text("{}\n")
            os.utime(path, (now - age * 86400, now - age * 86400))
        handler = logs.JsonlHandler(self.path)
        self.addCleanup(handler.close)
        self.assertFalse(old.exists())
        self.assertTrue(recent.exists())

    def test_at_most_max_rolled_files_are_kept(self):
        handler = logs.JsonlHandler(self.path, max_bytes=500, max_rolled=3)
        self.addCleanup(handler.close)
        self.emit(handler, 40)
        self.assertEqual(len(handler.rolled()), 3)

    def test_a_new_day_starts_a_new_file(self):
        handler = logs.JsonlHandler(self.path)
        self.addCleanup(handler.close)
        self.emit(handler, 1)
        handler._day_ends = time.time() - 1  # midnight has passed
        self.emit(handler, 1)
        self.assertEqual(len(handler.rolled()), 1)
        self.assertEqual(len(self.path.read_text().splitlines()), 1)

    def test_a_file_left_from_an_earlier_day_is_rolled_at_start(self):
        self.path.write_text('{"msg": "yesterday"}\n')
        then = time.time() - 2 * 86400
        os.utime(self.path, (then, then))
        handler = logs.JsonlHandler(self.path)
        self.addCleanup(handler.close)
        self.assertEqual(len(handler.rolled()), 1)
        self.assertEqual(self.path.stat().st_size, 0)

    def test_an_appending_process_follows_the_owner_to_the_new_file(self):
        owner = logs.JsonlHandler(self.path, max_bytes=300)
        appender = logs.JsonlHandler(self.path, rotate=False)
        self.addCleanup(owner.close)
        self.addCleanup(appender.close)
        self.emit(appender, 1, "before")
        self.emit(owner, 5)  # rolls the file over
        appender._checked = 0.0  # it looks again at most once a second
        self.emit(appender, 1, "after")
        current = self.path.read_text()
        self.assertIn("after", current)
        self.assertNotIn("before", current)
        self.assertEqual(len([r for r in self.lines() if "after" in r["msg"]]), 1)


class JournalLines(LogCase):
    """What people and scripts grep for in the journal reads as before."""

    SAMPLES = [
        (
            "omarchy_ai.voice.gemini",
            logging.INFO,
            "Gemini action started: session=%s call=%s name=%s args=%r",
            ("5f2c", "c-7", "set_volume", {"level": 40}),
            "omarchy_ai.voice.gemini INFO Gemini action started: session=5f2c call=c-7 "
            "name=set_volume args={'level': 40}",
        ),
        (
            "omarchy_ai.voice.gemini",
            logging.INFO,
            "Switchboard: call=%s name=%s decision=%s%s evidence=%s (%.0fms)",
            ("c-7", "set_volume", "run", "", "asked", 12.4),
            "omarchy_ai.voice.gemini INFO Switchboard: call=c-7 name=set_volume decision=run "
            "evidence=asked (12ms)",
        ),
        (
            "omarchy_ai.phone.presence",
            logging.INFO,
            "phone presence: phone connected; Stay Awake on (idle lock held off)",
            (),
            "omarchy_ai.phone.presence INFO phone presence: phone connected; Stay Awake on "
            "(idle lock held off)",
        ),
        (
            "omarchy_ai.core.agenda",
            logging.WARNING,
            "agenda: %s step %d/%d %s -> %s%s",
            ("t-abc", 1, 2, "set_volume", "FAILED: no", ""),
            "omarchy_ai.core.agenda WARNING agenda: t-abc step 1/2 set_volume -> FAILED: no",
        ),
    ]

    def test_the_daemon_format_is_unchanged(self):
        self.assertEqual(LOG_FORMAT, "%(asctime)s %(name)s %(levelname)s %(message)s")

    def test_sample_lines_are_byte_identical_with_ids_bound(self):
        with logs.bind(session_id="s", turn_id="t", tool_call_id="c", task_id="k"):
            for name, level, fmt, args, _ in self.SAMPLES:
                logging.getLogger(name).log(level, fmt, *args)
        lines = self.stream.getvalue().splitlines()
        self.assertEqual(len(lines), len(self.SAMPLES))
        for line, (*_, expected) in zip(lines, self.SAMPLES):
            self.assertRegex(line, "^" + ASCTIME + re.escape(expected) + "$")
        # The same records, with their IDs, in the JSONL file.
        self.assertEqual(
            [r["session_id"] for r in self.records()], ["s"] * len(self.SAMPLES)
        )

    def test_without_python_systemd_the_journal_gets_the_stderr_line(self):
        with self.journal_stream() as stderr:
            sys.modules.pop("systemd.journal", None)
            handlers = logs.setup(logging.INFO, fmt=LOG_FORMAT)
            self.assertIsInstance(handlers[0], logging.StreamHandler)
            self.assertIs(handlers[0].stream, stderr)

    def test_with_python_systemd_the_ids_are_journal_fields(self):
        sent = []
        journal = types.ModuleType("systemd.journal")
        journal.send = lambda message, **fields: sent.append((message, fields))  # type: ignore[attr-defined]
        package = types.ModuleType("systemd")
        package.journal = journal  # type: ignore[attr-defined]
        with (
            self.journal_stream(),
            mock.patch.dict(
                sys.modules, {"systemd": package, "systemd.journal": journal}
            ),
        ):
            logs.setup(logging.INFO, fmt=LOG_FORMAT)
            with logs.bind(session_id="s-j", tool_call_id="c-j"):
                name, level, fmt, args, expected = self.SAMPLES[0]
                logging.getLogger(name).log(level, fmt, *args)
            logging.getLogger("tests.journal").warning("careful")
        (message, fields), (warning, warned) = sent
        self.assertRegex(message, "^" + ASCTIME + re.escape(expected) + "$")
        self.assertEqual(fields["SESSION_ID"], "s-j")
        self.assertEqual(fields["TOOL_CALL_ID"], "c-j")
        self.assertNotIn("TURN_ID", fields)
        self.assertEqual((fields["PRIORITY"], warned["PRIORITY"]), ("6", "4"))
        self.assertEqual(fields["LOGGER"], "omarchy_ai.voice.gemini")
        self.assertTrue(warning.endswith(" tests.journal WARNING careful"))

    def journal_stream(self):
        """sys.stderr as a file the journal is said to be connected to."""
        test = self

        class Stream:
            def __enter__(self):
                self.file = open(test.tmp / "stderr", "w")
                st = os.fstat(self.file.fileno())
                self.patches = [
                    mock.patch.object(sys, "stderr", self.file),
                    mock.patch.dict(
                        os.environ, {"JOURNAL_STREAM": f"{st.st_dev}:{st.st_ino}"}
                    ),
                ]
                for p in self.patches:
                    p.start()
                return self.file

            def __exit__(self, *exc):
                for p in reversed(self.patches):
                    p.stop()
                self.file.close()

        return Stream()


class Seams(LogCase):
    def test_carry_takes_only_the_ids_into_another_thread(self):
        seen = {}
        with logs.bind(session_id="s-c", turn_id="t-c"):
            thread = threading.Thread(
                target=logs.carry(lambda: seen.update(logs.current()))
            )
        thread.start()
        thread.join()
        self.assertEqual(seen, {"session_id": "s-c", "turn_id": "t-c"})

    def test_bind_restores_what_was_bound(self):
        with logs.bind(session_id="outer"):
            with logs.bind(session_id="inner", turn_id="t"):
                self.assertEqual(
                    logs.current(), {"session_id": "inner", "turn_id": "t"}
                )
            self.assertEqual(logs.current(), {"session_id": "outer"})
        self.assertEqual(logs.current(), {})

    def test_env_round_trip(self):
        ids = {"session_id": "s", "step_id": "3"}
        self.assertEqual(logs.from_env(logs.env(ids)), ids)

    def test_an_agenda_run_binds_its_run_id_and_step(self):
        seen = []
        job = {"id": "t-1", "kind": "remind", "title": "x"}
        with (
            mock.patch.dict(
                agenda.RUNNERS,
                {"remind": lambda job, now, jev: seen.append(logs.current())},
            ),
            mock.patch.object(agenda, "_missed", lambda job, now: False),
            mock.patch.object(agenda, "_release", lambda job_id: None),
        ):
            agenda.run_job(job, jev=object(), now=1000.0)
        self.assertEqual(seen, [{"run_id": "t-1@1000"}])

    def test_an_effect_binds_its_effect_id_and_journals_the_ids(self):
        def dispatch(key):
            logging.getLogger("tests.effect").info("sending")
            return ActionResult(True, "sent")

        with logs.bind(session_id="s-e", tool_call_id="c-e"):
            effects.perform("t", "send", {"to": "x"}, dispatch, origin_id="turn:e")
        line = next(r for r in self.records() if r["logger"] == "tests.effect")
        journal = effects.records()
        self.assertEqual(line["effect_id"], journal[-1]["effect_id"])
        self.assertEqual(line["tool_call_id"], "c-e")
        self.assertEqual(
            (journal[0]["log"], journal[-1]["log"]["session_id"]),
            ({"session_id": "s-e", "tool_call_id": "c-e"}, "s-e"),
        )


@needs_bwrap
class SandboxedSubprocesses(FakeHome):
    """Task Runtime commands and user tools run under bwrap
    (runtime/sandbox.py) and still get the IDs in their environment."""

    def test_a_sandboxed_task_command_gets_the_ids(self):
        command = (
            "printenv OMARCHY_LOG_SESSION_ID OMARCHY_LOG_TASK_ID OMARCHY_LOG_STEP_ID"
        )
        policy = sandbox.for_command(command, Risk.LOW, cwd=self.ws, workspace=self.ws)
        self.assertIsNotNone(policy)
        with (
            patch("omarchy_ai.execution.actions._desktop_env", lambda: dict(self.env)),
            logs.bind(session_id="s-b", task_id="task-b", step_id=2),
        ):
            result = shell.run(command, self.ws, timeout=20, sandbox=policy)
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.output.split(), ["s-b", "task-b", "2"])

    def test_a_sandboxed_user_tool_gets_the_ids(self):
        tools_dir = self.home / "tools"
        for attr, value in (
            ("TOOLS_DIR", tools_dir),
            ("PROPOSED_DIR", tools_dir / "_proposed"),
            ("LEDGER", tools_dir / "approvals.json"),
        ):
            p = patch.object(user_tools, attr, value)
            p.start()
            self.addCleanup(p.stop)
        folder = tools_dir / "ids_probe"
        folder.mkdir(parents=True)
        (folder / "tool.json").write_text(
            json.dumps(
                {
                    "name": "ids_probe",
                    "description": "Prints its log IDs for a test, nothing else.",
                    "parameters": {"type": "object", "properties": {}},
                }
            )
        )
        (folder / "run").write_text("#!/bin/sh\nprintenv OMARCHY_LOG_TOOL_CALL_ID\n")
        (folder / "test").write_text("#!/bin/sh\nexit 0\n")
        for part in ("run", "test"):
            (folder / part).chmod(0o755)
        user_tools.LEDGER.write_text(
            json.dumps(
                {
                    "ids_probe": {
                        "approved_at": "now",
                        "sha256": user_tools._hashes(folder),
                    }
                }
            )
        )
        with logs.bind(tool_call_id="call-b"):
            result = user_tools.run("ids_probe", {})
        self.assertIn("call-b", result.message)


if __name__ == "__main__":
    unittest.main()
