import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from omarchy_ai.execution import approval_context


class FakeProc:
    """A /proc with just what the lookup reads: stat, cmdline and cwd."""

    def __init__(self, root: Path):
        self.root = root

    def add(self, pid, parent, name, argv, cwd=None, started=None):
        folder = self.root / str(pid)
        folder.mkdir()
        fields = ["S", str(parent)] + ["0"] * 17 + [str(started if started is not None else pid)]
        (folder / "stat").write_text(f"{pid} ({name}) " + " ".join(fields) + "\n")
        (folder / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
        if cwd:
            os.symlink(cwd, folder / "cwd")


class SudoContextTests(unittest.TestCase):
    """2026-10-05: the phone showed "Approve sudo for user ... on host ..."
    for every request, whoever ran the command and whatever it was."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.home.mkdir()
        (Path(self.tmp.name) / "proc").mkdir()
        self.proc = FakeProc(Path(self.tmp.name) / "proc")
        self.proc.add(1, 0, "systemd", ["/sbin/init"])
        (self.proc.root / "uptime").write_text("1 1\n")   # not a process

    def context(self):
        return approval_context.sudo_context(self.proc.root, self.home)

    def waiting(self, parent, argv):
        self.proc.add(900, parent, "sudo", argv)
        self.proc.add(901, 900, "flux-approve", ["/usr/lib/flux/flux-approve"])

    def test_no_request_is_open(self):
        self.proc.add(50, 1, "bash", ["bash"])
        self.assertIsNone(self.context())

    def test_claude_code_gives_its_own_description_of_the_command(self):
        project = str(self.home / "Git" / "app")
        self.proc.add(40, 1, "claude", ["claude"], cwd=project)
        self.proc.add(41, 40, "bash", ["/bin/bash", "-c", "sudo pacman -S ffmpeg"])
        self.waiting(41, ["sudo", "pacman", "-S", "ffmpeg"])
        sessions = self.home / ".claude" / "projects" / project.replace("/", "-").replace(".", "-").replace("_", "-")
        sessions.mkdir(parents=True)

        def call(command, description):
            return json.dumps({"message": {"content": [{"type": "tool_use", "name": "Bash",
                                                        "input": {"command": command, "description": description}}]}})
        (sessions / "s1.jsonl").write_text("\n".join([
            call("sudo pacman -S vlc", "Install VLC"), call("ls", "List files"),
            call("sudo pacman -S ffmpeg", "Install ffmpeg for the clip script"), "not json"]))
        self.assertEqual(self.context(), {
            "found": True, "request": "pacman -S ffmpeg", "by": "claude", "by_label": "Claude Code",
            "why": "Install ffmpeg for the clip script", "where": "~/Git/app"})

    def test_codex_gives_its_justification_or_what_it_said_before_the_call(self):
        self.proc.add(40, 1, "codex", ["codex", "--yolo"], cwd=str(self.home))
        self.proc.add(41, 40, "bash", ["bash", "-lc", "sudo systemctl restart nginx"])
        self.waiting(41, ["sudo", "systemctl", "restart", "nginx"])
        folder = self.home / ".codex" / "sessions" / "2026" / "10" / "05"
        folder.mkdir(parents=True)
        said = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                     "content": [{"type": "output_text", "text": "Restarting nginx to load the new config."}]}}
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                "input": 'const r = await tools.exec_command({cmd:"sudo systemctl restart nginx"});'}}
        (folder / "rollout-a.jsonl").write_text(json.dumps(said) + "\n" + json.dumps(call) + "\n")
        found = self.context()
        self.assertEqual((found["by"], found["by_label"], found["where"]), ("codex", "Codex", "~"))
        self.assertEqual(found["why"], "Restarting nginx to load the new config.")
        call["payload"]["input"] = ('const r = await tools.exec_command({cmd:"sudo systemctl restart nginx", '
                                    'justification:"Do you want to restart nginx?"});')
        (folder / "rollout-a.jsonl").write_text(json.dumps(said) + "\n" + json.dumps(call) + "\n")
        self.assertEqual(self.context()["why"], "Do you want to restart nginx?")

    def test_a_sudo_inside_a_script_gets_the_reason_of_the_call_running_it(self):
        # 2026-10-05 to 10-07: every request was `true` from a test run, "No reason given".
        project = str(self.home / "Git" / "app")
        self.proc.add(40, 1, "claude", ["claude"], cwd=project)
        self.proc.add(41, 40, "bash", ["/bin/bash", "-c", "python -m unittest discover -s tests"])
        self.proc.add(42, 41, "python", ["python", "-m", "unittest", "discover", "-s", "tests"])
        self.waiting(42, ["sudo", "-S", "-p", "", "true"])
        sessions = self.home / ".claude" / "projects" / project.replace("/", "-").replace(".", "-").replace("_", "-")
        sessions.mkdir(parents=True)

        def call(uid, command, description):
            return json.dumps({"message": {"content": [{"type": "tool_use", "name": "Bash", "id": uid,
                                                        "input": {"command": command, "description": description}}]}})

        def done(uid):
            return json.dumps({"message": {"content": [{"type": "tool_result", "tool_use_id": uid}]}})
        lines = [call("a", "ls", "List files"), done("a"),
                 call("b", "python -m unittest discover -s tests", "Run the test suite"),
                 call("c", "sleep 60", "Wait for the build")]
        (sessions / "s1.jsonl").write_text("\n".join(lines))
        self.assertEqual(self.context()["why"], "Run the test suite")      # the open call that runs it
        (sessions / "s1.jsonl").write_text("\n".join(lines + [done("b"), done("c")]))
        self.assertEqual(self.context()["why"], "")                        # nothing of Claude's is running

    def test_codex_running_a_script_gives_its_newest_call(self):
        self.proc.add(40, 1, "codex", ["codex", "--yolo"], cwd=str(self.home))
        self.proc.add(41, 40, "bash", ["bash", "-lc", "./install.sh"])
        self.waiting(41, ["sudo", "pacman", "-S", "ffmpeg"])
        folder = self.home / ".codex" / "sessions" / "2026" / "10" / "07"
        folder.mkdir(parents=True)
        said = {"type": "response_item", "payload": {"type": "message", "role": "assistant",
                                                     "content": [{"type": "output_text", "text": "Running the installer."}]}}
        call = {"type": "response_item", "payload": {"type": "custom_tool_call", "name": "exec",
                "input": 'const r = await tools.exec_command({cmd:"./install.sh"});'}}
        (folder / "rollout-a.jsonl").write_text(json.dumps(said) + "\n" + json.dumps(call) + "\n")
        self.assertEqual(self.context()["why"], "Running the installer.")

    def test_omarchy_gives_the_task_it_noted_and_only_while_it_is_fresh(self):
        self.proc.add(40, 1, "python", ["/opt/venv/bin/python", "-m", "omarchy_ai"], cwd=str(self.home))
        self.proc.add(41, 40, "sh", ["sh", "-c", "sudo -S -p '' pacman -Syu"])
        self.waiting(41, ["sudo", "-S", "-p", "", "pacman", "-Syu"])
        with patch.dict(os.environ, {"XDG_RUNTIME_DIR": self.tmp.name}):
            approval_context.note("Task: update the system")
            found = self.context()
            self.assertEqual((found["request"], found["by"], found["why"]),
                             ("pacman -Syu", "omarchy", "Task: update the system"))
            later = time.time() + approval_context.NOTE_SECONDS + 1
            self.assertEqual(approval_context.sudo_context(self.proc.root, self.home, later)["why"], "")

    def test_a_command_the_user_typed_is_theirs_and_has_no_reason(self):
        self.proc.add(40, 1, "ghostty", ["ghostty"])
        self.proc.add(41, 40, "bash", ["bash"], cwd=str(self.home / "notes"))
        self.waiting(41, ["sudo", "-u", "postgres", "psql"])
        self.assertEqual(self.context(), {"found": True, "request": "psql", "by": "you", "by_label": "You",
                                          "why": "", "where": "~/notes"})

    def test_the_nearest_agent_is_the_one_asking(self):
        # A Codex worker that Omarchy AI started: Codex chose this command.
        self.proc.add(30, 1, "python", ["python", "-m", "omarchy_ai"])
        self.proc.add(40, 30, "codex", ["codex", "exec"])
        self.proc.add(41, 40, "bash", ["bash"])
        self.waiting(41, ["sudo", "true"])
        self.assertEqual(self.context()["by"], "codex")

    def test_an_agent_is_known_by_its_program_not_by_words_in_its_arguments(self):
        # Real tree: Claude Code's shell ran `bash -c "... cd ~/Git/omarchy-ai; python -m omarchy_ai.cli ..."`.
        self.proc.add(40, 1, "claude", ["/opt/claude/latest/claude"], cwd=str(self.home))
        self.proc.add(41, 40, "bash", ["/usr/bin/bash", "-c", "cd ~/Git/omarchy_ai && sudo true # omarchy-ai codex"])
        self.waiting(41, ["sudo", "true"])
        self.assertEqual(self.context()["by"], "claude")

    def test_the_newest_of_two_requests_and_odd_commands(self):
        self.proc.add(41, 1, "bash", ["bash"])
        self.proc.add(700, 41, "sudo", ["sudo", "ls"], started=10)
        self.proc.add(701, 700, "flux-approve", ["flux-approve"], started=11)
        self.waiting(41, ["sudo", "-i"])
        self.assertEqual(self.context()["request"], "(a root shell)")
        self.assertEqual(approval_context.command_of(["sudo", "--", "sh", "-c", "echo a; rm -rf /x"]),
                         "sh -c 'echo a; rm -rf /x'")
        self.assertLessEqual(len(approval_context.command_of(["sudo", "echo", "x" * 900])), approval_context.MAX_TEXT)


if __name__ == "__main__":
    unittest.main()
