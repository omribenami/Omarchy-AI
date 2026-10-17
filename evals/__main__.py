"""Run the behavior evals.

uv run python -m evals run --prompt both
uv run python -m evals compare [--update]
OMARCHY_EVAL_LIVE=1 OMARCHY_EVAL_API_KEY=... uv run python -m evals run --live --prompt v2
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .suite import SCENARIOS, markdown_table, scorecard

BASELINE = Path(__file__).parent / "baselines" / "deterministic.json"


def _rows(live: bool) -> dict[str, dict]:
    return {
        "legacy": {
            score.scenario_id: score for score in scorecard("legacy", live=live)
        },
        "v2": {score.scenario_id: score for score in scorecard("v2", live=live)},
    }


def _dump(rows: dict) -> dict:
    body = {"mode": "deterministic", "scenarios": {}}
    for scenario in SCENARIOS:
        body["scenarios"][scenario.id] = {
            "title": scenario.title,
            "legacy": rows["legacy"][scenario.id].status,
            "v2": rows["v2"][scenario.id].status,
        }
    return body


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="score one or both prompts")
    run.add_argument("--prompt", choices=["legacy", "v2", "both"], default="both")
    run.add_argument(
        "--live", action="store_true", help="use a live model (OMARCHY_EVAL_LIVE=1)"
    )
    compare = sub.add_parser(
        "compare", help="print legacy vs v2 and check the committed baseline"
    )
    compare.add_argument(
        "--update", action="store_true", help="rewrite the baseline with these statuses"
    )
    args = parser.parse_args(argv)

    if args.command == "run":
        if args.live and __import__("os").environ.get("OMARCHY_EVAL_LIVE") != "1":
            print("refusing --live without OMARCHY_EVAL_LIVE=1", file=sys.stderr)
            return 2
        prompts = ["legacy", "v2"] if args.prompt == "both" else [args.prompt]
        for prompt in prompts:
            print(f"## {prompt}")
            for score in scorecard(prompt, live=args.live):
                print(f"{score.status:5}  {score.scenario_id}: {score.detail}")
        return 0

    rows = _rows(False)
    print(markdown_table(rows))
    actual = _dump(rows)
    if args.update:
        BASELINE.write_text(json.dumps(actual, indent=2) + "\n")
        print(f"\nwrote {BASELINE}")
        return 0
    if not BASELINE.is_file():
        print(f"\nno baseline at {BASELINE}", file=sys.stderr)
        return 1
    expected = json.loads(BASELINE.read_text())
    mismatches = []
    for scenario_id, item in actual["scenarios"].items():
        prior = expected["scenarios"].get(scenario_id, {})
        for prompt in ("legacy", "v2"):
            if prior.get(prompt) != item[prompt]:
                mismatches.append(
                    f"{scenario_id} {prompt}: baseline {prior.get(prompt)} now {item[prompt]}"
                )
    if mismatches:
        print("\nbaseline drift:", file=sys.stderr)
        print("\n".join(mismatches), file=sys.stderr)
        return 1
    print("\nmatches baselines/deterministic.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
