import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core import conversations
from omarchy_ai.phone import server


class ConversationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        for target, value in (("DIR", Path(self.tmp.name)), ("_cache", {}), ("_dirty", {})):
            patcher = patch.object(conversations, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # No titles from the Gateway in tests, no keyring for redaction.
        for target in ("_maybe_title",):
            patcher = patch.object(conversations, target)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(conversations, "_redact", side_effect=lambda text, speech: text.replace("hunter2", "***"))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tasks = {}

    def lookup(self, task_id):
        return self.tasks.get(task_id)

    def test_a_call_transcript_lands_in_its_conversation(self):
        cid = conversations.create("phone-voice")
        transcript = conversations.Transcript(cid)
        for role, text in (("user", "turn off "), ("user", "the TV"), ("assistant", "Done."), ("user", "thanks")):
            transcript.append({"role": role, "text": text})
        self.assertEqual(len(transcript), 4)  # still the session's own list
        lines = conversations.get(cid, self.lookup)["lines"]
        self.assertEqual([(l["role"], l["text"]) for l in lines],
                         [("user", "turn off the TV"), ("assistant", "Done."), ("user", "thanks")])
        conversations._cache.clear()  # what was saved to disk
        self.assertEqual(len(conversations.get(cid, self.lookup)["lines"]), 3)

    def test_passwords_are_masked_in_the_joined_line(self):
        cid = conversations.create("phone-text")
        conversations.add_line(cid, "user", "the password is hun")
        conversations.add_line(cid, "user", "ter2")
        self.assertEqual(conversations.get(cid, self.lookup)["lines"][0]["text"], "the password is ***")

    def test_task_cards_come_back_and_only_the_open_one_waits(self):
        cid = conversations.create("phone-voice")
        conversations.add_line(cid, "user", "email Chris")
        task = SimpleNamespace(id="t1", conversation=cid, status="waiting_user", question="Send it?",
                               pending_approval=None, result="")
        self.tasks["t1"] = task
        conversations.task_event(task, "waiting_user")
        conv = conversations.get(cid, self.lookup)
        card = conv["lines"][-1]
        self.assertEqual((card["role"], card["kind"], card["status"], card["question"]),
                         ("card", "question", "waiting", "Send it?"))
        self.assertEqual(conversations.summaries(self.lookup)["waiting"], 1)
        task.status, task.result = "certified", "Sent the reply to Chris."
        conversations.task_event(task, "certified")
        lines = conversations.get(cid, self.lookup)["lines"]
        self.assertEqual([l.get("status") for l in lines if l["role"] == "card"], ["done", "done"])
        self.assertEqual(lines[-1]["text"], "Sent the reply to Chris.")
        self.assertEqual(conversations.summaries(self.lookup)["waiting"], 0)

    def test_an_approval_card_waits_for_that_exact_request(self):
        cid = conversations.create("phone-text")
        task = SimpleNamespace(id="t2", conversation=cid, status="waiting_approval", question=None, result="",
                               pending_approval={"fingerprint": "fp1", "subject": "git push", "risk": "ELEVATED",
                                                 "reasons": ["publishes commits"]})
        self.tasks["t2"] = task
        conversations.task_event(task, "waiting_approval")
        card = conversations.get(cid, self.lookup)["lines"][-1]
        self.assertEqual((card["kind"], card["fingerprint"], card["risk"], card["status"]),
                         ("approval", "fp1", "ELEVATED", "waiting"))
        task.pending_approval = {"fingerprint": "fp2"}
        self.assertEqual(conversations.get(cid, self.lookup)["lines"][-1]["status"], "done")

    def test_a_task_without_a_conversation_adds_nothing(self):
        conversations.task_event(SimpleNamespace(id="t3", conversation="", status="failed", result="x"), "failed")
        self.assertEqual(conversations.summaries(self.lookup)["conversations"], [])

    def test_list_is_newest_first_and_hides_empty_talks(self):
        first, empty, second = (conversations.create(s) for s in ("phone-text", "phone-voice", "desktop-voice"))
        conversations.add_line(first, "user", "one")
        time.sleep(0.01)
        conversations.add_line(second, "user", "two")
        rows = conversations.summaries(self.lookup)["conversations"]
        self.assertEqual([r["id"] for r in rows], [second, first])
        self.assertEqual(rows[0]["preview"], "two")
        self.assertTrue(conversations.delete(first))
        self.assertEqual([r["id"] for r in conversations.summaries(self.lookup)["conversations"]], [second])
        self.assertIsNone(conversations.get("../../etc/passwd", self.lookup))

    def test_context_for_a_reopened_conversation(self):
        cid = conversations.create("phone-text")
        conversations.add_line(cid, "user", "my colour is teal")
        conversations.add_line(cid, "assistant", "Noted.")
        self.assertEqual(conversations.context(cid), "User: my colour is teal\nOmarchy: Noted.")

    def test_desktop_sessions_close_together_share_a_conversation(self):
        first = conversations.desktop()
        conversations.add_line(first, "user", "hi")
        self.assertEqual(conversations.desktop(), first)
        with patch.object(conversations, "DESKTOP_GAP", -1):
            self.assertNotEqual(conversations.desktop(), first)

    def test_notifications_point_to_their_conversation(self):
        conversations.notified("Omarchy AI · Approval: Omarchy task needs approval", "c1")
        self.assertEqual(conversations.for_notification("Omarchy AI · Approval: Omarchy task needs approval"), "c1")
        self.assertIsNone(conversations.for_notification("other"))

    def test_phone_api(self):
        cid = conversations.create("phone-text")
        conversations.add_line(cid, "user", "hello")
        with patch.object(conversations, "_task_lookup", return_value=self.lookup), \
                patch("omarchy_ai.execution.flux_approve.available", return_value=True):
            status, body = server.conversation_get("/api/conversations", {})
            self.assertEqual((status, body["conversations"][0]["id"]), (200, cid))
            status, body = server.conversation_get(f"/api/conversations/{cid}", {})
            self.assertEqual((status, body["lines"][0]["text"], body["fingerprint"]), (200, "hello", True))
            self.assertEqual(server.conversation_get("/api/conversations/nope-nope-nope", {})[0], 404)
            conversations.notified("Omarchy AI · Omarchy task done ✓", cid)
            status, body = server.conversation_get("/api/conversations/for-notification",
                                                   {"title": ["Omarchy AI · Omarchy task done ✓"]})
            self.assertEqual((status, body), (200, {"id": cid}))


class TitleTests(unittest.TestCase):
    def test_title_comes_from_the_gateway_model(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(conversations, "DIR", Path(tmp)), \
                patch.object(conversations, "_cache", {}), patch.object(conversations, "_dirty", {}), \
                patch.object(conversations, "_redact", side_effect=lambda text, speech: text):
            cid = conversations.create("phone-text")
            with patch.object(conversations, "_maybe_title"):
                conversations.add_line(cid, "user", "reply to Chris about the flooring quote")
                conversations.add_line(cid, "assistant", "Sent.")
            with patch("omarchy_ai.voice.omarchy.GatewayClient") as client, \
                    patch("omarchy_ai.config.load_config"):
                client.return_value.complete_json.return_value = {"title": '"Flooring quote reply to Chris."'}
                conversations._title(cid)
            self.assertEqual(conversations.get(cid, lambda _: None)["title"], "Flooring quote reply to Chris")


if __name__ == "__main__":
    unittest.main()
