"""File delivery (harness item 4.7): the outbox's attachment kind and task outputs."""

from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from omarchy_ai.core import conversations, outbox, outputs
from omarchy_ai.execution.actions import desktop
from omarchy_ai.execution.actions.base import ActionResult

OPEN_FILES, OPEN_EDITOR = (
    desktop.open_files,
    desktop.open_editor,
)  # before setUp fakes them
SECRET_LINE = "row-content-that-must-not-be-spoken"


class AttachmentCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        outbox.reset_for_tests()
        self.addCleanup(outbox.reset_for_tests)
        self.push = MagicMock()
        self.reveal_files = MagicMock(return_value=ActionResult(True, ""))
        self.reveal_editor = MagicMock(return_value=ActionResult(True, ""))
        patches = [
            patch.object(outbox, "LEDGER_PATH", self.root / "outbox.json"),
            patch.object(outbox, "_push", self.push),
            patch.object(outbox, "_desktop", MagicMock()),
            patch.object(outputs, "ROOT", self.root / "outputs"),
            patch.object(conversations, "DIR", self.root / "conversations"),
            patch.object(conversations, "ATTACHMENTS_DIR", self.root / "attachments"),
            patch.object(conversations, "_cache", {}),
            patch.object(conversations, "_dirty", {}),
            patch.object(conversations, "_maybe_title"),
            patch.object(conversations, "_maybe_summarize"),
            patch.object(
                conversations, "_redact", side_effect=lambda text, speech: text
            ),
            patch.object(desktop, "open_files", self.reveal_files),
            patch.object(desktop, "open_editor", self.reveal_editor),
        ]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        self.where("active")

    def where(self, desk_state, phone=False):
        for target, value in (
            ("_desk_state", lambda: desk_state),
            ("_phone_connected", lambda: phone),
        ):
            patcher = patch.object(outbox, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def output(
        self, name: str, body: bytes = b"a,b\n1,2\n", task_id: str = "t1"
    ) -> Path:
        folder = outputs.task_dir(task_id, create=True)
        path = folder / name
        path.write_bytes(body)
        return path

    def lines(self, cid: str) -> list[dict]:
        conversations.flush(cid)
        return [
            line
            for line in conversations._load(cid)["lines"]
            if line.get("role") == "assistant"
        ]


class ChatTests(AttachmentCase):
    def test_reaches_the_chat_once_in_the_existing_attachment_format(self):
        self.where("idle")
        cid = conversations.create("phone-text")
        path = self.output("report.csv")
        first = outbox.attachment(
            str(path), origin_id="task-t1", conversation=cid, label="Monthly report"
        )
        again = outbox.attachment(
            str(path), origin_id="task-t1", conversation=cid, label="Monthly report"
        )
        self.assertEqual(first, (outbox.CHAT, outbox.PUSH))
        self.assertEqual(again, ())
        lines = self.lines(cid)
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["text"], "Monthly report")
        meta = lines[0]["attachments"][0]
        self.assertEqual(set(meta), {"id", "name", "mime", "size", "url", "path"})
        self.assertEqual(
            (meta["name"], meta["mime"], meta["size"]),
            ("report.csv", "text/csv", path.stat().st_size),
        )
        self.assertIsNotNone(conversations.attachment(cid, meta["id"]))
        self.push.assert_called_once()
        self.assertTrue(outbox.delivered(f"task-t1:attachment:{path}"))

    def test_untrusted_provenance_is_kept_with_the_file(self):
        self.where("idle")
        cid = conversations.create("phone-text")
        outbox.attachment(
            str(self.output("page.txt")),
            origin_id="task-t1",
            conversation=cid,
            untrusted=True,
        )
        line = self.lines(cid)[0]
        self.assertTrue(line["attachments"][0]["untrusted"])
        self.assertIn("untrusted content", conversations.render_line(line))

    def test_too_large_or_unknown_is_sent_as_a_link_not_inlined(self):
        self.where("idle")
        cid = conversations.create("phone-text")
        big = self.output("big.png", b"")
        with big.open("wb") as stream:
            stream.truncate(outputs.INLINE_BYTES + 1)  # sparse: no 20 MB written
        odd = self.output("dump.qqzz", b"\x00\x01")
        for path in (big, odd):
            self.assertFalse(outputs.describe(path)["inline"])
            outbox.attachment(str(path), origin_id="task-t1", conversation=cid)
        lines = self.lines(cid)
        self.assertEqual(len(lines), 2)
        for line, path in zip(lines, (big, odd)):
            self.assertNotIn("attachments", line)
            self.assertIn(f"File on the desktop: {path}", line["text"])
        self.assertFalse((self.root / "attachments").exists())

    def test_a_missing_file_is_not_delivered(self):
        self.assertEqual(
            outbox.attachment(str(self.root / "gone.csv"), origin_id="task-t1"), ()
        )


class DesktopTests(AttachmentCase):
    def test_at_the_desk_an_image_is_revealed_and_a_text_file_opened(self):
        png = self.output("shot.png", b"\x89PNG")
        csv = self.output("data.csv")
        self.assertEqual(outbox.attachment(str(png), origin_id="o1"), (outbox.DESKTOP,))
        self.assertEqual(outbox.attachment(str(csv), origin_id="o1"), (outbox.DESKTOP,))
        self.reveal_files.assert_called_once_with({"path": str(png)})
        self.reveal_editor.assert_called_once_with({"path": str(csv)})
        self.push.assert_not_called()

    def test_open_files_selects_the_file(self):
        path = self.output("shot.png", b"\x89PNG")
        run = MagicMock(return_value=ActionResult(True, ""))
        with (
            patch.object(desktop, "_run_detached", run),
            patch.object(desktop.shutil, "which", return_value=None),
        ):
            OPEN_FILES({"path": str(path)})
            OPEN_FILES({})
            OPEN_EDITOR({"path": str(path)})
        self.assertEqual(
            [c.args[0] for c in run.call_args_list],
            [
                ["nautilus", "--new-window", "--select", str(path)],
                ["omarchy-launch-nautilus"],
                ["omarchy-launch-editor", str(path)],
            ],
        )


class VoiceTests(AttachmentCase):
    def test_voice_hears_a_summary_never_the_contents(self):
        said: list[dict] = []
        self.addCleanup(outbox.register_voice(lambda: True, said.append))
        cid = conversations.create("phone-voice")
        path = self.output("rows.csv", f"name\n{SECRET_LINE}\n".encode())
        self.assertEqual(
            outbox.attachment(str(path), origin_id="task-t1", conversation=cid),
            (outbox.VOICE, outbox.CHAT),
        )
        self.assertEqual(len(said), 1)
        self.assertIn("rows.csv", said[0]["detail"])
        self.assertIn("CSV", said[0]["detail"])
        self.assertNotIn(SECRET_LINE, str(said[0]))
        self.reveal_files.assert_not_called()
        self.reveal_editor.assert_not_called()


class RetentionTests(AttachmentCase):
    def test_old_folders_are_removed_and_recent_kept(self):
        old = self.output("a.csv", task_id="old")
        self.output("b.csv", task_id="new")
        stamp = time.time() - (outputs.KEEP_DAYS + 1) * 86400
        os.utime(old, (stamp, stamp))
        os.utime(old.parent, (stamp, stamp))
        self.assertEqual(outputs.cleanup(), ["old"])
        self.assertEqual(sorted(p.name for p in outputs.root().iterdir()), ["new"])

    def test_past_the_size_cap_the_oldest_go_first(self):
        for n, task_id in enumerate(("t-a", "t-b", "t-c")):
            path = self.output("f.bin", b"x" * 100, task_id=task_id)
            stamp = time.time() - 3600 * (3 - n)
            os.utime(path, (stamp, stamp))
            os.utime(path.parent, (stamp, stamp))
        self.assertEqual(outputs.cleanup(max_bytes=150), ["t-a", "t-b"])
        self.assertEqual([p.name for p in outputs.root().iterdir()], ["t-c"])

    def test_cleanup_never_creates_the_folder(self):
        self.assertEqual(outputs.cleanup(), [])
        self.assertFalse(outputs.root().exists())

    def test_default_root_is_the_xdg_data_dir(self):
        with (
            patch.object(outputs, "ROOT", None),
            patch.dict(os.environ, {"XDG_DATA_HOME": str(self.root / "xdg")}),
        ):
            self.assertEqual(
                outputs.root(), self.root / "xdg" / "omarchy-ai" / "outputs"
            )
            self.assertEqual(outputs.collect("t1"), [])
        self.assertFalse((self.root / "xdg").exists())

    def test_task_ids_cannot_leave_the_folder(self):
        with self.assertRaises(ValueError):
            outputs.task_dir("../etc")


class TaskTests(AttachmentCase):
    def task(self, **kw):
        from omarchy_ai.runtime.task import Task

        return Task(id="t1", goal="make a report", workspace=str(self.root), **kw)

    def test_outputs_are_linked_from_the_task_and_delivered_once(self):
        from omarchy_ai.runtime import runtime

        self.where("idle")
        cid = conversations.create("phone-text")
        path = self.output("report.csv")
        outside = self.root / "elsewhere.csv"
        outside.write_text("x")
        task = self.task(conversation=cid, tainted=True)
        task.artifacts.append(str(outside))
        runtime.TaskRuntime._collect_outputs(task)
        self.assertEqual(task.artifacts, [str(outside), str(path)])
        runtime._deliver_artifacts(task)
        runtime._deliver_artifacts(task)  # a second notice of the same task
        lines = self.lines(cid)
        self.assertEqual(len(lines), 1)
        meta = lines[0]["attachments"][0]
        self.assertEqual(meta["name"], "report.csv")
        self.assertTrue(meta["untrusted"])


if __name__ == "__main__":
    unittest.main()
