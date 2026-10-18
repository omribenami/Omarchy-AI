"""The outbox (core/outbox.py): one surface per message, results exactly once."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import no_live_desktop
from omarchy_ai.core import agenda, outbox


class OutboxCase(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        outbox.reset_for_tests()
        self.addCleanup(outbox.reset_for_tests)
        self.desktop, self.push, self.chat = MagicMock(), MagicMock(), MagicMock()
        for target, value in (
            ("LEDGER_PATH", self.root / "outbox.json"),
            ("_desktop", self.desktop),
            ("_push", self.push),
            ("_chat", self.chat),
        ):
            patcher = patch.object(outbox, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.where("desk")

    def where(self, desk_state, phone=False):
        """desk_state: "active", "idle" or None (unknown)."""
        for target, value in (
            ("_desk_state", lambda: desk_state),
            ("_phone_connected", lambda: phone),
        ):
            patcher = patch.object(outbox, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def conversation(self):
        said = []
        self.addCleanup(outbox.register_voice(lambda: True, said.append))
        return said


class RoutingTests(OutboxCase):
    def result(self, **kw):
        return outbox.Message(outbox.RESULT, "Done: backup", "All files copied.", **kw)

    def test_at_the_desk_a_desktop_notification_only(self):
        self.where("active")
        self.assertEqual(outbox.send(self.result()), (outbox.DESKTOP,))
        self.desktop.assert_called_once_with("Done: backup", "All files copied.", False)
        self.push.assert_not_called()

    def test_away_from_the_desk_or_on_the_phone_page_a_push_and_its_chat(self):
        for desk, phone in (("idle", False), ("active", True)):
            with self.subTest(desk=desk, phone=phone):
                self.where(desk, phone)
                self.assertEqual(
                    outbox.send(self.result(conversation="c1")),
                    (outbox.CHAT, outbox.PUSH),
                )
        self.desktop.assert_not_called()

    def test_in_a_conversation_it_is_said_there_and_nowhere_else(self):
        said = self.conversation()
        self.assertEqual(outbox.send(self.result(conversation="c1")), (outbox.VOICE,))
        self.assertEqual(said[0]["title"], "Done: backup")
        self.desktop.assert_not_called()
        self.push.assert_not_called()

    def test_a_phone_call_counts_as_a_conversation(self):
        loop, session = MagicMock(), MagicMock()
        loop.is_closed.return_value = False
        outbox.attach_call(session, loop)
        self.assertEqual(outbox.send(self.result()), (outbox.VOICE,))
        loop.call_soon_threadsafe.assert_called_once()
        self.assertTrue(
            agenda.attached(session), "the agenda's names reach the same registry"
        )

    def test_urgent_reaches_desktop_and_phone_unknown_presence_too(self):
        self.where("active")
        self.assertEqual(
            outbox.send(self.result(priority=outbox.URGENT)),
            (outbox.DESKTOP, outbox.PUSH),
        )
        self.where(None)
        self.assertEqual(
            outbox.send(outbox.Message(outbox.ALERT, "Low disk")),
            (outbox.DESKTOP, outbox.PUSH),
        )

    def test_a_message_that_names_its_channels_goes_there(self):
        said = self.conversation()
        used = outbox.send(
            outbox.Message(outbox.APPROVAL, "Approve?", channels=(outbox.PUSH,))
        )
        self.assertEqual((used, said), ((outbox.PUSH,), []))

    def test_expired_messages_are_dropped(self):
        old = self.result(ttl=5)
        old.created -= 10
        self.assertEqual(outbox.send(old), ())
        self.desktop.assert_not_called()

    def test_acks_and_progress_never_become_notifications(self):
        self.assertEqual(outbox.route(outbox.Message(outbox.ACK, "On it")), ())
        self.assertEqual(
            outbox.route(outbox.Message(outbox.PROGRESS, "Still on it")), ()
        )


class ExactlyOnceTests(OutboxCase):
    def test_one_origin_gets_exactly_one_result_across_three_channels(self):
        said: list = []
        unregister = outbox.register_voice(lambda: True, said.append)
        key = "task:t1:abc"
        sent = [
            outbox.send(
                outbox.Message(
                    outbox.RESULT, "Done", "x", dedupe_key=key, channels=(channel,)
                )
            )
            for channel in (outbox.VOICE, outbox.DESKTOP, outbox.PUSH)
        ]
        unregister()
        self.assertEqual(sent, [(outbox.VOICE,), (), ()])
        self.assertEqual(len(said), 1)
        self.desktop.assert_not_called()
        self.push.assert_not_called()
        self.assertEqual(
            [a["note"] for a in outbox.audit() if a["key"] == key],
            ["", "duplicate", "duplicate"],
        )

    def test_a_result_defaults_to_one_per_origin(self):
        for _ in range(3):
            outbox.send(outbox.Message(outbox.RESULT, "Done", origin_id="call-1"))
        self.desktop.assert_called_once()

    def test_the_ledger_survives_a_restart(self):
        self.assertTrue(outbox.claim("agenda:j1:100:completed"))
        outbox.reset_for_tests()  # a new process: nothing in memory
        self.assertFalse(outbox.claim("agenda:j1:100:completed"))
        self.assertTrue(outbox.delivered("agenda:j1:100:completed"))

    def test_concurrent_senders_deliver_once(self):
        barrier = threading.Barrier(8)

        def go():
            barrier.wait()
            outbox.send(outbox.Message(outbox.RESULT, "Done", dedupe_key="race"))

        threads = [threading.Thread(target=go) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.desktop.assert_called_once()

    def test_a_task_said_in_the_conversation_is_not_also_notified(self):
        # runtime/service.py's listener runs before runtime._notify: one delivery.
        from omarchy_ai.runtime import runtime, service
        from omarchy_ai.runtime.task import Task

        task = Task(
            id="t-once",
            goal="email Chris",
            workspace="/",
            status="certified",
            result="Sent.",
        )
        title, body, fingerprint = runtime.notice(task, "certified")
        said = []
        self.assertTrue(
            outbox.once(runtime.outbox_key(task, fingerprint), lambda: said.append(1))
        )
        store = SimpleNamespace(save=lambda t: None)
        runtime._notify(SimpleNamespace(store=store), task, "certified")
        self.assertEqual(said, [1])
        self.desktop.assert_not_called()
        self.push.assert_not_called()
        self.assertTrue(hasattr(service, "announce_to"))

    def test_without_a_conversation_the_task_notice_shows_once_with_its_open_button(
        self,
    ):
        from omarchy_ai.runtime import runtime
        from omarchy_ai.runtime.task import Task

        task = Task(
            id="t-desk",
            goal="email Chris",
            workspace="/",
            status="certified",
            result="Sent.",
        )
        store = SimpleNamespace(save=lambda t: None)
        runtime._notify(SimpleNamespace(store=store), task, "certified")
        task.notifications.clear()  # even a task record that lost its fingerprint
        runtime._notify(SimpleNamespace(store=store), task, "certified")
        self.desktop.assert_called_once()
        self.assertEqual(
            self.desktop.call_args.kwargs["actions"], {"default": "Open task"}
        )


class AgendaThroughOutboxTests(OutboxCase):
    def setUp(self):
        super().setUp()
        for target, value in (
            ("JOBS_PATH", self.root / "agenda.json"),
            ("INBOX_PATH", self.root / "inbox.jsonl"),
            ("_refresh_routines_hud", lambda: None),
        ):
            patcher = patch.object(agenda, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def job(self):
        return {"id": "j1", "title": "Backup", "kind": "command", "next_run": 1000.0}

    def test_in_a_conversation_a_job_result_is_said_not_shown_and_stays_in_the_inbox(
        self,
    ):
        said = self.conversation()
        agenda._inbox_add(
            self.job(), "succeeded", "ok", title="Backup", body="Command succeeded"
        )
        self.assertEqual(said[0]["outcome"], "succeeded")
        self.desktop.assert_not_called()
        self.assertEqual([e["outcome"] for e in agenda._inbox_read()], ["succeeded"])

    def test_with_none_open_it_is_shown_and_may_wake_her(self):
        heard = []
        with patch.object(agenda, "listeners", [heard.append]):
            agenda._inbox_add(
                self.job(), "succeeded", "ok", title="Backup", body="Command succeeded"
            )
        self.desktop.assert_called_once_with("Backup", "Command succeeded", False)
        self.assertEqual(heard[0]["outcome"], "succeeded")

    def test_the_same_run_is_never_delivered_twice(self):
        for _ in range(2):
            agenda._inbox_add(
                self.job(), "succeeded", "ok", title="Backup", body="Command succeeded"
            )
        self.desktop.assert_called_once()


class AckTests(OutboxCase):
    def test_one_ack_per_request(self):
        said = []
        self.assertTrue(outbox.ack("s:1", lambda: said.append(1)))
        self.assertFalse(outbox.ack("s:1", lambda: said.append(2)))
        self.assertEqual(said, [1])

    def test_no_ack_after_the_model_spoke(self):
        outbox.model_acked("s:2")
        self.assertFalse(outbox.ack("s:2", lambda: self.fail("acked twice")))

    def test_which_calls_are_acknowledged(self):
        for tool in (
            "myapi_gmail_search",
            "myapi_list_services",
            "start_task",
            "browser_task",
            "desktop_task",
            "run_mission",
            "look_camera",
        ):
            self.assertTrue(outbox.needs_ack(tool), tool)
        for tool in ("volume_up", "list_windows", "use_tool", "open_browser"):
            self.assertFalse(outbox.needs_ack(tool), tool)

    def test_progress_is_rate_limited_and_coalesced(self):
        got = []
        self.assertTrue(outbox.progress("t", "step 1", got.append, every=10, now=100))
        self.assertFalse(outbox.progress("t", "step 2", got.append, every=10, now=105))
        self.assertFalse(outbox.progress("t", "step 3", got.append, every=10, now=108))
        self.assertTrue(outbox.progress("t", "step 4", got.append, every=10, now=111))
        self.assertEqual(got, ["step 1", "step 4"])

    def test_the_users_own_acknowledgement_words(self):
        rules = [
            "Always answer in short sentences.",
            "When you start working on a request, acknowledge it with an assertive 'Right away!' first.",
        ]
        self.assertEqual(outbox.ack_phrase(rules), "Right away!")
        hebrew = ['כשאת מתחילה משימה, תאשרי בקול: "בטח, מיד!"']
        self.assertEqual(outbox.ack_phrase(hebrew), "בטח, מיד!")
        self.assertEqual(outbox.phrase_language("בטח, מיד!"), "he")
        self.assertIsNone(outbox.ack_phrase(["Call me 'boss' always."]))
        self.assertIsNone(outbox.ack_phrase([]))

    def test_only_an_acknowledgement(self):
        for text in (
            "On it.",
            "Okay, done!",
            "על זה!",
            "בסדר, רגע",
            "Right away!",
            "ok ok",
        ):
            self.assertTrue(outbox.only_ack(text, "Right away!"), text)
        for text in (
            "The fan is on.",
            "על זה! כיביתי את המאוורר בחצר.",
            "Done: you have 4 new emails",
        ):
            self.assertFalse(outbox.only_ack(text, "Right away!"), text)

    def test_the_ack_clip_uses_those_words_once_rendered(self):
        from omarchy_ai.voice import fallback

        phrase = "Right away!"
        stem = outbox.ack_stem(phrase)
        with patch.object(outbox, "ack_phrase", return_value=phrase):
            self.assertEqual(fallback.ack_texts(), {stem: phrase})
            with patch.object(
                fallback.alert_clips,
                "find",
                side_effect=lambda stems, lang=None: (
                    Path(f"/clips/{stems[0]}.ogg") if stems == [stem] else None
                ),
            ):
                self.assertEqual(fallback.ack_clip(), Path(f"/clips/{stem}.ogg"))
            with patch.object(
                fallback.alert_clips,
                "find",
                side_effect=lambda stems, lang=None: (
                    Path("/clips/working-he.ogg") if stems == ["working"] else None
                ),
            ):
                self.assertEqual(fallback.ack_clip(), Path("/clips/working-he.ogg"))


class TransportTests(unittest.TestCase):
    def test_desktop_notification_command(self):
        with (
            patch.object(outbox.subprocess, "run") as run,
            patch.object(outbox, "_desktop_env", lambda: None),
        ):
            no_live_desktop.REAL_OUTBOX["desktop"]("T", "B", True)
        self.assertEqual(
            run.call_args.args[0],
            ["notify-send", "-a", "Omarchy AI", "-u", "critical", "T", "B"],
        )

    def test_push_goes_through_flux_off_the_callers_thread(self):
        from omarchy_ai.phone import flux_notify

        done = threading.Event()
        with (
            patch.object(flux_notify, "_enabled", return_value=True),
            patch.object(
                flux_notify, "deliver", side_effect=lambda *a: done.set()
            ) as deliver,
        ):
            no_live_desktop.REAL_OUTBOX["push"]("T", "B", "approval", ["d1"])
            self.assertTrue(done.wait(2))
        deliver.assert_called_once_with("T", "B", "approval", ["d1"])


class PresenceTests(unittest.TestCase):
    def test_unknown_when_the_shell_cannot_be_asked(self):
        outbox.reset_for_tests()
        with (
            patch.object(outbox, "_phone_connected", side_effect=OSError),
            patch.object(outbox, "_desk_state", lambda: "active"),
        ):
            self.assertEqual(outbox.presence(), outbox.UNKNOWN)
        with (
            patch.object(outbox, "_phone_connected", lambda: False),
            patch.object(outbox, "_desk_state", lambda: "idle"),
        ):
            self.assertEqual(outbox.presence(), outbox.AWAY)


if __name__ == "__main__":
    unittest.main()
