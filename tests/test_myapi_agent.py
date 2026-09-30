import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.core import conversations, pending_actions
from omarchy_ai.execution import myapi_agent
from omarchy_ai.execution.actions import ActionResult

GMAIL = [
    {"name": "GMAIL_FETCH_EMAILS", "description": "Fetch emails matching a query.",
     "parameters": {"query": {"type": "string", "optional": True}, "max_results": {"type": "integer", "optional": True}}},
    {"name": "GMAIL_REPLY_TO_THREAD", "description": "Reply in an existing thread.",
     "parameters": {"thread_id": {"type": "string"}, "recipient_email": {"type": "string"},
                    "message_body": {"type": "string"}, "user_id": {"type": "string", "optional": True}}},
]


class FakeJev:
    """Scripted Jev decisions: service, then steps; every argument check passes."""

    def __init__(self, picks):
        self.picks = list(picks)
        self.asked = []

    def ask(self, state, questions, **_):
        self.asked.append((state, questions))
        if "q" in questions:
            # Arguments match; a search changes nothing.
            return {"q": {"p": 0.05 if "send, create, change or delete" in questions["q"]["instructions"] else 0.95}}
        pick = self.picks.pop(0)
        return {"pick": {"choice": pick, "p": 0.9, "probabilities": {pick: 0.9}}}


class MyApiAgentTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        for target, name_, value in ((conversations, "DIR", root / "c"), (conversations, "_cache", {}),
                              (conversations, "_dirty", {}), (pending_actions, "PATH", root / "actions.json"),
                              (myapi_agent, "_cache", {})):
            p = patch.object(target, name_, value)
            p.start()
            self.addCleanup(p.stop)
        for target in ((conversations, "_maybe_title"),):
            p = patch.object(*target)
            p.start()
            self.addCleanup(p.stop)
        p = patch.object(conversations, "_redact", side_effect=lambda text, speech: text)
        p.start()
        self.addCleanup(p.stop)
        for name, value in (("services", [{"id": "gmail", "name": "gmail", "category": "email"}]),
                            ("methods", GMAIL)):
            p = patch.object(myapi_agent, name, return_value=value)
            p.start()
            self.addCleanup(p.stop)
        p = patch("omarchy_ai.myapi.is_connected", return_value=True)
        p.start()
        self.addCleanup(p.stop)
        self.cid = conversations.create("phone-text")

    def run_agent(self, jev, fills, calls):
        fill = iter(fills)
        with patch("omarchy_ai.core.jev.Jev", return_value=jev), \
                patch.object(myapi_agent, "_fill", side_effect=lambda *a: next(fill)), \
                patch.object(myapi_agent, "_call", side_effect=calls) as call:
            token = conversations.CURRENT.set(self.cid)
            try:
                return myapi_agent.run({"request": "reply to Chris about the flooring quote"}), call
            finally:
                conversations.CURRENT.reset(token)

    def test_a_reply_is_found_then_prepared_not_sent(self):
        found = json.dumps({"data": {"response": {"data": {"messages": [
            {"messageId": "m1", "threadId": "t1", "sender": "Chris <chris@example.com>", "subject": "Flooring quote",
             "messageText": "Here is the quote", "labelIds": []}]}}}})
        jev = FakeJev(["gmail", "GMAIL_FETCH_EMAILS", "GMAIL_REPLY_TO_THREAD"])
        reply = {"thread_id": "t1", "recipient_email": "chris@example.com", "message_body": "Any news on the price?"}
        result, call = self.run_agent(jev, [{"query": "from:chris flooring"}, reply],
                                      lambda s, m, a: ActionResult(True, found))
        self.assertTrue(result.ok)
        self.assertIn("NOT done yet", result.message)
        call.assert_called_once()  # only the search ran; the reply waits
        card = conversations.get(self.cid, lambda _: None)["lines"][-1]
        self.assertEqual((card["kind"], card["status"], card["action"]["title"]), ("confirm", "waiting", "Reply in thread"))
        fields = {f["label"]: f["value"] for f in card["action"]["fields"]}
        self.assertEqual((fields["To"], fields["Thread"]), ("chris@example.com", "Flooring quote"))
        # The step choice saw the search result before picking the reply.
        self.assertTrue(any("t1" in state.get("results_so_far", "") for state, questions in jev.asked
                            if "pick" in questions))

    def test_send_runs_it_and_email_counts_only_when_it_is_in_sent(self):
        aid = pending_actions.create({"service": "gmail", "method": "GMAIL_REPLY_TO_THREAD", "title": "Reply in thread",
                                      "arguments": {"thread_id": "t1", "recipient_email": "c@example.com",
                                                    "message_body": "hi"}, "fields": [], "conversation": self.cid})
        with patch.object(myapi_agent, "_call", return_value=ActionResult(True, '{"successful": true}')) as call, \
                patch.object(myapi_agent, "_worked", return_value=True), \
                patch.object(myapi_agent, "_sent_check", return_value="Sent ✓ to c@example.com"):
            ok, message = myapi_agent.decide(aid, True, "on the phone")
            self.assertEqual((ok, message), (True, "Sent ✓ to c@example.com"))
            self.assertFalse(myapi_agent.decide(aid, True, "again")[0])  # never twice
        call.assert_called_once_with("gmail", "GMAIL_REPLY_TO_THREAD", {"thread_id": "t1", "recipient_email":
                                                                        "c@example.com", "message_body": "hi"})
        self.assertEqual(pending_actions.get(aid)["outcome"], "sent")
        with patch.object(myapi_agent, "_call", return_value=ActionResult(True, "{}")), \
                patch.object(myapi_agent, "_worked", return_value=True), \
                patch.object(myapi_agent, "_sent_check", return_value="Not in Sent: ..."):
            other = pending_actions.create({"service": "gmail", "method": "GMAIL_SEND_EMAIL", "title": "Send email",
                                            "arguments": {"recipient_email": "c@example.com"}, "fields": []})
            self.assertFalse(myapi_agent.decide(other, True, "on the phone")[0])
        self.assertEqual(pending_actions.get(other)["outcome"], "failed")

    def test_cancel_runs_nothing(self):
        aid = pending_actions.create({"service": "gmail", "method": "GMAIL_SEND_EMAIL", "title": "Send email",
                                      "arguments": {}, "fields": []})
        with patch.object(myapi_agent, "_call") as call:
            self.assertEqual(myapi_agent.decide(aid, False, "on the phone"), (True, "Cancelled."))
        call.assert_not_called()

    def test_reads_are_run_and_only_reads(self):
        self.assertFalse(myapi_agent.is_write("GMAIL_FETCH_EMAILS"))
        self.assertFalse(myapi_agent.is_write("GOOGLECALENDAR_LIST_EVENTS"))
        for method in ("GMAIL_SEND_EMAIL", "GMAIL_DELETE_MESSAGE", "GITHUB_CREATE_AN_ISSUE", "SOMETHING_ODD"):
            self.assertTrue(myapi_agent.is_write(method), method)

    def test_no_fitting_service_says_so(self):
        with patch("omarchy_ai.core.jev.Jev", return_value=FakeJev(["none"])):
            result = myapi_agent.run({"request": "water the plants"})
        self.assertFalse(result.ok)
        self.assertIn("None of the user's connected services", result.message)


if __name__ == "__main__":
    unittest.main()
