import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core.jev import JevError
from omarchy_ai.voice.myapi_refinement import (
    FAST_RESULT_CHARS,
    MAX_REFINEMENT_SOURCE_CHARS,
    refine_myapi_result,
)


class MyApiRefinementTests(unittest.TestCase):
    def test_small_result_needs_no_model(self):
        raw = '{"date":"Monday"}'
        with patch("omarchy_ai.voice.myapi_refinement.Jev") as jev:
            self.assertEqual(refine_myapi_result(object(), "When?", raw), raw)
        jev.assert_not_called()

    def test_large_result_uses_one_jev_choice_to_select_records(self):
        rows = [{"id": i, "subject": "x" * 900} for i in range(20)]
        raw = json.dumps({"data": {"messages": rows}})
        self.assertGreater(len(raw), FAST_RESULT_CHARS)
        jev = SimpleNamespace(ask=lambda *a, **k: {
            "records": {"choice": "r7", "p": .9, "probabilities": {"r7": .9, "r2": .8}}
        })
        with patch("omarchy_ai.voice.myapi_refinement.Jev", return_value=jev):
            refined = json.loads(refine_myapi_result(object(), "Find message 7", raw))
        self.assertTrue(refined["jev_selected"])
        self.assertEqual([row["id"] for row in refined["selected_records"]], [7, 2])
        self.assertEqual(refined["source_path"], "$.data.messages")

    def test_unavailable_jev_preserves_bounded_source(self):
        raw = json.dumps({"rows": [{"id": i, "text": "x" * 4000} for i in range(30)]})
        broken = SimpleNamespace(ask=lambda *a, **k: (_ for _ in ()).throw(JevError("offline")))
        with patch("omarchy_ai.voice.myapi_refinement.Jev", return_value=broken):
            result = refine_myapi_result(object(), "Find it", raw)
        self.assertEqual(len(result), MAX_REFINEMENT_SOURCE_CHARS)
        self.assertIn("shortened from", result)

    def test_empty_request_skips_refinement(self):
        raw = "x" * (FAST_RESULT_CHARS + 1)
        with patch("omarchy_ai.voice.myapi_refinement.Jev") as jev:
            self.assertEqual(refine_myapi_result(object(), "", raw), raw)
        jev.assert_not_called()


if __name__ == "__main__":
    unittest.main()
