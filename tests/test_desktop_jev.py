import copy
import json
import unittest
from unittest.mock import Mock, patch

from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.desktop_jev import (
    DesktopLoop, cancel_desktop_tasks, candidates, questions, choice, verified,
)
from omarchy_ai.execution.os_knowledge import search, search_os_knowledge


def state():
    return {"errors": [], "windows": [{"address": "0x123", "app": "foot", "title": "Terminal", "focused": True}],
            "panels": [{"id": "omarchy.ai", "name": "Assistant"}], "workspace": 1,
            "volume": {"percent": 50, "muted": False}, "brightness": 50,
            "themes": ["Tokyo Night", "Nord"], "theme": "Tokyo Night"}


def answer(qs, operation, **targets):
    selections = {"operation": operation, **targets}
    result = {"goal_met": {"probability": 1.0 if operation == "done" else 0.0}}
    for name, q in qs.items():
        if q["type"] == "choice":
            selected = selections.get(name, "none")
            result[name] = {"choice": selected, "probabilities": {key: float(key == selected) for key in q["criteria"]}}
    return result


class DesktopLoopTests(unittest.TestCase):
    def test_sequence_verified_against_fresh_state(self):
        observed = state()
        def execute(name, args):
            observed["workspace"] = args["number"]
            return ActionResult(True, "dispatched")
        def evaluate(raw, qs):
            payload = json.loads(raw)
            return answer(qs, "done") if payload["verified_steps"] else answer(qs, "workspace", workspace="3")
        execute = Mock(side_effect=execute)
        result = DesktopLoop(evaluate, lambda: copy.deepcopy(observed), execute).run("Switch to workspace three")
        self.assertTrue(result.ok)
        self.assertEqual(json.loads(result.message)["status"], "completed")
        execute.assert_called_once_with("workspace_switch", {"number": 3})

    def test_dispatch_without_postcondition_is_not_success(self):
        execute = Mock(return_value=ActionResult(True, "ok"))
        result = DesktopLoop(lambda _, qs: answer(qs, "workspace", workspace="3"), state, execute).run("workspace 3")
        self.assertFalse(result.ok)
        self.assertIn("outcome unverified", result.message)
        self.assertEqual(execute.call_count, 1)

    def test_done_cannot_invent_completion(self):
        execute = Mock()
        result = DesktopLoop(lambda _, qs: answer(qs, "done"), state, execute).run("install package")
        self.assertFalse(result.ok)
        execute.assert_not_called()

    def test_stale_desktop_does_not_execute(self):
        changed = state(); changed["workspace"] = 2
        execute = Mock()
        result = DesktopLoop(lambda _, qs: answer(qs, "workspace", workspace="3"), Mock(side_effect=[state(), changed]), execute).run("workspace 3")
        self.assertIn("stale", result.message)
        execute.assert_not_called()

    def test_cancellation_during_request_prevents_execution(self):
        def evaluate(_, qs):
            cancel_desktop_tasks()
            return answer(qs, "workspace", workspace="3")
        execute = Mock()
        result = DesktopLoop(evaluate, state, execute).run("workspace 3")
        self.assertFalse(result.ok)
        execute.assert_not_called()

    def test_timeout_during_request_prevents_execution(self):
        execute = Mock()
        clock = [0.0]
        def evaluate(_, qs):
            clock[0] = 100
            return answer(qs, "workspace", workspace="3")
        with patch("omarchy_ai.execution.desktop_jev.time.monotonic", side_effect=lambda: clock[0]):
            result = DesktopLoop(evaluate, state, execute).run("workspace 3")
        self.assertFalse(result.ok)
        execute.assert_not_called()

    def test_invalid_uncertain_and_missing_answers_fail_closed(self):
        for mutation in (lambda a: a["operation"].update(choice="shell"),
                         lambda a: a["operation"].update(probabilities={"workspace": 1}),
                         lambda a: a["operation"].update(confidence=.2),
                         lambda a: a["operation"]["probabilities"].update(workspace=float("nan")),
                         lambda a: a.clear()):
            def evaluate(_, qs):
                result = answer(qs, "workspace", workspace="3"); mutation(result); return result
            execute = Mock()
            self.assertFalse(DesktopLoop(evaluate, state, execute).run("workspace 3").ok)
            execute.assert_not_called()

    def test_none_target_and_model_failure_handoff(self):
        for evaluate in (lambda _, qs: answer(qs, "focus"), Mock(side_effect=TimeoutError)):
            execute = Mock()
            self.assertFalse(DesktopLoop(evaluate, state, execute).run("focus it").ok)
            execute.assert_not_called()

    def test_dry_run_never_executes(self):
        execute = Mock()
        result = DesktopLoop(lambda _, qs: answer(qs, "volume_set", volume_percent="43"), state, execute).run("volume 43", dry_run=True)
        self.assertEqual(json.loads(result.message)["trace"][0]["args"], {"percent": 43})
        self.assertFalse(result.ok)
        execute.assert_not_called()

    def test_panel_dispatch_requires_vision_handoff(self):
        execute = Mock(return_value=ActionResult(True, "Shell accepted; visually unverified"))
        result = DesktopLoop(lambda _, qs: answer(qs, "open_panel", panel="0"), state, execute).run("open assistant panel")
        execute.assert_called_once_with("open_bar_panel", {"id": "omarchy.ai"})
        self.assertFalse(result.ok)
        self.assertIn("outcome unverified", result.message)

    def test_repeat_toggle_is_blocked(self):
        observed = state()
        def execute(*_):
            observed["volume"]["muted"] = not observed["volume"]["muted"]
            return ActionResult(True, "ok")
        execute = Mock(side_effect=execute)
        result = DesktopLoop(lambda _, qs: answer(qs, "volume_mute_toggle"), lambda: copy.deepcopy(observed), execute).run("mute")
        self.assertIn("Repeated action", result.message)
        self.assertEqual(execute.call_count, 1)

    def test_candidate_domains_and_unsupported_operations(self):
        s = state()
        s["windows"] *= 200
        qs = questions(s, candidates(s))
        self.assertTrue(all(len(q["criteria"]) <= 255 for q in qs.values() if q["type"] == "choice"))
        self.assertNotIn("close_window", qs["operation"]["criteria"])
        self.assertNotIn("type_text", qs["operation"]["criteria"])
        self.assertNotIn("run_omarchy_command", qs["operation"]["criteria"])
        self.assertEqual(candidates({}), {})

    def test_volume_verifies_unmute_and_percentage(self):
        before = state(); after = state()
        after["volume"] = {"percent": 40, "muted": True}
        self.assertFalse(verified("volume_set", {"percent": 40}, before, after))
        after["volume"]["muted"] = False
        self.assertTrue(verified("volume_set", {"percent": 40}, before, after))

    def test_device_change_invalidates_verification(self):
        before = state(); after = state()
        before["volume_device"] = "speaker"; after["volume_device"] = "headphones"
        self.assertFalse(verified("volume_set", {"percent": 50}, before, after))

    def test_unrelated_window_title_does_not_invalidate_brightness(self):
        changed = state(); changed["windows"][0]["title"] = "Clock tick"
        after = copy.deepcopy(changed); after["brightness"] = 51
        execute = Mock(return_value=ActionResult(True, "ok"))
        observe = Mock(side_effect=[state(), changed, after, after])
        count = [0]
        def evaluate(_, qs):
            count[0] += 1
            return answer(qs, "done") if count[0] > 1 else answer(qs, "brightness_set", brightness_percent="51")
        result = DesktopLoop(evaluate, observe, execute).run("brightness 51")
        self.assertTrue(result.ok)
        execute.assert_called_once_with("brightness_set", {"percent": 51})

    def test_failed_verification_read_preserves_dispatch_trace(self):
        execute = Mock(return_value=ActionResult(True, "ok"))
        observe = Mock(side_effect=[state(), state(), RuntimeError("unavailable")])
        result = DesktopLoop(lambda _, qs: answer(qs, "workspace", workspace="3"), observe, execute).run("workspace 3")
        payload = json.loads(result.message)
        self.assertFalse(result.ok)
        self.assertTrue(payload["trace"][0]["dispatch_ok"])
        self.assertIn("verification observation failed", payload["reason"])

    def test_batched_gateway_questions_use_evaluation_endpoint(self):
        from omarchy_ai.voice.omarchy import GatewayClient
        from types import SimpleNamespace
        client = GatewayClient.__new__(GatewayClient)
        client.config = SimpleNamespace(omarchy_jev_model="typesafe-ai/jev")
        client._request = Mock(return_value=b'{"answers": {}}')
        qs = questions(state(), candidates(state()))
        self.assertEqual(client.evaluate_questions("state", qs), {})
        args = client._request.call_args.args
        self.assertEqual(args[0], "/ai/evaluation-model")
        self.assertEqual(json.loads(args[1])["questions"], qs)
        self.assertEqual(args[3]["ai-model-id"], "typesafe-ai/jev")
        self.assertEqual(client._request.call_args.kwargs["timeout"], 8)

    def test_probability_is_not_renamed_confidence(self):
        qs = questions(state(), candidates(state()))
        a = answer(qs, "workspace", workspace="3")
        self.assertEqual(choice(a, "operation", qs["operation"]), "workspace")
        self.assertNotIn("confidence", a["operation"])

    def test_knowledge_is_packaged_bounded_and_reference_only(self):
        self.assertLessEqual(len(search("audio volume", 100)), 5)
        self.assertTrue(any("volume" in d["text"].lower() for d in search("audio volume")))
        result = search_os_knowledge({"query": "theme"})
        self.assertTrue(json.loads(result.message)["reference_only"])
        self.assertFalse(search_os_knowledge({"query": ""}).ok)

    def test_shared_live_tools_include_worker_and_knowledge(self):
        from omarchy_ai.config import Config
        from omarchy_ai.voice.live import build_session_config
        with patch("omarchy_ai.voice.live.load_recent_context", return_value=""), patch("omarchy_ai.voice.live.load_preferences", return_value=[]), patch("omarchy_ai.voice.live.updates.wake_notice", return_value=""):
            session = build_session_config(Config(myapi_enabled=False))
        names = {t["name"] for t in session["delegation"]["responses"]["tools"]}
        self.assertTrue({"desktop_task", "browser_task", "search_os_knowledge", "describe_screen"} <= names)
        self.assertIn("remain the live conversational model", session["instructions"])
