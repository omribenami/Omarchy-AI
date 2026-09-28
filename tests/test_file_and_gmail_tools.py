import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from omarchy_ai.execution import actions, files, tile_logs


class LocalFileToolTests(unittest.TestCase):
    def test_sudo_access_uses_keyring_without_exposing_password(self):
        from omarchy_ai.execution import sudo_approval
        stored = SimpleNamespace(returncode=0, stdout="", stderr="")
        looked_up = SimpleNamespace(returncode=0, stdout="not-in-results\n", stderr="")
        with patch.object(sudo_approval, "_run", side_effect=[stored, looked_up]) as run:
            sudo_approval.store("not-in-results")
            self.assertEqual(sudo_approval.retrieve(), "not-in-results")
        self.assertEqual(run.call_count, 2)

    def test_readme_is_not_a_script_but_a_narrated_demo_is(self):
        # Journal 2026-09-27 08:18: README.md was marked a script and grep was blocked.
        readme = "## Install\n- one\n- two\n- three\nThen switch workspace.\n1. a\n2. b\n3. c step\n"
        self.assertFalse(actions._looks_like_script(readme, "~/Git/omarchy-ai/README.md"))
        self.assertFalse(actions._looks_like_script("- a\n- b\n- c\n", "notes.md"))
        demo = "In workspace 5 only:\nAll narration in parallel.\n1. Intro\n2. Terminal\n3. Browser\n"
        self.assertTrue(actions._looks_like_script(demo, "/home/u/commercial_prompt.md"))

    def test_submit_sudo_password_never_returns_the_secret(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(actions.sudo_approval, "retrieve", return_value="not-in-results"), \
             patch.object(actions, "load_config", return_value=SimpleNamespace(sudo_access_enabled=True)), \
             patch.object(actions.shutil, "which", return_value="/usr/bin/wtype"), \
             patch.object(actions, "_screen_locked", return_value=False), \
             patch.object(actions.subprocess, "run", return_value=completed) as run:
            result = actions.submit_sudo_password({})
        self.assertTrue(result.ok)
        self.assertNotIn("not-in-results", result.message)
        # The password, then Return (_desktop_env also asks hyprctl for the session).
        self.assertEqual([c.args[0][0] for c in run.call_args_list if c.args[0][0] == "wtype"], ["wtype", "wtype"])

    def test_regular_window_sudo_is_pointed_at_submit_sudo_password(self):
        with patch.object(actions, "load_config", return_value=SimpleNamespace(sudo_access_enabled=True)), \
             patch("omarchy_ai.execution.workbench.exists", return_value=False):
            result = actions.terminal_sudo({"name": "user@desktop:~"})
        self.assertFalse(result.ok)
        self.assertIn("submit_sudo_password", result.message)

    def test_missing_path_suggests_near_names(self):
        # 2026-09-24: 'Docker' asked for, the folder is 'docker'.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "docker").mkdir()
            (root / "minecraft_new").mkdir()
            config = SimpleNamespace(file_access_roots=[str(root)])
            with self.assertRaises(files.FileAccessError) as caught:
                files.list_files(str(root / "Docker"), config)
            self.assertIn("docker", str(caught.exception))
            with self.assertRaises(files.FileAccessError) as caught:
                files.list_files(str(root / "minecraft"), config)
            self.assertIn("minecraft_new", str(caught.exception))

    def test_assistant_terminal_gets_a_unique_stable_label(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            thread = MagicMock()
            with patch.object(tile_logs, "TILE_LOG_DIR", root), \
                 patch.object(tile_logs, "_clients", return_value=[]), \
                 patch.object(tile_logs.threading, "Thread", return_value=thread):
                initial, prefix, label = tile_logs.start_terminal_log()
            self.assertTrue(initial.exists())
            self.assertEqual(prefix[:2], ["env", f"OMARCHY_AI_TERMINAL_TITLE={label}"])
            self.assertTrue(label.startswith("Omarchy AI "))
            self.assertEqual(tile_logs.find_log(label), initial)
            with tile_logs._lock:
                tile_logs._aliases.pop(label.lower(), None)

    def test_read_write_and_root_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(file_access_roots=[str(root)])
            saved = files.write_file(str(root / "notes" / "todo.txt"), "hello\nworld", config)
            self.assertEqual(files.read_file(str(saved), config, 2), "world")
            with self.assertRaises(files.FileAccessError):
                files.read_file("/etc/passwd", config)
            with self.assertRaises(files.FileAccessError):
                files.write_file(str(saved), "replace", config)

    def test_exact_edit_is_atomic_and_refuses_stale_or_ambiguous_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = SimpleNamespace(file_access_roots=[str(root)])
            target = root / "settings.conf"
            target.write_text("before\nkey=old\nafter\n")
            path, count = files.edit_file(str(target), "key=old", "key=new", config)
            self.assertEqual((path, count), (target, 1))
            self.assertEqual(target.read_text(), "before\nkey=new\nafter\n")
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)
            with self.assertRaisesRegex(files.FileAccessError, "found 0"):
                files.edit_file(str(target), "key=old", "bad", config)
            target.write_text("key=old\nkey=old\n")
            with self.assertRaisesRegex(files.FileAccessError, "found 2"):
                files.edit_file(str(target), "key=old", "bad", config)

    def test_privileged_edit_is_bounded_to_etc_and_requires_password(self):
        config = SimpleNamespace(file_access_roots=["/tmp"])
        with self.assertRaisesRegex(files.FileAccessError, "limited to files under /etc"):
            files.edit_file("/usr/share", "a", "b", config, privileged=True, sudo_password="x")

    def test_manual_terminal_context_is_discoverable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            log = root / "pts_2.log"
            log.write_text(f"[2026-01-01T00:00:00] shell=bash pid={os.getpid()} cwd=/work/demo status=0 command=pytest\n")
            with patch.object(tile_logs, "TERMINAL_CONTEXT_DIR", root):
                self.assertTrue(any("/work/demo" in item for item in tile_logs.list_tiles()))
                self.assertIn("pytest", tile_logs.read_log("demo"))

    def test_closed_assistant_transcript_is_removed_on_daemon_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            live = root / "live"
            history = root / "history"
            live.mkdir()
            transcript = live / "Omarchy_AI_1234abcd.log"
            transcript.write_text("installer finished successfully\n")
            with patch.object(tile_logs, "TILE_LOG_DIR", live), \
                 patch.object(tile_logs, "TERMINAL_HISTORY_DIR", history):
                tile_logs.sweep_stale()
                self.assertFalse(transcript.exists())
                self.assertNotIn("installer finished successfully", tile_logs.read_log("Omarchy AI 1234abcd"))
                self.assertFalse(any("completed terminal" in item for item in tile_logs.list_tiles()))


class GmailAttachmentToolTests(unittest.TestCase):
    def test_gmail_search_uses_bounded_union_when_exact_text_misses(self):
        empty = {"data": {"response": {"data": {"messages": []}}}}
        found = {"data": {"response": {"data": {"messages": [
            {"id": "message-1", "from": "Chris", "snippet": "Tri-Pointe update"}
        ]}}}}
        client = MagicMock()
        client.request.side_effect = [empty, empty, empty, found]
        with patch.object(actions.myapi, "is_connected", return_value=True), \
             patch.object(actions.myapi, "MyApiClient", return_value=client), \
             patch.object(actions.myapi_usage, "record"):
            result = actions.myapi_gmail_search({"query": "Chris Tri-Point"})
        self.assertTrue(result.ok)
        payload = json.loads(result.message)
        self.assertEqual(payload["gmail_search"]["attempted"], [
            "Chris Tri-Point", 'Chris "Tri Point"', "Chris TriPoint",
            '{Chris Tri-Point "Tri Point" TriPoint}',
        ])
        self.assertEqual(payload["gmail_search"]["matched_by"],
                         '{Chris Tri-Point "Tri Point" TriPoint}')
        queries = [call.kwargs["body"]["params"]["arguments"]["query"]
                   for call in client.request.call_args_list]
        self.assertEqual(queries, payload["gmail_search"]["attempted"])

    def test_generic_gmail_zero_result_uses_search_fallback(self):
        empty_rest = {"ok": True, "data": {"resultSizeEstimate": 0}}
        empty = {"data": {"response": {"data": {"messages": []}}}}
        found = {"data": {"response": {"data": {"messages": [{"id": "message-1"}]}}}}
        client = MagicMock()
        client.call_service.return_value = empty_rest
        client.request.side_effect = [empty, empty, empty, empty, found]
        with patch.object(actions.myapi, "is_connected", return_value=True), \
             patch.object(actions.myapi, "MyApiClient", return_value=client), \
             patch.object(actions.myapi_usage, "record"):
            result = actions.myapi_call({
                "service": "gmail",
                "path": "/gmail/v1/users/me/messages",
                "query": {"q": "from:Chris Tri-Point"},
            })
        self.assertTrue(result.ok)
        payload = json.loads(result.message)
        self.assertEqual(payload["gmail_search"]["matched_by"],
                         '{Chris Tri-Point "Tri Point" TriPoint}')
        self.assertEqual(payload["result"], found)

    def test_myapi_write_passes_mutation_body_to_service(self):
        client = MagicMock()
        client.call_service.return_value = {"ok": True, "id": "event-1"}
        with patch.object(actions.myapi, "is_connected", return_value=True), \
             patch.object(actions.myapi, "MyApiClient", return_value=client), \
             patch.object(actions.myapi_usage, "record"):
            result = actions.myapi_write({
                "service": "googlecalendar", "path": "/calendar/v3/calendars/primary/events",
                "method": "POST", "body": {"summary": "Lunch"}, "description": "create Lunch",
            })
        self.assertTrue(result.ok)
        client.call_service.assert_called_once_with(
            "googlecalendar", "/calendar/v3/calendars/primary/events", "POST",
            query=None, body={"summary": "Lunch"},
        )

    def test_query_planner_preserves_hard_filters_and_relaxes_text_fields(self):
        planned = actions._gmail_fallback_queries(
            'from:Alex subject:"Quarterly-Update" has:attachment after:2026/01/01'
        )
        self.assertEqual(planned[0], 'Alex Quarterly-Update has:attachment after:2026/01/01')
        self.assertIn('Alex "Quarterly Update" has:attachment after:2026/01/01', planned)
        self.assertTrue(all('has:attachment' in query and 'after:2026/01/01' in query
                            for query in planned))
        self.assertTrue(planned[-1].startswith('{'))

    def test_query_planner_does_not_relax_filter_only_searches(self):
        self.assertEqual(actions._gmail_fallback_queries(
            'has:attachment after:2026/01/01 label:receipts'
        ), [])

    def test_search_extracts_attachment_ids(self):
        response = {"data": {"response": {"data": {"messages": [{"id": "message-1", "payload": {"parts": [
            {"filename": "report.pdf", "mimeType": "application/pdf", "body": {"attachmentId": "attach-1", "size": 42}}
        ]}}]}}}}
        client = MagicMock()
        client.request.return_value = response
        with patch.object(actions.myapi, "is_connected", return_value=True), \
             patch.object(actions.myapi, "MyApiClient", return_value=client), \
             patch.object(actions.myapi_usage, "record"):
            result = actions.myapi_gmail_search_attachments({"query": "filename:report.pdf"})
        self.assertTrue(result.ok)
        attachment = json.loads(result.message)["attachments"][0]
        self.assertEqual(attachment["message_id"], "message-1")
        self.assertEqual(attachment["attachment_id"], "attach-1")

    def test_download_decodes_and_saves_to_downloads(self):
        payload = base64.urlsafe_b64encode(b"file bytes").decode().rstrip("=")
        client = MagicMock()
        client.request.return_value = {"data": {"response": {"data": {"content_base64": payload}}}}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = SimpleNamespace(file_access_roots=[str(home)])
            with patch.object(actions.myapi, "is_connected", return_value=True), \
                 patch.object(actions.myapi, "MyApiClient", return_value=client), \
                 patch.object(actions, "load_config", return_value=config), \
                 patch.object(actions.Path, "home", return_value=home), \
                 patch.object(actions.myapi_usage, "record"):
                result = actions.myapi_gmail_download_attachment({
                    "message_id": "message-1", "attachment_id": "attach-1", "filename": "report.pdf",
                })
            saved = home / "Downloads" / "Omarchy_AI" / "report.pdf"
            self.assertTrue(result.ok)
            self.assertEqual(saved.read_bytes(), b"file bytes")

    def test_download_uses_myapi_file_url_when_present(self):
        client = MagicMock()
        client.request.return_value = {"data": {"response": {"data": {"file": {"s3url": "https://example.test/file"}}}}}
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = SimpleNamespace(file_access_roots=[str(home)])
            with patch.object(actions.myapi, "is_connected", return_value=True), \
                 patch.object(actions.myapi, "MyApiClient", return_value=client), \
                 patch.object(actions, "load_config", return_value=config), \
                 patch.object(actions.Path, "home", return_value=home), \
                 patch.object(actions, "_download_url", return_value=b"url bytes") as download, \
                 patch.object(actions.myapi_usage, "record"):
                result = actions.myapi_gmail_download_attachment({
                    "message_id": "message-1", "attachment_id": "attach-1", "filename": "report.pdf",
                })
            download.assert_called_once_with("https://example.test/file")
            self.assertTrue(result.ok)
            self.assertEqual((home / "Downloads" / "Omarchy_AI" / "report.pdf").read_bytes(), b"url bytes")


if __name__ == "__main__":
    unittest.main()
