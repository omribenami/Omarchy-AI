"""omarchy-ai-tool: tools the assistant writes for herself (execution/user_tools.py).

    python -m omarchy_ai.cli.tools format             how to write a tool
    python -m omarchy_ai.cli.tools check <dir>        validate + run its test
    python -m omarchy_ai.cli.tools propose <dir>      stage it for approval
    python -m omarchy_ai.cli.tools install <name>     ALWAYS asks the user first
    python -m omarchy_ai.cli.tools run <name> [json]  run an approved tool
    python -m omarchy_ai.cli.tools list | remove <name>
"""
from __future__ import annotations

import json
import sys

from ..execution import user_tools

FORMAT = f"""A tool is a folder with three files:

  tool.json  {{"name": "snake_case_name", "description": "What it does and when to use it (20-1000 chars).",
              "parameters": {{"type": "object", "properties": {{"title": {{"type": "string", "description": "..."}}}},
                             "required": ["title"]}}}}
  run        executable (any language, with a shebang). Reads its JSON arguments on stdin. Prints the result for
             the assistant to read (keep it short). Exit 0 = success, non-zero = failure (print why).
  test       executable. Exits 0 only if the tool really works (use a dry-run or read-only path for anything
             public or destructive: a test must never post, send or delete for real).

Rules: do one job well; take everything specific (repo, title, file) as parameters rather than hard-coding it;
never write into the tool's own folder at run time (approval pins every file's hash); use logged-in CLIs (gh,
git, ...) rather than stored secrets. Then `propose <dir>` and `install <name>`: install asks the user, and once
approved the tool is usable immediately by the voice assistant and by `run <name> '<json>'`.
Installed tools live in {user_tools.TOOLS_DIR}."""


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    verb = args.pop(0) if args else "help"
    if verb == "format":
        print(FORMAT)
        return 0
    if verb == "check" and args:
        spec, problems = user_tools.validate(args[0])
        if problems:
            print("Problems: " + "; ".join(problems))
            return 1
        passed, output = user_tools._test(user_tools.Path(args[0]).expanduser().resolve())
        print(("Test passed." if passed else "Test FAILED.") + (f"\n{output}" if output else ""))
        return 0 if passed else 1
    if verb in ("propose", "install", "remove") and args:
        ok, message = getattr(user_tools, verb)(args[0])
        print(message)
        return 0 if ok else 1
    if verb == "run" and args:
        try:
            payload = json.loads(args[1]) if len(args) > 1 else {}
        except ValueError as exc:
            print(f"arguments must be JSON: {exc}")
            return 2
        result = user_tools.run(args[0], payload)
        print(result.message)
        return 0 if result.ok else 1
    if verb == "list":
        for tool in user_tools.installed():
            print(f"{tool['name']} (approved {tool['approved_at']}): {tool['description']}")
        proposed = sorted(p.name for p in user_tools.PROPOSED_DIR.glob("*") if p.is_dir())
        if proposed:
            print("Waiting for approval: " + ", ".join(proposed))
        return 0
    print(__doc__)
    return 0 if verb == "help" else 2


if __name__ == "__main__":
    sys.exit(main())
