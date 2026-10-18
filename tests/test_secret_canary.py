"""No secret reaches a model, TTS, the phone, a log or a state file (plan 2.5).

A canary value made at run time is seeded into each secret source the
harness knows -- the saved Sudo Access password, a password the user said,
a MyApi vault value handed to a program, an API key file -- and the flows
that carry text outward are driven with fakes: a tool result (a MyApi
answer containing a vault token), terminal_sudo at an SSH prompt, a vault
fetch, a Gateway chat turn and a TTS reply, the task model's prompt and a
command's saved full output, a phone push and a phone reply, an agenda step
and its inbox, a task result, the conversation history, and the effect
journal. Then every recorded outbound payload, every log sink (stderr and
JSONL) and every file under the temporary state directory is searched: no
canary may appear in any of them. The flows that legitimately hand a secret
on (terminal_sudo typing it at the prompt, vault.get returning it to a
program) are checked to still do so.
"""

from __future__ import annotations

import io
import json
import logging
import secrets
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.core import agenda, conversations, effects, history, logs
from omarchy_ai.execution import passwords as masking
from omarchy_ai.execution import sudo_approval, workbench
from omarchy_ai.execution.actions import ActionResult
from omarchy_ai.execution.runner import ToolRunner
from omarchy_ai.myapi import vault
from omarchy_ai.runtime import shell
from omarchy_ai.runtime.llm import WorkerModel
from omarchy_ai.runtime.task import Task, TaskStore
from omarchy_ai.voice import omarchy


def _canary(tag: str) -> str:
    # Made at run time: no secret-looking literal is committed.
    return f"cnry{tag}{secrets.token_hex(8)}"


def _reset_cache() -> None:
    masking._cache = (0.0, [])
    masking._loaded = False
    masking._refreshing = False


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SecretCanaryTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="omarchy-canary-"))
        self.addCleanup(__import__("shutil").rmtree, self.root, ignore_errors=True)
        self.sudo, self.spoken, self.vault_value, self.api_key = (
            _canary("sudo"),
            _canary("spk"),
            _canary("vlt"),
            _canary("key"),
        )
        self.canaries = [self.sudo, self.spoken, self.vault_value, self.api_key]
        key_file = self.root / "config" / "vercel-ai-gateway-key"
        key_file.parent.mkdir(parents=True)
        key_file.write_text(self.api_key + "\n")
        state = self.root / "state"
        state.mkdir()
        self.outbound: list[str] = []  # every payload that left: models, TTS, phone
        patches = [
            patch.object(sudo_approval, "retrieve", self._saved),
            patch.object(masking, "spoken_password", self._stated),
            patch.object(vault, "revealed_values", lambda: [self.vault_value]),
            patch.object(masking, "key_files", lambda: [key_file]),
            patch.object(effects, "journal_path", lambda: state / "effects.jsonl"),
            patch.object(effects, "index_path", lambda: state / "effects_index.json"),
            patch.object(history, "HISTORY_PATH", state / "conversation_history.jsonl"),
            patch.object(history, "STATE_DIR", state),
            patch.object(conversations, "DIR", state / "conversations"),
            patch.object(conversations, "_cache", {}),
            patch.object(agenda, "INBOX_PATH", state / "agenda_inbox.jsonl"),
            patch.object(agenda, "JOBS_PATH", state / "agenda.json"),
            patch.object(agenda, "listeners", []),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        _reset_cache()
        self.addCleanup(_reset_cache)
        self.state = state
        self.stderr = io.StringIO()
        root = logging.getLogger()
        level = root.level
        logs.setup(
            logging.INFO,
            fmt="%(levelname)s %(name)s %(message)s",
            jsonl_path=self.root / "logs" / "omarchy-ai.jsonl",
            stream=self.stderr,
            journal=False,
        )
        self.addCleanup(root.setLevel, level)
        self.addCleanup(logs.shutdown)
        self.log = logging.getLogger("omarchy_ai.test.canary")

    def _saved(self):
        return self.sudo

    def _stated(self):
        return self.spoken

    # ------------------------------------------------------------ flows

    def _tool_result(self):
        """A tool that prints secrets (a MyApi answer with a vault token)."""

        def execute(name, args):
            self.log.info("tool call: %s(%s)", name, args)
            return ActionResult(
                True,
                json.dumps(
                    {
                        "data": {"token": self.vault_value, "key": self.api_key},
                        "note": f"sudo={self.sudo} ssh={self.spoken}",
                    }
                ),
            )

        result = ToolRunner().run("myapi_execute", {"service": "x"}, execute=execute)
        self.log.info("tool result: ok=%s message=%r", result.ok, result.message)
        self.outbound.append(result.message)  # what the model is told
        self.assertTrue(result.ok)
        self.assertIn(masking.MASK, result.message)

    def _terminal_sudo(self):
        typed: list[str] = []
        from omarchy_ai.execution.actions.terminal import terminal_sudo

        with (
            patch.object(workbench, "exists", lambda name: True),
            patch.object(
                workbench,
                "read",
                lambda *a, **k: ActionResult(True, "user@host's password:"),
            ),
            patch.object(
                workbench,
                "submit_password",
                lambda name, pw: typed.append(pw) or ActionResult(True, f"typed {pw}"),
            ),
        ):
            result = ToolRunner().run(
                "terminal_sudo", {"name": "t1"}, execute=lambda n, a: terminal_sudo(a)
            )
        self.outbound.append(result.message)
        self.assertEqual(typed, [self.spoken])  # the prompt still gets it

    def _sanitized_command(self):
        stated = _canary("cmd")
        self.canaries.append(stated)
        stored: list[str] = []
        with (
            patch.object(masking, "spoken_password", lambda: None),
            patch.object(masking, "store_spoken", lambda pw: stored.append(pw)),
        ):
            command = masking.sanitize_command(f"sshpass -p {stated} ssh host")
        self.log.info("running %s", command)
        self.outbound.append(command)
        self.assertEqual(stored, [stated])

    def _vault_fetch(self):
        class Client:
            def request(self_, method, path, **kw):
                if path.endswith("/reveal"):
                    return {"data": {"token": self.vault_value}}
                return {
                    "data": [{"id": "vt_1", "label": "github", "service": "github"}]
                }

        with (
            patch.object(vault, "_keyring_get", lambda name: None),
            patch.object(vault, "_keyring_put", lambda name, value: None),
            patch.object(
                vault, "_revealed_path", lambda: self.state / "vault_revealed.json"
            ),
            patch.dict(vault._cache, clear=True),
        ):
            value = vault.get("github", Client())
            self.log.info("vault: gave %s to a program", value)
        self.assertEqual(value, self.vault_value)  # the program still gets it

    def _gateway(self):
        """A chat turn, a Jev question and a TTS reply, as sent on the wire."""

        def urlopen(request, timeout=None):
            self.outbound.append(request.data.decode())
            if "speech-model" in request.full_url:
                return _Response(b'{"audio": "AAA="}')
            if "evaluation" in request.full_url:
                return _Response(b'{"answers": {}}')
            return _Response(b'{"choices": [{"message": {"content": "{}"}}]}')

        client = omarchy.GatewayClient.__new__(omarchy.GatewayClient)
        client.config = SimpleNamespace(
            omarchy_tts_voice="alloy",
            omarchy_tts_model="tts",
            omarchy_text_model="m",
            omarchy_model_choice="gemini",
        )
        client.key = "gateway-header"
        with patch.object(omarchy.urllib.request, "urlopen", urlopen):
            client.chat(
                [
                    {"role": "user", "content": f"use {self.api_key}"},
                    {"role": "tool", "content": f"token {self.vault_value}"},
                ]
            )
            client.complete_json("plan", {"output": f"sudo {self.sudo}"})
            client.speech(f"Your token is {self.vault_value}. Pass: {self.spoken}")
        self.assertEqual(len(self.outbound) >= 3, True)

    def _task_model_and_output(self):
        seen: list[dict] = []
        model = WorkerModel(
            "m",
            transport=lambda payload, timeout: (
                seen.append(payload) or {"choices": [{"message": {"content": "{}"}}]}
            ),
        )
        model.complete(f"system {self.sudo}", {"output": f"printed {self.vault_value}"})
        self.outbound += [json.dumps(p) for p in seen]
        saved = shell._save_full(f"line\n{self.api_key}\n", self.state / "tasks" / "t")
        self.assertIsNotNone(saved)
        store = TaskStore(self.state / "tasks")
        task = Task(id="t1", goal=f"deploy with {self.sudo}", workspace=str(self.root))
        task.add_evidence("output", f"got {self.vault_value}", ok=True, step=1)
        store.save(task)

    def _phone(self):
        from omarchy_ai.phone import flux_notify
        from omarchy_ai.phone.server import _Handler

        calls: list[dict] = []

        class Fluxd:
            def __init__(self, timeout=None):
                pass

            def call(self, method, params, timeout=None):
                if method == "state":
                    return {"devices": [{"id": "d", "paired": True, "online": True}]}
                calls.append(params)
                return {}

            def close(self):
                pass

        with patch("omarchy_ai.execution.flux_approve.Fluxd", Fluxd):
            flux_notify.deliver(f"Task done {self.sudo}", f"token {self.vault_value}")
        self.outbound += [json.dumps(c) for c in calls]
        self.assertEqual(len(calls), 1)

        handler = _Handler.__new__(_Handler)
        handler.wfile = io.BytesIO()
        handler.send_response = handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        handler._send_json(200, {"ok": True, "message": f"key {self.api_key}"})
        self.outbound.append(handler.wfile.getvalue().decode())

    def _agenda(self):
        heard: list[dict] = []
        agenda.listeners.append(heard.append)
        job = {"id": "j1", "title": "check", "kind": "action", "announce": ""}
        agenda._inbox_add(job, "done", f"result {self.vault_value}")
        agenda.task_result("t1", "goal", "done", f"printed {self.sudo}")
        with patch(
            "omarchy_ai.execution.actions.run_action",
            lambda tool, args: ActionResult(True, f"answer {self.api_key}"),
        ):
            agenda._call(
                {"tool": "myapi_execute", "args": {"q": 1}},
                origin="run:1:1",
                job_id="j1",
            )
        self.outbound += [json.dumps(e) for e in heard]
        self.assertEqual(len(heard), 1)

    def _conversation(self):
        cid = conversations.create("phone")
        conversations.add_line(cid, "user", f"my password is {self.spoken}")
        conversations.add_line(cid, "assistant", f"the token is {self.vault_value}")
        conversations.flush()
        history.append_session(
            [
                {"role": "user", "text": f"pass: {self.spoken}"},
                {"role": "assistant", "text": f"sudo is {self.sudo}"},
            ],
            24,
        )
        self.outbound.append(masking.for_speech(f"typed: password: {self.spoken}"))

    def _logged_failure(self):
        try:
            raise RuntimeError(f"auth failed with {self.api_key}")
        except RuntimeError:
            self.log.exception("call failed for %s", self.sudo)

    # ------------------------------------------------------------ the test

    def test_no_canary_reaches_any_sink(self):
        flows = [
            self._tool_result,
            self._terminal_sudo,
            self._sanitized_command,
            self._vault_fetch,
            self._gateway,
            self._task_model_and_output,
            self._phone,
            self._agenda,
            self._conversation,
            self._logged_failure,
        ]
        for flow in flows:
            with self.subTest(flow=flow.__name__):
                flow()
        for handler in logging.getLogger().handlers:
            handler.flush()

        sinks = {f"outbound[{i}]": text for i, text in enumerate(self.outbound)}
        sinks["stderr log"] = self.stderr.getvalue()
        for path in sorted(self.root.rglob("*")):
            if path.is_file() and path.name != "vercel-ai-gateway-key":
                sinks[str(path.relative_to(self.root))] = path.read_text(
                    errors="replace"
                )
        # The sinks this test means to cover were all written.
        for name in (
            "logs/omarchy-ai.jsonl",
            "state/effects.jsonl",
            "state/effects_index.json",
            "state/agenda_inbox.jsonl",
            "state/conversation_history.jsonl",
        ):
            self.assertIn(name, sinks)
        self.assertTrue(any(k.startswith("state/conversations/") for k in sinks))
        self.assertTrue(any(k.startswith("state/tasks/") for k in sinks))
        self.assertIn(masking.MASK, sinks["stderr log"])

        leaks = [
            (name, canary[:8])
            for name, text in sinks.items()
            for canary in self.canaries
            if canary in text
        ]
        self.assertEqual(leaks, [])


class RotateOnLeakTests(unittest.TestCase):
    def test_names_every_source_and_the_copies_to_delete(self):
        text = masking.rotate_on_leak()
        for word in (
            "Sudo Access",
            "secret-tool clear",
            "omarchy-ai-vault get --fresh",
            "API key",
            "effects.jsonl",
        ):
            self.assertIn(word, text)
        self.assertNotIn("MyApi vault token", masking.rotate_on_leak(["sudo"]))


if __name__ == "__main__":
    unittest.main()
