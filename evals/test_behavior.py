"""Deterministic behavior scores for the legacy prompt and prompt v2.

A cell with an xfail reason is a known gap (strict: the day the harness or
prompt fixes it, the cell fails until its marker is removed here and the
baseline is regenerated with ``python -m evals compare --update``).
"""

from __future__ import annotations

import os

import pytest

from evals.suite import SCENARIOS, run_scenario

_PROMPTS = ("legacy", "v2")


def _cells():
    for scenario in SCENARIOS:
        for prompt in _PROMPTS:
            reason = scenario.xfail.get(prompt)
            marks = [pytest.mark.xfail(strict=True, reason=reason)] if reason else []
            yield pytest.param(
                prompt, scenario, id=f"{scenario.id}-{prompt}", marks=marks
            )


@pytest.mark.parametrize(("prompt", "scenario"), list(_cells()))
def test_scenario(prompt, scenario):
    score = run_scenario(prompt, scenario)
    assert score.passed, score.detail


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
