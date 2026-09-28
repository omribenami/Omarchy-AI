import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core.jev import JevError
from omarchy_ai.voice.myapi_refinement import (
    MAX_REFINEMENT_SOURCE_CHARS,
    refine_myapi_result,
)


class MyApiRefinementTests(unittest.TestCase):
    def test_returns_jev_validated_answer(self):
        gateway = SimpleNamespace(complete_json=lambda *a, **k: {"answer": "Chris replied Monday."})
        jev = SimpleNamespace(ask=lambda *a, **k: {
            "answers_request": {"p": 0.96},
            "grounded": {"p": 0.99},
        })
        with patch("omarchy_ai.voice.myapi_refinement.GatewayClient", return_value=gateway), \
                patch("omarchy_ai.voice.myapi_refinement.Jev", return_value=jev):
            refined = refine_myapi_result(object(), "When did Chris reply?", '{"date":"Monday"}')
        self.assertEqual(json.loads(refined), {
            "jev_refined": True,
            "answer": "Chris replied Monday.",
        })

    def test_uncertain_or_unavailable_jev_preserves_original_result(self):
        raw = '{"date":"Monday"}'
        gateway = SimpleNamespace(complete_json=lambda *a, **k: {"answer": "Chris replied Tuesday."})
        uncertain = SimpleNamespace(ask=lambda *a, **k: {
            "answers_request": {"p": 0.95},
            "grounded": {"p": 0.12},
        })
        with patch("omarchy_ai.voice.myapi_refinement.GatewayClient", return_value=gateway), \
                patch("omarchy_ai.voice.myapi_refinement.Jev", return_value=uncertain):
            self.assertEqual(refine_myapi_result(object(), "When?", raw), raw)
        broken = SimpleNamespace(ask=lambda *a, **k: (_ for _ in ()).throw(JevError("offline")))
        with patch("omarchy_ai.voice.myapi_refinement.GatewayClient", return_value=gateway), \
                patch("omarchy_ai.voice.myapi_refinement.Jev", return_value=broken):
            self.assertEqual(refine_myapi_result(object(), "When?", raw), raw)

    def test_large_source_is_bounded_before_models_receive_it(self):
        captured = {}
        gateway = SimpleNamespace(complete_json=lambda system, user, **kwargs: (
            captured.setdefault("source", user["myapi_result"]) and {"answer": "Found it."}
        ))
        jev = SimpleNamespace(ask=lambda *a, **k: {
            "answers_request": {"p": 1.0},
            "grounded": {"p": 1.0},
        })
        raw = "start" + "x" * 500_000 + "end"
        with patch("omarchy_ai.voice.myapi_refinement.GatewayClient", return_value=gateway), \
                patch("omarchy_ai.voice.myapi_refinement.Jev", return_value=jev):
            refine_myapi_result(object(), "Find it", raw)
        self.assertEqual(len(captured["source"]), MAX_REFINEMENT_SOURCE_CHARS)
        self.assertTrue(captured["source"].startswith("start"))
        self.assertTrue(captured["source"].endswith("end"))

    def test_empty_request_skips_refinement(self):
        raw = '{"ok":true}'
        with patch("omarchy_ai.voice.myapi_refinement.GatewayClient") as gateway:
            self.assertEqual(refine_myapi_result(object(), "", raw), raw)
        gateway.assert_not_called()


if __name__ == "__main__":
    unittest.main()
