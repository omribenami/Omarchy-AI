"""Deterministic behavior scores for the legacy prompt and prompt v2."""

from __future__ import annotations

import json
import os

import pytest

from evals.__main__ import BASELINE, _dump, _rows
from evals.suite import SCENARIOS, run_scenario

_PROMPTS = ["legacy", "v2"]


@pytest.mark.parametrize("prompt", _PROMPTS)
@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda scenario: scenario.id)
def test_scenario(prompt, scenario):
    score = run_scenario(prompt, scenario)
    reason = scenario.xfail.get(prompt)
    if reason:
        if score.passed:
            pytest.fail(
                f"expected this to stay xfail ({reason}) but it passed; update the baseline"
            )
        pytest.xfail(reason)
    assert score.passed, score.detail


def test_scorecard_matches_committed_baseline():
    actual = _dump(_rows(False))
    expected = json.loads(BASELINE.read_text())
    assert actual["scenarios"].keys() == expected["scenarios"].keys()
    for scenario_id, item in actual["scenarios"].items():
        prior = expected["scenarios"][scenario_id]
        assert item["legacy"] == prior["legacy"], scenario_id
        assert item["v2"] == prior["v2"], scenario_id


@pytest.mark.live
@pytest.mark.skipif(
    os.environ.get("OMARCHY_EVAL_LIVE") != "1",
    reason="set OMARCHY_EVAL_LIVE=1 to call a live model",
)
@pytest.mark.skipif(
    not os.environ.get("OMARCHY_EVAL_API_KEY"), reason="OMARCHY_EVAL_API_KEY is not set"
)
def test_live_model_can_score_v2_ack():
    scenario = next(item for item in SCENARIOS if item.id == "ack_before_long_work")
    score = run_scenario("v2", scenario, live=True)
    assert score.passed, score.detail
