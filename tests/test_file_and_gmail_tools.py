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

    def test_submit_sudo_password_never_returns_the_secret(self):
        completed = SimpleNamespace(returncode=0, stdout="", stderr="")
        with patch.object(actions.sudo_approval, "retrieve", return_value="not-in-results"), \
             patch.object(actions, "load_config", return_value=SimpleNamespace(sudo_access_enabled=True)), \
             patch.object(actions.shutil, "which", return_value="/usr/bin/wtype"), \
             patch.object(actions.subprocess, "run", return_value=completed) as run:
            result = actions.submit_sudo_password({})
        self.assertTrue(result.ok)
        self.assertNotIn("not-in-results", result.message)
        self.assertEqual(run.call_count, 2)

    def test_regular_window_sudo_is_pointed_at_submit_sudo_password(self):
        with patch.object(actions, "load_config", return_value=SimpleNamespace(sudo_access_enabled=True)), \
             patch("omarchy_ai.execution.workbench.exists", return_value=False):
            result = actions.terminal_sudo({"name": "ben-ami@Jarvis-HQ:~"})
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
