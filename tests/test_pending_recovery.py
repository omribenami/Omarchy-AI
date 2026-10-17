"""Pending-action recovery (core/pending_actions.py): expiry, the uncertain
state and its card, and the user's check / resend / drop. The kill-and-restart
cases are in tests/chaos/test_recovery.py."""

import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from omarchy_ai.core import conversations, effects, pending_actions
from omarchy_ai.execution import myapi_agent
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.runtime import service
from test_effects import use_journal_in

ARGS = {"recipient_email": "someone@example.com", "subject": "Hi", "body": "Hi."}


class RecoveryCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        use_journal_in(self, root)
        for target, name, value in (
            (pending_actions, "PATH", root / "pending_actions.json"),
            (conversations, "DIR", root / "conversations"),
            (conversations, "_cache", {}),
            (conversations, "_dirty", {}),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.notified = []
        for target, name, value in (
            (
                pending_actions,
                "_notify",
                lambda t, b, c: self.notified.append((t, b, c)),
            ),
            (myapi_agent, "_worked", lambda action, raw: True),
            (myapi_agent, "_sent_check", lambda action: None),
            (conversations, "_task_lookup", lambda: lambda task_id: None),
            (conversations, "sync_installs", lambda: None),
        ):
            patcher = mock.patch.object(target, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.cid = conversations.create("phone-text")

    def prepare(self, method="GMAIL_SEND_EMAIL"):
        return pending_actions.create(
            {
                "service": "gmail",
                "method": method,
                "title": "Send email",
                "fields": [],
                "arguments": ARGS,
                "conversation": self.cid,
            }
        )

    def edit(self, aid, **fields):
        with pending_actions._locked() as doc:
            doc.data[aid].update(fields)
            for name, value in fields.items():
                if value is None:
                    doc.data[aid].pop(name)

    def cards(self):
        return [
            line
            for line in conversations.get(self.cid)["lines"]
            if line.get("role") == "card"
        ]

    def make_uncertain(self, aid):
        self.edit(aid, status="running", owner={"pid": 1, "start": "gone"})
        key = pending_actions.effect_key({**pending_actions.get(aid)})
        with effects._index() as doc:
            effects._entries(doc.data)[key] = {
                "effect_id": "e1",
                "kind": "myapi_execute",
                "tool": "GMAIL_SEND_EMAIL",
                "target": "gmail:GMAIL_SEND_EMAIL",
                "args_hash": "",
                "state": "dispatched",
                "ts": time.time(),
                "updated": time.time(),
                "owner": {"pid": 1, "start": "gone"},
            }
        (changed,) = pending_actions.recover()
        self.assertEqual(changed["status"], "uncertain")


class ExpiryTests(RecoveryCase):
    def test_new_actions_carry_their_expiry_and_cards_show_it(self):
        with mock.patch.object(myapi_agent, "_fields", return_value=("Send email", [])):
            myapi_agent._prepare(
                "gmail",
                {"name": "GMAIL_SEND_EMAIL"},
                ARGS,
                "send it",
                [],
                self.cid,
            )
        (aid,) = pending_actions._load()
        action = pending_actions.get(aid)
        self.assertAlmostEqual(
            action["expires_at"], action["created"] + pending_actions.WAIT_SECONDS
        )
        (card,) = self.cards()
        self.assertEqual(card["expires_at"], action["expires_at"])
        self.assertIn("waits for your OK until", card["text"])

    def test_an_old_file_without_expires_at_gets_the_old_rule(self):
        aid = self.prepare()
        created = time.time() - 3600
        self.edit(aid, expires_at=None, created=created)
        action = pending_actions.get(aid)
        self.assertEqual(
            pending_actions.expires_at(action), created + pending_actions.KEEP_SECONDS
        )
        self.assertEqual(pending_actions.recover(), [])
        self.assertEqual(self.cards()[-1:], [])

    def test_an_expired_action_cannot_be_sent_and_its_card_stops_waiting(self):
        aid = self.prepare()
        conversations.add_card(
            self.cid,
            {"kind": "confirm", "task_id": f"action:{aid}", "action": {"id": aid}},
        )
        self.assertEqual(self.cards()[-1]["status"], "waiting")
        self.edit(aid, expires_at=time.time() - 1)
        self.assertEqual(self.cards()[-1]["status"], "done")
        with mock.patch.object(myapi_agent, "_call") as call:
            ok, message = myapi_agent.decide(aid, True, "on the phone")
        self.assertFalse(ok)
        self.assertIn("expired", message)
        call.assert_not_called()
        (changed,) = pending_actions.recover()
        self.assertEqual(changed["outcome"], "cancelled")
        self.assertEqual(self.notified[0][2], self.cid)

    def test_settled_actions_are_pruned_open_ones_are_kept(self):
        old = time.time() - pending_actions.KEEP_SECONDS - 10
        done, waiting = self.prepare(), self.prepare()
        self.edit(done, status="done", created=old, decided=old)
        self.edit(waiting, created=old)
        self.prepare()
        data = pending_actions._load()
        self.assertNotIn(done, data)
        self.assertIn(waiting, data)


class RunningTests(RecoveryCase):
    def test_a_live_claimer_is_left_alone(self):
        aid = self.prepare()
        pending_actions.claim(aid)
        self.edit(aid, claimed=time.time() - 3600)
        self.assertEqual(pending_actions.recover(), [])
        self.assertEqual(pending_actions.get(aid)["status"], "running")

    def test_an_old_file_without_a_claimer_waits_two_minutes(self):
        aid = self.prepare()
        self.edit(aid, status="running", created=time.time() - 60)
        self.assertEqual(pending_actions.recover(), [])
        self.edit(aid, created=time.time() - pending_actions.STALE_RUNNING_SECONDS - 1)
        (changed,) = pending_actions.recover()
        self.assertEqual((changed["status"], changed["outcome"]), ("done", "failed"))
        self.assertIn("nothing went out", changed["message"])

    def test_a_send_still_in_flight_elsewhere_is_left_alone(self):
        aid = self.prepare()
        self.edit(aid, status="running", owner={"pid": 1, "start": "gone"})
        key = pending_actions.effect_key(pending_actions.get(aid))
        with mock.patch.object(effects, "in_flight", return_value=True) as in_flight:
            self.assertEqual(pending_actions.recover(), [])
        in_flight.assert_called_once_with(key)

    def test_a_confirmed_send_becomes_done_with_its_stored_result(self):
        aid = self.prepare()
        self.edit(aid, status="running", owner={"pid": 1, "start": "gone"})
        with effects.origin(f"pending:{aid}"):
            effects.perform(
                "myapi_execute",
                "GMAIL_SEND_EMAIL",
                {"service": "gmail", "arguments": ARGS},
                lambda key: ActionResult(True, '{"id": "m1"}'),
            )
        (changed,) = pending_actions.recover()
        self.assertEqual((changed["status"], changed["outcome"]), ("done", "done"))
        self.assertEqual(changed["result"], {"ok": True, "message": '{"id": "m1"}'})
        self.assertEqual(self.cards()[-1]["event"], "certified")


class UncertainTests(RecoveryCase):
    def test_the_card_waits_for_check_resend_or_drop(self):
        aid = self.prepare()
        self.make_uncertain(aid)
        card = self.cards()[-1]
        self.assertEqual(
            (card["kind"], card["status"], card["outcome"]),
            ("uncertain", "waiting", "uncertain"),
        )
        self.assertEqual(card["action"]["id"], aid)
        self.assertEqual(conversations.summaries()["waiting"], 1)
        self.assertIn(
            f"[Task action:{aid} uncertain] I may have sent this",
            conversations.context(self.cid),
        )

    def test_a_plain_send_never_resends(self):
        aid = self.prepare()
        self.make_uncertain(aid)
        with mock.patch.object(myapi_agent, "_call") as call:
            ok, message = myapi_agent.decide(aid, True, "on the phone")
        self.assertFalse(ok)
        self.assertIn("Resend", message)
        call.assert_not_called()
        self.assertEqual(pending_actions.get(aid)["status"], "uncertain")

    def test_cancel_drops_it(self):
        aid = self.prepare()
        self.make_uncertain(aid)
        self.assertTrue(myapi_agent.decide(aid, False, "on the phone")[0])
        self.assertEqual(pending_actions.get(aid)["outcome"], "dropped")
        self.assertEqual(self.cards()[-1]["status"], "done")

    def test_resend_uses_a_new_origin(self):
        aid = self.prepare()
        self.make_uncertain(aid)
        with mock.patch.object(
            myapi_agent, "_call", return_value=ActionResult(True, "{}")
        ) as call:
            ok, _ = myapi_agent.resolve(aid, "resend", "on the phone")
        self.assertTrue(ok)
        origin = call.call_args.kwargs["origin"]
        self.assertTrue(origin.startswith(f"pending:{aid}:resend:1:"), origin)
        self.assertEqual(pending_actions.get(aid)["outcome"], "done")

    def test_check_finds_it_in_sent(self):
        aid = self.prepare()
        self.make_uncertain(aid)
        with mock.patch.object(myapi_agent, "_sent_check", return_value=None):
            self.assertFalse(myapi_agent.resolve(aid, "check", "on the phone")[0])
        self.assertEqual(pending_actions.get(aid)["status"], "uncertain")
        with mock.patch.object(
            myapi_agent, "_sent_check", return_value="Sent ✓ to someone@example.com"
        ):
            self.assertTrue(myapi_agent.resolve(aid, "check", "on the phone")[0])
        self.assertEqual(pending_actions.get(aid)["outcome"], "sent")

    def test_a_live_send_with_no_answer_becomes_uncertain(self):
        aid = self.prepare()

        def no_answer(service_, method, args, origin=""):
            with effects.origin(origin):
                return effects.perform(
                    "myapi_execute",
                    method,
                    {"service": service_, "arguments": args},
                    lambda key: (_ for _ in ()).throw(
                        effects.Uncertain(ActionResult(False, "timed out"))
                    ),
                )

        with mock.patch.object(myapi_agent, "_call", side_effect=no_answer):
            ok, message = myapi_agent.decide(aid, True, "on the phone")
        self.assertFalse(ok)
        self.assertIn("may have sent", message)
        self.assertEqual(pending_actions.get(aid)["status"], "uncertain")
        self.assertEqual(self.cards()[-1]["kind"], "uncertain")

    def test_task_respond_answers_it_in_its_chat(self):
        aid = self.prepare()
        self.make_uncertain(aid)
        token = conversations.CURRENT.set(self.cid)
        try:
            with mock.patch.object(service, "get_runtime"):
                result = service.task_respond({"answer": "drop"})
        finally:
            conversations.CURRENT.reset(token)
        self.assertTrue(result.ok, result.message)
        self.assertEqual(pending_actions.get(aid)["outcome"], "dropped")


class PhoneEndpointTests(RecoveryCase):
    def post(self, body):
        from omarchy_ai.phone import server

        handler = server._Handler.__new__(server._Handler)
        handler.path = "/api/actions/respond"
        handler._is_paired = lambda: True
        handler._read_json_body = lambda: body
        sent = []
        handler._send_json = lambda code, payload: sent.append((code, payload))
        handler.do_POST()
        return sent[0]

    def test_choice_resolves_and_bad_bodies_are_refused(self):
        aid = self.prepare()
        self.make_uncertain(aid)
        self.assertEqual(self.post({"id": aid, "choice": "explode"})[0], 400)
        code, payload = self.post({"id": aid, "choice": "drop"})
        self.assertEqual(code, 200, payload)
        self.assertEqual(pending_actions.get(aid)["outcome"], "dropped")


if __name__ == "__main__":
    unittest.main()
