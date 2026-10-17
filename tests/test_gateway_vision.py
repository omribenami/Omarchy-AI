import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from omarchy_ai.execution import vision
from omarchy_ai.execution.actions import describe_screen, screenshot


def _answers(questions):
    answers = {}
    for question_id, question in questions.items():
        if question["type"] == "boolean":
            answers[question_id] = {"p": 0.8}
        elif question["type"] == "choice":
            keys = list(question["criteria"])
            probabilities = {key: 0.0 for key in keys}
            probabilities[keys[0]] = 1.0
            answers[question_id] = {
                "choice": keys[0], "p": 1.0, "probabilities": probabilities, "confidence": 0.9,
            }
        else:
            probabilities = {str(index): 0.0 for index in range(len(question["criteria"]))}
            probabilities["0"] = 1.0
            answers[question_id] = {"score": 0.0, "probabilities": probabilities, "confidence": 0.9}
    return answers


class GatewayVisionTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.image = self.base / "screen.png"
        Image.new("RGB", (640, 360), "green").save(self.image)
        self.config = SimpleNamespace(omarchy_jev_model="typesafe-ai/jev")

    def test_planner_validation_drops_bad_questions_individually(self):
        payload = {"questions": {
            "parcel": {"type": "boolean", "instructions": "Is a parcel visible?"},
            "vehicle_kind": {"type": "choice", "instructions": "What vehicle is visible?",
                             "criteria": {"car": "a car", "other": "none or another kind"}},
            "people": {"type": "score", "instructions": "How many people are visible?",
                       "criteria": list(vision._COUNT_LEVELS)},
            "bad count": {"type": "boolean", "instructions": "Invalid id"},
            "missing_other": {"type": "choice", "instructions": "Which?",
                              "criteria": {"car": "car", "truck": "truck"}},
            "wrong_count_levels": {"type": "score", "instructions": "How many dogs?",
                                   "criteria": ["none", "some"]},
            "extra_field": {"type": "boolean", "instructions": "Visible?", "answer": True},
            "unknown_type": {"type": "box", "instructions": "Where?", "criteria": ["left", "right"]},
        }}
        self.assertEqual(
            list(vision._validate_planned_questions(payload)),
            ["parcel", "vehicle_kind", "people"],
        )

    def test_planning_failure_uses_only_the_fixed_baseline(self):
        with patch("omarchy_ai.voice.omarchy.GatewayClient") as client, \
                patch("omarchy_ai.execution.vision.judge_image") as judge:
            client.return_value.complete_json.side_effect = RuntimeError("planner down")
            judge.side_effect = lambda _path, questions, _config, _context: _answers(questions)
            result = vision.look(self.image, "Describe it", self.config)
        questions = judge.call_args.args[1]
        self.assertEqual(set(questions), {
            "baseline_scene", "baseline_people", "baseline_vehicles", "baseline_animals",
        })
        self.assertTrue(result.startswith("Jev's typed answers about the image follow."))
        self.assertIn("Jev cannot read out exact text", result)

    def test_text_planner_never_receives_the_image_and_total_is_bounded(self):
        planned = {f"check_{index}": {"type": "boolean", "instructions": f"Is item {index} visible?"}
                   for index in range(8)}
        with patch("omarchy_ai.voice.omarchy.GatewayClient") as client, \
                patch("omarchy_ai.execution.vision.judge_image") as judge:
            client.return_value.complete_json.return_value = {"questions": planned}
            judge.side_effect = lambda _path, questions, _config, _context: _answers(questions)
            vision.look(self.image, "Check the desktop", self.config,
                        subject="the screen", context="Focused window class: kitty")
        _system, user = client.return_value.complete_json.call_args.args
        self.assertEqual(user, {
            "request": "Check the desktop", "subject": "the screen",
            "context": "Focused window class: kitty",
        })
        self.assertNotIn(str(self.image), repr(user))
        questions = judge.call_args.args[1]
        self.assertEqual(len(questions), 12)
        self.assertEqual(len([key for key in questions if key.startswith("check_")]), 5)
        self.assertEqual(len([key for key in questions if key.startswith("baseline_")]), 7)

    def test_renders_boolean_choice_and_score_uncertainty(self):
        questions = {
            "animal": {"type": "boolean", "instructions": "Is an animal present?"},
            "kind": {"type": "choice", "instructions": "What vehicle is it?",
                     "criteria": {"car": "a car", "truck": "a truck", "other": "none or other"}},
            "cars": {"type": "score", "instructions": "How many cars are visible?",
                     "criteria": list(vision._COUNT_LEVELS)},
        }
        count_probabilities = {str(index): 0.0 for index in range(10)}
        count_probabilities.update({"4": 0.15, "5": 0.45, "6": 0.40})
        answers = {
            "animal": {"p": 0.2},
            "kind": {"choice": "car", "p": 0.51,
                     "probabilities": {"car": 0.51, "truck": 0.45, "other": 0.04},
                     "confidence": 0.6},
            "cars": {"score": 5.2, "probabilities": count_probabilities, "confidence": 0.59},
        }
        result = vision._render("the image", questions, answers)
        self.assertIn("Is an animal present? -> no (p(yes)=0.20)", result)
        self.assertIn("a car (p=0.51; runner-up a truck p=0.45; confidence 0.60)", result)
        self.assertIn("How many cars are visible? -> about 5 (likely 5–6; confidence 0.59)", result)

    def test_look_never_raises_when_jev_fails(self):
        with patch("omarchy_ai.voice.omarchy.GatewayClient") as client, \
                patch("omarchy_ai.execution.vision.judge_image", side_effect=RuntimeError("down")):
            client.return_value.complete_json.return_value = {"questions": {}}
            self.assertTrue(vision.look(self.image, "describe", self.config).startswith("error:"))

    def test_capture_failure_never_claims_success(self):
        with patch("omarchy_ai.execution.vision._capture", return_value=None):
            self.assertFalse(screenshot({}).ok)

    def test_saved_screenshot_is_judged_by_look_and_failure_is_preserved(self):
        key = self.base / "vercel-key"
        key.write_text("vk-test")
        cfg = SimpleNamespace(vercel_gateway_api_key_path=str(key), omarchy_jev_model="typesafe-ai/jev")
        with patch("omarchy_ai.config.load_config", return_value=cfg), \
                patch("omarchy_ai.execution.vision._capture", return_value=self.image), \
                patch("omarchy_ai.execution.actions.desktop._screen_context", return_value="Focused: kitty"), \
                patch("omarchy_ai.execution.vision.look", return_value="error: unavailable") as look:
            result = screenshot({})
        self.assertFalse(result.ok)
        self.assertIn("saved at", result.message)
        look.assert_called_once_with(
            self.image,
            "Describe the visible desktop in this saved screenshot.",
            cfg,
            subject="the visible desktop screen",
            context="Focused: kitty",
        )

    def test_describe_screen_requires_gateway_and_uses_jev_look(self):
        cfg = SimpleNamespace(vercel_gateway_api_key_path=str(self.base / "missing"))
        with patch("omarchy_ai.config.load_config", return_value=cfg), \
                patch("omarchy_ai.execution.actions.desktop._terminal_text_hint", return_value=""), \
                patch("omarchy_ai.execution.vision._capture") as capture:
            result = describe_screen({"question": "How many cars?"})
        self.assertFalse(result.ok)
        self.assertIn("Gateway key", result.message)
        capture.assert_not_called()

        key = self.base / "key"
        key.write_text("vk-test")
        cfg = SimpleNamespace(vercel_gateway_api_key_path=str(key), omarchy_jev_model="typesafe-ai/jev")
        with patch("omarchy_ai.config.load_config", return_value=cfg), \
                patch("omarchy_ai.execution.vision._capture", return_value=self.image), \
                patch("omarchy_ai.execution.actions.desktop._screen_context", return_value="Focused: browser"), \
                patch("omarchy_ai.execution.actions.desktop._terminal_text_hint", return_value=""), \
                patch("omarchy_ai.execution.vision.look", return_value="typed answers") as look:
            result = describe_screen({"question": "How many cars?"})
        self.assertTrue(result.ok)
        look.assert_called_once_with(
            self.image, "How many cars?", cfg,
            subject="the visible desktop screen", context="Focused: browser",
        )


if __name__ == "__main__":
    unittest.main()
