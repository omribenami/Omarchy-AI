"""Every tool declares what calling it does to the world (execution/tools.py
EFFECT_CLASSES), and the agenda retries by it."""

import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from fakes.myapi_server import FakeMyApi, fake_identity
from omarchy_ai.core import agenda, approvals, effects, outbox, retry
from omarchy_ai.execution import actions, tools
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.catalog import USE_TOOL

SRC = Path(__file__).resolve().parent.parent / "src" / "omarchy_ai"
# A tool a session declares on its own, outside execution/tools.py.
_DECLARED = re.compile(r'"name": "([a-z_]+)",\s*\n\s*"description"')

# What core/agenda.py never retried before effect classes (its old regex).
_WAS_NOT_REPEATABLE = re.compile(
    r"toggle|play_pause|_next$|_prev$|type_text|press_key|terminal_type|gmail_send|"
    r"gmail_reply|myapi_write|myapi_execute|^myapi$|start_task|run_skill|write_file|edit_file"
)


def declared_tools() -> set[str]:
    names = {t["name"] for t in tools.TOOLS + tools.MYAPI_TOOLS + [USE_TOOL]}
    for folder in ("voice", "phone"):
        for path in (SRC / folder).glob("*.py"):
            names.update(_DECLARED.findall(path.read_text()))
    return names


class EffectClassTests(unittest.TestCase):
    def test_every_tool_has_an_effect_class(self):
        names = declared_tools()
        self.assertIn("remember_voice", names)  # the scan finds session-declared tools
        missing = sorted(n for n in names if n not in tools.EFFECT_CLASSES)
        self.assertEqual(missing, [], "add these to execution/tools.py EFFECT_CLASSES")
        for name, value in tools.EFFECT_CLASSES.items():
            self.assertIn(
                value, (tools.READ, tools.REVERSIBLE, tools.IRREVERSIBLE), name
            )

    def test_no_stale_entries(self):
        self.assertEqual(sorted(set(tools.EFFECT_CLASSES) - declared_tools()), [])

    def test_the_schemas_sent_to_models_stay_unchanged(self):
        for tool in tools.TOOLS + tools.MYAPI_TOOLS:
            self.assertEqual(set(tool), {"type", "name", "description", "parameters"})

    def test_every_tool_the_agenda_never_repeated_is_irreversible(self):
        for name in tools.EFFECT_CLASSES:
            if _WAS_NOT_REPEATABLE.search(name):
                self.assertEqual(tools.effect_class(name), tools.IRREVERSIBLE, name)

    def test_user_tools_are_reversible_and_unknown_names_irreversible(self):
        with mock.patch(
            "omarchy_ai.execution.user_tools.exists",
            side_effect=lambda n: n == "lights",
        ):
            self.assertEqual(tools.effect_class("lights"), tools.REVERSIBLE)
            self.assertEqual(tools.effect_class("no_such_tool"), tools.IRREVERSIBLE)


class AgendaRetryTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name)
        for name, file in (
            ("journal_path", effects.JOURNAL_NAME),
            ("index_path", effects.INDEX_NAME),
        ):
            patcher = mock.patch.object(effects, name, lambda file=file: base / file)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target, value in (
            ("JOBS_PATH", base / "agenda.json"),
            ("INBOX_PATH", base / "inbox.jsonl"),
            ("STEP_RETRY", retry.Policy(base=0.0, jitter=0.0)),
        ):
            patcher = mock.patch.object(agenda, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for owner, target in ((outbox, "_desktop"), (agenda, "_refresh_routines_hud")):
            patcher = mock.patch.object(owner, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.fake = FakeMyApi()
        self.addCleanup(self.fake.close)
        for patcher in (
            self.fake.patch(),
            mock.patch(
                "omarchy_ai.myapi.client._load_identity", side_effect=fake_identity
            ),
            mock.patch.object(actions.myapi, "is_connected", return_value=True),
            mock.patch.object(actions.myapi_usage, "record"),
            mock.patch("omarchy_ai.myapi.client._TIMEOUT", 0.3),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def routine(self, steps):
        # Approved when it is scheduled, as the user's tap would (core/approvals.py).
        with approvals.approved_call("schedule_task", {}, "tap"):
            return agenda.create(
                {"title": "routine", "kind": "action", "in_minutes": 1, "steps": steps}
            )

    def run_it(self, job):
        job = next(j for j in agenda._load() if j["id"] == job["id"])
        agenda.run_job(job, now=job["next_run"])

    def send_step(self):
        return {
            "tool": "myapi_gmail_send",
            "args": {
                "recipient_email": "a@example.com",
                "subject": "Hi",
                "body": "Hello",
            },
        }

    def test_an_irreversible_step_that_may_have_happened_is_never_repeated(self):
        self.fake.hang(match="/execute")
        self.addCleanup(self.fake.release)
        job = self.routine([self.send_step()])
        self.run_it(job)
        self.assertEqual(len(self.fake.writes), 1)
        self.assertIn(
            "failed at step 1", next(j for j in agenda._load())["last_result"]
        )

    def test_the_same_run_again_does_not_resend_its_irreversible_step(self):
        job = self.routine([self.send_step()])
        job = next(j for j in agenda._load() if j["id"] == job["id"])
        for _ in range(3):  # three runs of one occurrence (one origin)
            agenda.run_job(dict(job), now=job["next_run"])
        self.assertEqual(len(self.fake.writes), 1)

    def test_a_reversible_step_is_still_retried(self):
        job = self.routine([{"tool": "volume_set", "args": {"percent": 30}}])
        with mock.patch.object(
            actions,
            "ACTIONS",
            {"volume_set": mock.Mock(return_value=ActionResult(False, "busy"))},
        ) as table:
            self.run_it(job)
        self.assertEqual(table["volume_set"].call_count, 3)


if __name__ == "__main__":
    unittest.main()
