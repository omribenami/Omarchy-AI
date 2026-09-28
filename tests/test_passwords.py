"""Passwords never land in files or logs, and SSH with a password runs
(real case 2026-09-27 18:55: a spoken SSH password was stored in plain text in
conversation_history.jsonl, and she still could not SSH)."""
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core import history
from omarchy_ai.execution import askpass, passwords
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.runtime.permissions import Risk, Scope, classify
from omarchy_ai.runtime.task import Task, TaskStore

SECRET = "<fake-password>"


class KeyringFree(unittest.TestCase):
    """No test touches the real GNOME Keyring."""

    def setUp(self):
        self.known = []
        self.stored = []
        for target, value in [("known_secrets", lambda: list(self.known)),
                              ("store_spoken", lambda pw: self.stored.append(pw) or True),
                              ("spoken_password", lambda: self.stored[-1] if self.stored else None)]:
            patcher = patch.object(passwords, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)


class SpokenPasswordTests(KeyringFree):
    def test_finds_stated_passwords_only(self):
        cases = {"User: user Pass: <fake-password>": ["<fake-password>"], "the password is hunter22": ["hunter22"],
                 "סיסמה: abc123": ["abc123"], "my pwd=Xy9!zz": ["Xy9!zz"],
                 "same password as root": [], "the password for that": [], "password manager": [],
                 "passwords are fine": [], "password abc": []}
        for text, expected in cases.items():
            self.assertEqual(passwords._spoken_values(text), expected, text)

    def test_capture_stores_it(self):
        self.assertTrue(passwords.capture_spoken("User: user Pass: <fake-password>"))
        self.assertEqual(self.stored, [SECRET])

    def test_redacts_saved_passwords_also_json_escaped(self):
        self.known = ['pa"ss99']
        self.assertEqual(passwords.redact(json.dumps({"x": 'use pa"ss99 now'})), '{"x": "use [password] now"}')

    def test_history_joins_fragments_then_masks(self):
        turns = [{"role": "user", "text": "User: user Pass: Xq7-fa"}, {"role": "user", "text": "ke-Pw!"},
                 {"role": "assistant", "text": "I cannot use <fake-password> directly"}]
        self.known = [SECRET]
        saved = history._redacted(turns)
        self.assertEqual(len(saved), 2)
        self.assertNotIn(SECRET, json.dumps(saved))
        self.assertIn("[password]", saved[0]["text"])

    def test_task_files_mask_saved_passwords(self):
        self.known = [SECRET]
        with tempfile.TemporaryDirectory() as directory:
            store = TaskStore(Path(directory))
            task = Task(id="t1", goal=f"ssh with {SECRET}", workspace=directory)
            store.save(task)
            self.assertNotIn(SECRET, store.path("t1").read_text())

    def test_log_filter_masks(self):
        self.known = [SECRET]
        record = logging.LogRecord("x", logging.INFO, __file__, 1, "args=%s", ({"command": f"sshpass -p {SECRET}"},), None)
        passwords.RedactingFilter().filter(record)
        self.assertNotIn(SECRET, record.getMessage())


class SshTests(KeyringFree):
    def test_literal_sshpass_password_is_stripped_and_kept(self):
        command = passwords.sanitize_command(f"sshpass -p '{SECRET}' ssh-copy-id -i k.pub user@192.0.2.10")
        self.assertEqual(command, "sshpass ssh-copy-id -i k.pub user@192.0.2.10")
        self.assertEqual(self.stored, [SECRET])

    def test_a_model_literal_never_replaces_the_users_password(self):
        self.stored = [SECRET]
        passwords.sanitize_command("sshpass -p 'made-up-99' ssh user@h true")
        self.assertEqual(self.stored, [SECRET])

    def test_harness_answers_the_prompt(self):
        with patch.object(passwords, "askpass_helper", return_value="/run/askpass.sh"):
            command, env = passwords.ssh_env("sshpass ssh user@h 'echo ok'", {"PATH": "/usr/bin"})
            self.assertEqual(command, "ssh user@h 'echo ok'")
            self.assertEqual((env["SSH_ASKPASS"], env["SSH_ASKPASS_REQUIRE"]), ("/run/askpass.sh", "force"))
            self.assertEqual(passwords.ssh_env("ls -la", {"A": "1"}), ("ls -la", {"A": "1"}))
            typed = passwords.ssh_terminal_command(f"sshpass -p {SECRET} ssh-copy-id user@h")
        self.assertNotIn(SECRET, typed)
        self.assertTrue(typed.startswith("SSH_ASKPASS=/run/askpass.sh SSH_ASKPASS_REQUIRE=force"))
        self.assertTrue(typed.endswith("ssh-copy-id user@h"))

    def test_askpass_answers(self):
        self.stored = [SECRET]
        self.assertEqual(askpass.answer("user@192.0.2.10's password: "), SECRET)
        self.assertEqual(askpass.answer("Are you sure you want to continue connecting (yes/no/[fingerprint])? "), "yes")
        self.assertIsNone(askpass.answer("Enter passphrase for key '/home/x/.ssh/id_ed25519': "))

    def test_password_logins_need_the_users_ok(self):
        scope = Scope(workspaces=[Path.home()])
        for command in ("sshpass ssh user@h true", "ssh-copy-id -i k.pub user@h"):
            assessment = classify(command, str(Path.home()), scope)
            self.assertEqual(assessment.risk, Risk.ELEVATED, command)
            self.assertIn("user@h", " ".join(assessment.reasons))
        self.assertEqual(classify("ssh user@h true", str(Path.home()), scope).risk, Risk.NORMAL)

    def test_terminal_sudo_gives_a_login_prompt_the_ssh_password(self):
        from omarchy_ai.execution import actions, workbench
        self.stored = [SECRET]
        with patch.object(workbench, "exists", return_value=True), \
                patch.object(workbench, "read", return_value=ActionResult(True, "$ ssh-copy-id user@h\nuser@h's password: ")), \
                patch.object(workbench, "submit_password", return_value=ActionResult(True, "ok")) as submit:
            self.assertTrue(actions.terminal_sudo({"name": "ssh-ha"}).ok)
        submit.assert_called_once_with("ssh-ha", SECRET)


if __name__ == "__main__":
    unittest.main()


from omarchy_ai.runtime import shell  # noqa: E402
from omarchy_ai.runtime.executors.base import DONE, NEEDS_APPROVAL, Report  # noqa: E402
from test_task_runtime import RuntimeHarness, ScriptedJev, ScriptExecutor  # noqa: E402


class RuntimeSshTests(RuntimeHarness):
    def setUp(self):
        super().setUp()
        self.stored = []
        for target, value in [("known_secrets", lambda: [SECRET] if self.stored else []),
                              ("store_spoken", lambda pw: self.stored.append(pw) or True),
                              ("spoken_password", lambda: self.stored[-1] if self.stored else None),
                              ("askpass_helper", lambda: "/run/askpass.sh")]:
            patcher = patch.object(passwords, target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_worker_sshpass_runs_after_approval_without_the_password_anywhere(self):
        ran = []

        def fake_run(command, cwd=None, **kw):
            ran.append((command, kw.get("env") or {}))
            return shell.CommandResult(command if isinstance(command, str) else " ".join(command), str(cwd), 0,
                                       "Number of key(s) added: 1" if "ssh" in str(command) else "", 0.1)

        def work(a, ctx):
            r = ctx.run_command(f"sshpass -p '{SECRET}' ssh-copy-id -i k.pub user@192.0.2.10", a.workspace, 10)
            if r["decision"] == "ask":
                return Report(NEEDS_APPROVAL, claim="need to log in", approval=r["request"])
            return Report(DONE, claim=r["output"])

        runtime = self.runtime([ScriptExecutor("SYSTEM_AGENT", work)], ScriptedJev())
        with patch.object(shell, "run", side_effect=fake_run):
            task = runtime.start("install my key on HA", str(self.ws), background=False)
            waiting = self.store.load(task.id)
            self.assertEqual(waiting.pending_approval["risk"], "ELEVATED")
            self.assertEqual(waiting.pending_approval["subject"], "sshpass ssh-copy-id -i k.pub user@192.0.2.10")
            self.assertTrue(runtime.respond(task.id, approve=True, channel="voice", background=False)["ok"])
        ssh = [(c, env) for c, env in ran if isinstance(c, str) and "ssh-copy-id" in c]
        self.assertEqual([c for c, _ in ssh], ["ssh-copy-id -i k.pub user@192.0.2.10"])
        self.assertEqual(ssh[0][1]["SSH_ASKPASS_REQUIRE"], "force")
        self.assertEqual(self.stored, [SECRET])
        self.assertNotIn(SECRET, self.store.path(task.id).read_text())
