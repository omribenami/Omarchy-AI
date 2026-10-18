import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from omarchy_ai.core.jev import JevError
from omarchy_ai.execution import catalog
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.tools import TOOLS
from omarchy_ai.voice import switchboard


class PickJev:
    """Answers the catalog choice with `pick` at probability `p`."""

    def __init__(self, pick, p=0.97, fail=False):
        self.pick, self.p, self.fail, self.calls = pick, p, fail, []

    def ask(self, state, questions, **kwargs):
        self.calls.append((state, questions))
        if self.fail:
            raise JevError("Gateway down")
        options = list(questions["tool"]["criteria"])
        rest = (1 - self.p) / (len(options) - 1)
        probabilities = {o: (self.p if o == self.pick else rest) for o in options}
        return {"tool": {"choice": self.pick, "p": self.p, "probabilities": probabilities}}


class CatalogTests(unittest.TestCase):
    def test_every_tool_is_either_declared_or_in_the_catalog(self):
        # Built-in tools only: the user's own tools (~/.config/omarchy-ai/tools,
        # e.g. one a task installed 2026-09-27) are not TOOLS.
        from omarchy_ai.execution import user_tools
        patcher = patch.object(user_tools, "schemas", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        declared = {t["name"] for t in catalog.declared()}
        self.assertEqual({t["name"] for t in TOOLS}, (declared - {"use_tool"}) | set(catalog.catalog()))
        self.assertFalse(declared & set(catalog.catalog()))
        self.assertLess(len(declared), 25)

    def test_look_camera_is_reached_through_use_tool_not_declared(self):
        patcher = patch("omarchy_ai.execution.user_tools.schemas", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)
        self.assertNotIn("look_camera", catalog.CORE)
        self.assertIn("look_camera", catalog.catalog())
        self.assertNotIn("look_camera", {t["name"] for t in catalog.declared(myapi_on=True)})
        pick = catalog.resolve({"name": "look_camera", "args": {"camera": "Gate", "question": "who is there?"}})
        self.assertEqual((pick.run, pick.tool), (True, "look_camera"))

    def test_myapi_is_declared_directly_when_connected(self):
        # 2026-09-30: MyApi is Jev's single entry point (execution/myapi_agent.py).
        names = {t["name"] for t in catalog.declared(myapi_on=True)}
        self.assertIn("myapi", names)
        self.assertNotIn("myapi", {t["name"] for t in catalog.declared()})

    def test_a_call_nested_inside_args_is_unwrapped(self):
        args = {"recipient_email": "a@example.com", "subject": "Hi", "body": "Hello"}
        r = catalog.resolve({"request": "send it", "args": {"name": "myapi_gmail_send", "args": args}}, myapi_on=True)
        self.assertTrue(r.run)
        self.assertEqual((r.tool, r.args), ("myapi_gmail_send", args))

    def test_a_confident_pick_without_arguments_runs_at_once(self):
        r = catalog.resolve({"request": "turn on the night light"}, jev=PickJev("nightlight_toggle"))
        self.assertTrue(r.run)
        self.assertEqual(r.tool, "nightlight_toggle")
        self.assertTrue(r.picked)

    def test_a_pick_that_needs_arguments_returns_its_parameters(self):
        r = catalog.resolve({"request": "remind me in 10 minutes"}, jev=PickJev("set_reminder"))
        self.assertFalse(r.run)
        offer = json.loads(r.message)
        self.assertEqual(offer["tools"][0]["name"], "set_reminder")
        self.assertIn("minutes", offer["tools"][0]["parameters"])

    def test_known_name_with_valid_args_skips_jev(self):
        jev = PickJev("none")
        r = catalog.resolve({"name": "set_reminder", "args": {"minutes": 10, "message": "oven"}}, jev=jev)
        self.assertTrue(r.run)
        self.assertFalse(r.picked)
        self.assertEqual(jev.calls, [])

    def test_a_parameter_beside_name_is_moved_into_args(self):
        # Journal 2026-09-27 01:33: {"name": "list_commands", "query": "screen recording"}.
        r = catalog.resolve({"name": "list_commands", "query": "screen recording"}, jev=PickJev("none"))
        self.assertTrue(r.run)
        self.assertEqual(r.args, {"query": "screen recording"})

    def test_a_name_inside_args_is_found(self):
        r = catalog.resolve({"args": {"name": "run_omarchy_command", "args": ["toggle", "nightlight"]}},
                            jev=PickJev("none"))
        self.assertTrue(r.run)
        self.assertEqual((r.tool, r.args), ("run_omarchy_command", {"args": ["toggle", "nightlight"]}))

    def test_known_name_without_args_is_filled_from_the_request(self):
        # Journal 2026-09-27 01:33: ~60 identical offers for "stop screen recording".
        asked = []
        def fill(tool, request, heard):
            asked.append((tool["name"], request, heard))
            return {"args": ["capture", "screenrecording", "--stop-recording"]}
        r = catalog.resolve({"name": "run_omarchy_command", "request": "stop screen recording"},
                            "stop the recording", jev=PickJev("none"), fill=fill)
        self.assertTrue(r.run)
        self.assertEqual(r.args, {"args": ["capture", "screenrecording", "--stop-recording"]})
        self.assertFalse(r.picked)  # the switchboard still reviews filled arguments
        self.assertEqual(asked, [("run_omarchy_command", "stop screen recording", "stop the recording")])

    def test_a_confident_pick_is_filled_too(self):
        r = catalog.resolve({"request": "close the disable-wifi terminal"}, jev=PickJev("terminal_close"),
                            fill=lambda tool, request, heard: {"name": "disable-wifi"})
        self.assertTrue(r.run)
        self.assertEqual((r.tool, r.args), ("terminal_close", {"name": "disable-wifi"}))

    def test_file_content_is_never_invented_from_the_request(self):
        r = catalog.resolve({"name": "write_file", "request": "add all the things I said to the file"},
                            jev=PickJev("none"), fill=lambda *a: self.fail("content must not be filled"))
        self.assertFalse(r.run)
        self.assertIn("missing", r.message)

    def test_an_incomplete_or_failed_fill_falls_back_to_the_offer(self):
        for fill in (lambda *a: {}, lambda *a: (_ for _ in ()).throw(TimeoutError("slow"))):
            r = catalog.resolve({"name": "run_omarchy_command", "request": "stop recording"},
                                jev=PickJev("none"), fill=fill)
            self.assertFalse(r.run)
            self.assertIn("run_omarchy_command", r.message)

    def test_known_name_with_bad_args_gets_the_schema_back(self):
        r = catalog.resolve({"name": "set_reminder", "args": {"when": "soon"}}, jev=PickJev("none"))
        self.assertFalse(r.run)
        self.assertIn("missing", r.message)
        self.assertIn("unknown when", r.message)

    def test_core_tools_are_called_directly(self):
        r = catalog.resolve({"name": "type_text", "args": {"text": "x"}})
        self.assertFalse(r.run)
        self.assertIn("call it directly", r.message)

    def test_unsure_pick_offers_the_likeliest(self):
        r = catalog.resolve({"request": "do the thing"}, jev=PickJev("screenshot", p=0.4))
        self.assertFalse(r.run)
        self.assertEqual(len(json.loads(r.message)["tools"]), catalog.CANDIDATES)

    def test_nothing_fits(self):
        r = catalog.resolve({"request": "tell me a joke"}, jev=PickJev("none"))
        self.assertFalse(r.run)
        self.assertIn("No catalog tool", r.message)

    def test_jev_down_falls_back_to_keyword_matches(self):
        r = catalog.resolve({"request": "cast the screen to the TV"}, jev=PickJev("x", fail=True))
        self.assertFalse(r.run)
        names = [t["name"] for t in json.loads(r.message)["tools"]]
        self.assertIn("start_casting", names)

    def test_signatures_mark_required_parameters(self):
        self.assertIn("set_reminder(minutes*, message)", catalog.signatures())

    def test_documented_myapi_operation_keeps_exact_execute_shape(self):
        r = catalog.resolve({
            "name": "GMAIL_CREATE_EMAIL_DRAFT",
            "parameters": {"recipient_email": "a@example.com", "body": "hello"},
            "request": "Create a draft to a@example.com",
        }, myapi_on=True)
        self.assertTrue(r.run)
        self.assertEqual(r.tool, "myapi_execute")
        self.assertEqual(r.args["service"], "gmail")
        self.assertEqual(r.args["operation"], "GMAIL_CREATE_EMAIL_DRAFT")
        self.assertEqual(r.args["arguments"]["body"], "hello")


class LiveConfigTests(unittest.TestCase):
    def test_picker_declares_core_plus_use_tool_and_explains_the_catalog(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import build_live_config
        on = build_live_config(Config(tool_picker=True))
        off = build_live_config(Config(tool_picker=False))
        names = {t["name"] for t in on["tools"][0]["function_declarations"]}
        self.assertIn("use_tool", names)
        self.assertIn("end_conversation", names)
        self.assertNotIn("set_reminder", names)
        self.assertIn("TOOL CATALOG", on["system_instruction"])
        self.assertNotIn("TOOL CATALOG", off["system_instruction"])
        self.assertIn("set_reminder", {t["name"] for t in off["tools"][0]["function_declarations"]})


class UseToolDispatchTests(unittest.TestCase):
    def session(self, review):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.gemini_live import GeminiLiveSession
        with patch("omarchy_ai.voice.gemini_live.EchoCancellation"):
            s = GeminiLiveSession(Config())
        s._transcript = [{"role": "user", "text": "turn on the night light"}]
        s._switchboard = SimpleNamespace(review=review)
        from omarchy_ai.voice import escalation
        s._escalator = escalation.Escalator(lambda reply, latest: (False, False))  # no network in tests
        return s

    def dispatch(self, s, args, pick, result=None):
        session = SimpleNamespace(send_tool_response=AsyncMock())
        result = result if result is not None else ActionResult(True, "done")
        with patch("omarchy_ai.voice.gemini_live.catalog.resolve", return_value=pick), \
                patch("omarchy_ai.voice.gemini_live.run_action", return_value=result) as run, \
                patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
            async def call():
                await s._run_call(session, SimpleNamespace(id="c1", name="use_tool", args=args))
                await asyncio.gather(*list(s._bg_tasks))  # the escalation check runs in the background
            asyncio.run(call())
        return run, session.send_tool_response.call_args.kwargs["function_responses"].response

    def test_a_workspace_switch_through_use_tool_is_still_checked_against_the_fast_path(self):
        # A switch that does arrive through use_tool is held to the 2026-09-23 "asked for 4, got 5" guard too.
        import time
        s = self.session(lambda *a, **k: self.fail("a call the session answers itself needs no review"))
        s._jev_done = ("workspace_switch", {"number": 4}, time.monotonic(), "verified: now on workspace 4", "switch to 4")
        run, response = self.dispatch(s, {"name": "workspace_switch", "args": {"number": 5}},
                                      catalog.Resolution(True, "workspace_switch", {"number": 5}, evidence={"by": "name"}))
        run.assert_not_called()
        self.assertFalse(response["ok"])
        self.assertIn("workspace 4", response["message"])
        s._jev_done = ("workspace_switch", {"number": 4}, time.monotonic(), "verified: now on workspace 4", "switch to 4")
        run, response = self.dispatch(s, {"name": "workspace_switch", "args": {"number": 4}},
                                      catalog.Resolution(True, "workspace_switch", {"number": 4}, evidence={"by": "name"}))
        run.assert_not_called()
        self.assertIn("Already done", response["message"])

    def test_recent_actions_through_use_tool_are_answered_by_the_session(self):
        s = self.session(lambda *a, **k: self.fail("a call the session answers itself needs no review"))
        s._action_log = [{"action": "volume_up", "args": {}, "ok": True, "message": "ok", "window": {}}]
        run, response = self.dispatch(s, {"request": "what did you do"},
                                      catalog.Resolution(True, "get_recent_actions", {}, picked=True, evidence={}))
        run.assert_not_called()
        self.assertTrue(response["ok"])
        self.assertIn("volume_up", response["message"])

    def test_the_moved_tools_are_catalog_tools(self):
        for name in ("close_window", "hide_window_labels", "get_recent_actions"):
            self.assertIn(name, catalog.catalog())
            self.assertNotIn(name, {t["name"] for t in catalog.declared()})
        self.assertIn("get_recent_actions(target)", catalog.signatures())
        # Without its own declaration the model used desktop_task for a workspace switch.
        self.assertIn("workspace_switch", {t["name"] for t in catalog.declared()})

    def test_jev_pick_runs_the_inner_tool_without_a_second_review(self):
        reviewed = []
        s = self.session(lambda tool, args, ctx: reviewed.append(tool))
        pick = catalog.Resolution(True, "nightlight_toggle", {}, picked=True, evidence={"p": 0.99})
        run, response = self.dispatch(s, {"request": "turn on the night light"}, pick)
        run.assert_called_once_with("nightlight_toggle", {})
        self.assertEqual(reviewed, [])
        self.assertTrue(response["ok"])

    def test_a_silent_success_through_use_tool_is_confirmed(self):
        # Journal 2026-10-17 07:56 and 09:49: use_tool volume_up/volume_down
        # reached Gemini as ok=True message='' (the volume script prints nothing).
        s = self.session(lambda tool, args, ctx: switchboard.Verdict("execute", tool, args))
        pick = catalog.Resolution(True, "volume_up", {}, evidence={"by": "name"})
        run, response = self.dispatch(s, {"name": "volume_up"}, pick, ActionResult(True, ""))
        run.assert_called_once_with("volume_up", {})
        self.assertEqual(response, {"ok": True, "message": "done"})

    def test_named_call_is_reviewed_as_the_inner_tool(self):
        reviewed = []
        def review(tool, args, ctx):
            reviewed.append((tool, args))
            return switchboard.Verdict("reject", message="Not run: Jev checked set_reminder ...")
        s = self.session(review)
        pick = catalog.Resolution(True, "set_reminder", {"minutes": 5, "message": "x"})
        run, response = self.dispatch(s, {"name": "set_reminder", "args": {"minutes": 5, "message": "x"}}, pick)
        run.assert_not_called()
        self.assertEqual(reviewed, [("set_reminder", {"minutes": 5, "message": "x"})])
        self.assertFalse(response["ok"])

    def test_an_offer_is_a_successful_lookup_not_a_failure(self):
        s = self.session(lambda *a: self.fail("no review for an offer"))
        run, response = self.dispatch(s, {"request": "remind me"},
                                      catalog.Resolution(False, message='{"tools": [{"name": "set_reminder"}]}'))
        run.assert_not_called()
        self.assertTrue(response["ok"])
        self.assertIn("set_reminder", response["message"])
        self.assertEqual(s._escalator._failures, [])

    def test_no_catalog_capability_is_a_failure_for_immediate_escalation(self):
        s = self.session(lambda *a: self.fail("no review for an offer"))
        run, response = self.dispatch(
            s, {"request": "control the sprinkler"},
            catalog.Resolution(False, message="No catalog tool does this. Use start_task."))
        self.assertFalse(response["ok"])
        self.assertIn("Missing capability", response["message"])

    def test_repeated_offers_become_a_failure(self):
        # Journal 2026-09-27: every offer was ok=True, so ~60 in a row never escalated.
        s = self.session(lambda *a: self.fail("no review for an offer"))
        offer = catalog.Resolution(False, message='{"tools": [{"name": "run_omarchy_command"}]}')
        oks = [self.dispatch(s, {"name": "run_omarchy_command", "request": "stop"}, offer)[1]["ok"]
               for _ in range(4)]
        self.assertEqual(oks, [True, True, False, False])
        self.assertGreater(s._escalator._last_escalation, 0)  # two failures: handed to the Task Runtime
        s._last_user_speech += 1  # the user speaks: a new turn starts counting again
        self.assertTrue(self.dispatch(s, {"request": "remind me"}, offer)[1]["ok"])


class CatalogOriginTests(unittest.TestCase):
    """2026-10-15 19:52: "tell the kids they have 2 minutes to leave the computer", said to the
    speaker, was picked as set_reminder (a notification on this computer's screen)."""

    SPEAKER = {"channel": "satellite", "talking_to": "the voice speaker", "at_this_computer": False,
               "room": None, "person": None}

    def test_the_pick_sees_where_the_request_came_from(self):
        jev = PickJev("set_reminder")
        catalog.resolve({"request": "tell the kids they have 2 minutes"}, jev=jev, origin=self.SPEAKER)
        state, questions = jev.calls[0]
        self.assertEqual(state["origin"], {"channel": "satellite", "talking_to": "the voice speaker",
                                           "at_this_computer": False})
        self.assertIn("`origin`", questions["tool"]["instructions"])

    def test_no_origin_known_means_none_in_the_state(self):
        jev = PickJev("set_reminder")
        catalog.resolve({"request": "remind me in 10 minutes"}, jev=jev, origin={"channel": None})
        self.assertNotIn("origin", jev.calls[0][0])

    def test_a_call_by_name_skips_the_pick_but_not_the_review(self):
        r = catalog.resolve({"name": "set_reminder", "args": {"minutes": 2, "message": "time to go"}},
                            jev=PickJev("x", fail=True))
        self.assertTrue(r.run)
        self.assertFalse(r.picked)  # _run_call reviews every call that is not `picked`

    def test_the_session_passes_its_origin_to_the_pick(self):
        s = UseToolDispatchTests.session(self, lambda tool, args, ctx: switchboard.Verdict("execute", tool, args))
        s.channel = "desktop-text"
        pick = catalog.Resolution(False, message="Jev is not sure.")
        with patch("omarchy_ai.voice.gemini_live.catalog.resolve", return_value=pick) as resolve, \
                patch("omarchy_ai.voice.gemini_live.run_action", side_effect=AssertionError("nothing runs")), \
                patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
            session = SimpleNamespace(send_tool_response=AsyncMock())
            asyncio.run(s._run_call(session, SimpleNamespace(id="c1", name="use_tool", args={"request": "x"})))
        self.assertEqual(resolve.call_args.kwargs["origin"]["channel"], "desktop-text")


if __name__ == "__main__":
    unittest.main()
