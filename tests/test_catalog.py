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
        declared = {t["name"] for t in catalog.declared()}
        self.assertEqual({t["name"] for t in TOOLS}, (declared - {"use_tool"}) | set(catalog.catalog()))
        self.assertFalse(declared & set(catalog.catalog()))
        self.assertLess(len(declared), 25)

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
        return s

    def dispatch(self, s, args, pick):
        session = SimpleNamespace(send_tool_response=AsyncMock())
        with patch("omarchy_ai.voice.gemini_live.catalog.resolve", return_value=pick), \
                patch("omarchy_ai.voice.gemini_live.run_action", return_value=ActionResult(True, "done")) as run, \
                patch("omarchy_ai.voice.gemini_live.LiveSession._current_window", return_value={}):
            asyncio.run(s._run_call(session, SimpleNamespace(id="c1", name="use_tool", args=args)))
        return run, session.send_tool_response.call_args.kwargs["function_responses"].response

    def test_jev_pick_runs_the_inner_tool_without_a_second_review(self):
        reviewed = []
        s = self.session(lambda tool, args, ctx: reviewed.append(tool))
        pick = catalog.Resolution(True, "nightlight_toggle", {}, picked=True, evidence={"p": 0.99})
        run, response = self.dispatch(s, {"request": "turn on the night light"}, pick)
        run.assert_called_once_with("nightlight_toggle", {})
        self.assertEqual(reviewed, [])
        self.assertTrue(response["ok"])

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


if __name__ == "__main__":
    unittest.main()
