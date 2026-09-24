"""omarchy-ai-task: run and manage Task Runtime tasks from a terminal.

    omarchy-ai-task run "why does my bluetooth keep disconnecting"
    omarchy-ai-task run -C ~/Git/myservice "fix the crash in the logs and test it"
    omarchy-ai-task list | show [ID] | approve [ID] | deny [ID] | answer [ID] TEXT
    omarchy-ai-task resume ID | cancel [ID] | executors | classify "COMMAND"

A task started here runs in this process (foreground) and prints its
progress. Tasks started by voice run inside the daemon; approving one here
resumes it in this process. State lives in ~/.local/state/omarchy-ai/tasks/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time


def _print_task(task, verbose: bool = False) -> None:
    print(f"{task.id}  [{task.status}]  {task.goal[:100]}")
    print(f"  workspace: {task.workspace}   phase: {task.phase}   steps: {len(task.steps)}")
    if task.objective:
        print(f"  objective: {task.objective}")
    for c in task.plan:
        print(f"    - {c}")
    for step in task.steps:
        print(f"  #{step['n']} {step['executor']} ({step['role']}) -> {step['outcome']}")
        if verbose and step.get("claim"):
            print("     " + step["claim"][:600].replace("\n", "\n     "))
    if verbose:
        for d in task.decisions[-12:]:
            p = f"{d['p']:.2f}" if isinstance(d.get("p"), (int, float)) else "-"
            print(f"  jev {d['kind']}: {d['value']} (p={p})")
        for c in task.commands[-15:]:
            print(f"  $ {c['command'][:140]}  [{c.get('risk')}/{c.get('decision')}] exit={c.get('exit_code')}")
    if task.files_modified:
        print(f"  files: {', '.join(task.files_modified[:12])}")
    if task.pending_approval:
        r = task.pending_approval
        print(f"  NEEDS APPROVAL ({r.get('risk')}): {r.get('subject')}\n    {'; '.join(r.get('reasons', []))}")
        print(f"    -> omarchy-ai-task approve {task.id}   |   omarchy-ai-task deny {task.id}")
    if task.question:
        print(f"  QUESTION: {task.question}\n    -> omarchy-ai-task answer {task.id} \"...\"")
    if task.result:
        print(f"  result: {task.result}")


def _follow(runtime, task_id: str) -> int:
    """Print new steps/commands as they happen until the task stops."""
    seen_steps = seen_cmds = 0
    while True:
        task = runtime.store.load(task_id)
        if task is None:
            return 1
        for c in task.commands[seen_cmds:]:
            print(f"    $ {c['command'][:160]}  [{c.get('risk')}/{c.get('decision')}]"
                  + (f" exit={c['exit_code']}" if c.get("exit_code") is not None else ""))
        seen_cmds = len(task.commands)
        for s in task.steps[seen_steps:]:
            print(f"  -> #{s['n']} {s['executor']} ({s['role']}) {s.get('directive') or ''}")
        seen_steps = len(task.steps)
        thread = runtime._threads.get(task_id)
        if thread is None or not thread.is_alive():
            print()
            _print_task(runtime.store.load(task_id))
            return 0 if task.status == "certified" else 2
        time.sleep(0.5)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="omarchy-ai-task", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="start a task and follow it")
    run.add_argument("goal")
    run.add_argument("-C", "--workspace", default=None)
    run.add_argument("--auto-approve", choices=["LOW", "NORMAL", "ELEVATED"], default=None)
    run.add_argument("--max-steps", type=int, default=None)
    sub.add_parser("list")
    for name in ("show", "approve", "deny", "cancel", "resume"):
        p = sub.add_parser(name)
        p.add_argument("id", nargs="?")
        if name == "show":
            p.add_argument("--json", action="store_true")
    answer = sub.add_parser("answer")
    answer.add_argument("id", nargs="?")
    answer.add_argument("text")
    sub.add_parser("executors")
    classify = sub.add_parser("classify", help="show the risk the harness assigns to a command")
    classify.add_argument("command")
    classify.add_argument("-C", "--workspace", default=None)
    args = parser.parse_args(argv)

    if args.cmd == "classify":
        from pathlib import Path
        from ..runtime.permissions import Scope, classify as risk
        ws = Path(args.workspace or ".").resolve()
        print(json.dumps(risk(args.command, ws, Scope(workspaces=[ws])).as_dict()))
        return 0

    import logging
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    from ..runtime.runtime import TaskRuntime
    runtime = TaskRuntime()

    if args.cmd == "executors":
        print(json.dumps(runtime.available(refresh=True), indent=1))
        return 0
    if args.cmd == "list":
        for task in runtime.store.list(20):
            print(f"{task.id}  {task.status:17} {task.goal[:90]}")
        return 0
    if args.cmd == "show":
        task = runtime.store.load(args.id) if args.id else runtime.store.latest()
        if not task:
            print("no such task", file=sys.stderr)
            return 1
        if args.json:
            from dataclasses import asdict
            print(json.dumps(asdict(task), indent=1, ensure_ascii=False))
        else:
            _print_task(task, verbose=True)
        return 0
    if args.cmd == "run":
        task = runtime.start(args.goal, args.workspace, source="cli", auto_approve=args.auto_approve,
                             max_steps=args.max_steps)
        print(f"task {task.id} started in {task.workspace}")
        return _follow(runtime, task.id)
    if args.cmd == "cancel":
        print(runtime.cancel(args.id)["message"])
        return 0
    if args.cmd == "resume":
        result = runtime.resume(args.id)
        print(result["message"])
        # Follow the task actually resumed (args.id may be omitted): leaving
        # early would end this process and orphan the task mid-step.
        return _follow(runtime, result["task_id"]) if result["ok"] else 1
    if args.cmd in ("approve", "deny", "answer"):
        result = runtime.respond(args.id, approve={"approve": True, "deny": False}.get(args.cmd),
                                 answer=getattr(args, "text", None), channel="cli")
        print(result["message"])
        if not result["ok"]:
            return 1
        return _follow(runtime, result["task"]["id"])
    return 1


if __name__ == "__main__":
    sys.exit(main())
