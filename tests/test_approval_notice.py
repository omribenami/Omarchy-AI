"""A task waiting for approval is said at once and floats as an envelope on
the desktop until it is answered (user request, 2026-09-27, after a push
approval sat unnoticed while the user thought the task was still working)."""
import json
import subprocess
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from omarchy_ai.core import agenda
from omarchy_ai.display import assistant_huds
from omarchy_ai.runtime import service
from omarchy_ai.runtime.executors.base import NEEDS_APPROVAL, Report

from test_task_runtime import RuntimeHarness, ScriptedJev, ScriptExecutor, git_repo


class ApprovalNoticeTests(RuntimeHarness):
    def setUp(self):
        super().setUp()
        patch.object(agenda, "INBOX_PATH", self.root / "inbox.jsonl").start()
        self.addCleanup(patch.stopall)

    def waiting_task(self, session=None, wake=None):
        git_repo(self.ws)

        def work(a, ctx):
            r = ctx.run_command("git clean -n", a.workspace, 10)
            return Report(NEEDS_APPROVAL, claim="need git clean", approval=r["request"])
        runtime = self.runtime([ScriptExecutor("SYSTEM_AGENT", work)], ScriptedJev())
        loop = MagicMock()
        loop.call_soon_threadsafe.side_effect = lambda fn, *args: fn(*args)
        with patch.object(service, "get_runtime", return_value=runtime):
            service.announce_to(lambda: session, loop, wake=wake)
        task = runtime.start("tidy the repo", str(self.ws), source="voice", background=False)
        return runtime, self.store.load(task.id)

    def test_an_approval_wakes_the_assistant_even_with_no_conversation(self):
        woken = []
        _, task = self.waiting_task(session=None, wake=woken.append)
        self.assertEqual(task.status, "waiting_approval")
        [entry] = woken
        self.assertEqual((entry["task_id"], entry["outcome"]), (task.id, "waiting_approval"))
        self.assertIn("git clean", entry["detail"])

    def test_the_daemon_starts_a_conversation_to_say_it(self):
        from omarchy_ai.config import Config
        from omarchy_ai.core.daemon import OmaDaemon
        d = OmaDaemon.__new__(OmaDaemon)
        d.config, d._state, d._session = Config(provider="gemini"), "listening", None
        d._pending_announcements, d._manual, d._listen_stop = [], False, threading.Event()
        self.waiting_task(session=None, wake=d._on_agenda_result)
        self.assertTrue(d._listen_stop.is_set(), "the listener is stopped so a conversation starts")
        self.assertTrue(d._manual)
        self.assertEqual(d._pending_announcements[0]["outcome"], "waiting_approval")

    def test_status_shows_the_change_waiting_behind_the_approval(self):
        runtime, task = self.waiting_task(wake=lambda e: None)
        (self.ws / "README.md").write_text("old demo line\n")
        subprocess.run(["git", "-C", str(self.ws), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(self.ws), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "r"],
                       check=True)
        (self.ws / "README.md").write_text("new demo line\n")
        with patch.object(service, "get_runtime", return_value=runtime):
            status = json.loads(service.task_status({"task_id": task.id}).message)
        preview = status["change_preview"]
        self.assertIn("README.md", preview["uncommitted"])
        self.assertIn("new demo line", preview["diff"])

    def test_envelope_rows_are_the_waiting_approvals(self):
        _, task = self.waiting_task(wake=lambda e: None)
        with patch("omarchy_ai.runtime.task.TaskStore.list", return_value=[task]):
            [item] = assistant_huds.approval_items()
        self.assertEqual(item["id"], task.id)
        self.assertEqual(item["risk"], "ELEVATED")
        self.assertIn("git clean", item["subject"])

    def test_envelope_buttons_resume_the_task_in_the_daemon(self):
        from omarchy_ai.core.daemon import OmaDaemon
        runtime, task = self.waiting_task(wake=lambda e: None)
        d = OmaDaemon.__new__(OmaDaemon)
        with patch.object(service, "get_runtime", return_value=runtime), \
                patch.object(runtime, "_launch") as launched:
            result = d.panel_command(f"approval deny {task.id}")
        self.assertTrue(result["ok"], result)
        launched.assert_called_once()
        self.assertIn(task.pending_approval["fingerprint"], self.store.load(task.id).declined)
        self.assertFalse(d.panel_command("approval maybe x")["ok"])


class EnvelopeRefreshTests(RuntimeHarness):
    def test_refreshing_tasks_also_updates_the_envelope(self):
        with patch.object(assistant_huds, "_ipc_async") as ipc, \
                patch.object(assistant_huds, "task_items", return_value=[]), \
                patch.object(assistant_huds, "approval_items", return_value=[{"id": "t1"}]):
            assistant_huds.refresh_tasks()
        ipc.assert_any_call("updateApprovals", {"items": [{"id": "t1"}]})

    def test_no_git_workspace_has_no_preview(self):
        self.assertIsNone(service.change_preview(str(self.ws)))
