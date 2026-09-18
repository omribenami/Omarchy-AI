#!/usr/bin/env python3
"""Read real OS state and query Jev without executing any desktop action."""
import argparse
import json

from omarchy_ai.config import load_config
from omarchy_ai.execution.desktop_jev import DesktopLoop
from omarchy_ai.voice.omarchy import GatewayClient

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("goal", help="A goal to evaluate only; never executed")
args = parser.parse_args()
client = GatewayClient(load_config())
result = DesktopLoop(client.evaluate_questions).run(args.goal, dry_run=True)
print(json.dumps(json.loads(result.message), indent=2, ensure_ascii=False))
