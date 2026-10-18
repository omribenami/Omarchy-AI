"""omarchy-ai-doctor (cli/doctor.py): every check against fakes, --json,
--trace, --bundle, and the run_diagnostics tool."""

import io
import json
import os
from pathlib import Path
import secrets
import tarfile
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from omarchy_ai.cli import doctor
from omarchy_ai.cli.doctor import GREEN, RED, YELLOW, Completed, Context, Response
from omarchy_ai.core import health

WPCTL_STATUS = """PipeWire 'pipewire-0' [1.6.8]
 └─ Clients:
        32. WirePlumber                         [1.6.8]

Audio
 ├─ Devices:
 │      51. Built-in Audio                      [alsa]
 │
 ├─ Sinks:
 │  *   57. Built-in Audio Analog Stereo        [vol: 1.00]
 │
 ├─ Sources:
 │  *   58. Built-in Audio Analog Stereo        [vol: 0.35]
 │
 ├─ Filters:
 │
 └─ Streams:
        66. pw-record

Video
 ├─ Sources:
 │  *   60. Integrated Camera (V4L2)
"""
UNIT = {
    "LoadState": "loaded",
    "ActiveState": "active",
    "SubState": "running",
    "NRestarts": "0",
    "Type": "notify",
    "WatchdogUSec": "30s",
    "StatusText": "Running",
    "MainPID": "42",
    "WorkingDirectory": "",
}


class FakeRunner:
    """Answers commands from a table; anything else is a missing program."""

    def __init__(self, table=None):
        self.table = {
            ("wpctl", "status"): Completed(0, WPCTL_STATUS),
            ("wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"): Completed(
                0, "Volume: 1.00\n"
            ),
            ("wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@"): Completed(
                0, "Volume: 0.35\n"
            ),
            ("hyprctl", "version", "-j"): Completed(0, '{"tag": "v0.50.0"}'),
            (
                "/usr/bin/bwrap",
                "--ro-bind",
                "/",
                "/",
                "--dev-bind",
                "/dev",
                "/dev",
                "true",
            ): Completed(0),
            ("gst-inspect-1.0", "webrtcbin"): Completed(0),
            ("/usr/bin/python3", "-c", "import gi"): Completed(0),
            (
                "systemctl",
                "--user",
                "show",
                "omarchy-ai",
                "-p",
                doctor.UNIT_PROPERTIES,
            ): Completed(0, "".join(f"{k}={v}\n" for k, v in UNIT.items())),
        }
        self.table.update(table or {})
        self.calls = []

    def __call__(self, argv, timeout=None, env=None):
        self.calls.append(list(argv))
        return self.table.get(tuple(argv), Completed(127, "", f"{argv[0]}: not found"))


class FakeHttp:
    def __init__(self, replies=None):
        self.replies = replies or {}
        self.calls = []

    def __call__(self, url, headers, timeout):
        self.calls.append((url, dict(headers), timeout))
        return self.replies.get(url, Response(0, b"", "no network in tests"))


class DoctorCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        self.conf = self.root / "config"
        self.runtime = self.root / "run"
        for d in (self.state, self.conf, self.runtime):
            d.mkdir()
        self.runner = FakeRunner()
        self.http = FakeHttp()
        self.write_config({})

    def write_config(self, data, raw=None):
        defaults = {
            "api_key_path": str(self.conf / "key"),
            "gemini_api_key_path": str(self.conf / "gemini-key"),
            "vercel_gateway_api_key_path": str(self.conf / "vercel-ai-gateway-key"),
            "custom_wake_model_paths": [],
        }
        text = raw if raw is not None else json.dumps({**defaults, **data})
        (self.conf / "config.yaml").write_text(text)

    def key(self, name, value=None, mode=0o600):
        path = self.conf / name
        path.write_text(value or secrets.token_hex(20))
        os.chmod(path, mode)
        return path

    def ctx(self, **kw):
        base = dict(
            state_dir=self.state,
            config_dir=self.conf,
            config_path=self.conf / "config.yaml",
            run=self.runner,
            http=self.http,
            which=lambda name: f"/usr/bin/{name}",
            exists=lambda p: True,
            env={"XDG_RUNTIME_DIR": str(self.runtime)},
            installed_version=lambda: "1.2.3",
        )
        base.update(kw)
        return Context(**base)

    def check(self, fn, **kw):
        result = fn(self.ctx(**kw))
        self.assertIn(result.status, (GREEN, YELLOW, RED))
        self.assertTrue(result.reason)
        if result.status != GREEN:
            self.assertTrue(result.fix, f"{result.id} has no fix hint")
        return result


class ConfigAndKeysTests(DoctorCase):
    def test_config_green_unknown_yellow_broken_red(self):
        self.assertEqual(self.check(doctor.check_config).status, GREEN)
        self.write_config({"no_such_setting": 1})
        self.assertEqual(self.check(doctor.check_config).status, YELLOW)
        self.write_config({}, raw="provider: [unclosed")
        self.assertEqual(self.check(doctor.check_config).status, RED)

    def test_keys_never_printed(self):
        value = secrets.token_hex(24)
        self.key("gemini-key", value)
        self.key("vercel-ai-gateway-key", value + "x")
        self.write_config({"provider": "gemini"})
        result = self.check(doctor.check_api_keys)
        self.assertEqual(result.status, GREEN)
        self.assertNotIn(value, json.dumps(result.__dict__))

    def test_missing_required_key_is_red_open_mode_yellow(self):
        self.write_config({"provider": "gemini"})
        self.key("vercel-ai-gateway-key")
        self.assertEqual(self.check(doctor.check_api_keys).status, RED)
        self.key("gemini-key", mode=0o644)
        result = self.check(doctor.check_api_keys)
        self.assertEqual(result.status, YELLOW)
        self.assertIn("chmod 600", result.fix)


class NetworkTests(DoctorCase):
    def test_gateway(self):
        self.key("vercel-ai-gateway-key")
        url = doctor.GATEWAY_CREDITS_URL
        self.http.replies[url] = Response(
            200, b'{"balance": "12.5", "total_used": "3"}'
        )
        result = self.check(doctor.check_gateway)
        self.assertEqual(result.status, GREEN)
        self.assertTrue(self.http.calls[0][1]["Authorization"].startswith("Bearer "))
        self.assertLessEqual(self.http.calls[0][2], 5)
        self.http.replies[url] = Response(401, b"{}")
        self.assertEqual(self.check(doctor.check_gateway).status, RED)
        self.http.replies[url] = Response(200, b'{"balance": "0"}')
        self.assertEqual(self.check(doctor.check_gateway).status, RED)
        del self.http.replies[url]
        self.assertEqual(self.check(doctor.check_gateway).status, RED)

    def test_quick_skips_network(self):
        self.key("vercel-ai-gateway-key")
        for fn in (doctor.check_gateway, doctor.check_version):
            self.check(fn, quick=True)
        self.assertEqual(self.http.calls, [])

    def _identity(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding,
            NoEncryption,
            PrivateFormat,
            PublicFormat,
        )
        import base64

        key = Ed25519PrivateKey.generate()
        identity = {
            "private_key": base64.b64encode(
                key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
            ).decode(),
            "public_key": base64.b64encode(
                key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
            ).decode(),
            "key_fingerprint": "fp-test",
        }
        (self.conf / "myapi").mkdir()
        (self.conf / "myapi" / "identity.json").write_text(json.dumps(identity))

    def test_myapi(self):
        self.assertEqual(self.check(doctor.check_myapi).status, GREEN)  # not used
        self.write_config({"myapi_enabled": True})
        self.assertEqual(
            self.check(doctor.check_myapi).status, YELLOW
        )  # on, not connected
        self._identity()
        from omarchy_ai.myapi import client

        url = client.BASE_URL + "/services"
        self.http.replies[url] = Response(200, b"{}")
        self.assertEqual(self.check(doctor.check_myapi).status, GREEN)
        url_, headers, _ = self.http.calls[-1]
        self.assertIn("X-Agent-Signature", headers)
        self.http.replies[url] = Response(403, b"{}")
        self.assertEqual(self.check(doctor.check_myapi).status, RED)

    def test_version(self):
        url = doctor.LATEST_RELEASE_URL
        self.http.replies[url] = Response(200, b'{"tag_name": "v1.2.3"}')
        self.assertEqual(self.check(doctor.check_version).status, GREEN)
        self.http.replies[url] = Response(200, b'{"tag_name": "v1.3.0"}')
        self.assertEqual(self.check(doctor.check_version).status, YELLOW)

    def test_version_service_runs_another_install(self):
        other = self.root / "other"
        other.mkdir()
        (other / "pyproject.toml").write_text(
            '[project]\nname = "x"\nversion = "1.0.0"\n'
        )
        unit = {**UNIT, "WorkingDirectory": str(other)}
        self.runner.table[
            ("systemctl", "--user", "show", "omarchy-ai", "-p", doctor.UNIT_PROPERTIES)
        ] = Completed(0, "".join(f"{k}={v}\n" for k, v in unit.items()))
        result = self.check(doctor.check_version, quick=True)
        self.assertEqual(result.status, YELLOW)
        self.assertIn("1.0.0", result.reason)


class HealthTests(DoctorCase):
    def _health(self, data):
        (self.state / "health.json").write_text(
            json.dumps({"schema_version": 1, **data})
        )

    def test_quota_and_provider_health(self):
        until = time.time() + 600
        (self.state / "provider_health.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "entries": {
                        "quota:gemini": {
                            "provider": "gemini",
                            "kind": "quota",
                            "state": "exhausted",
                            "until": until,
                            "reason": "q",
                        },
                        "llm:x": {
                            "provider": "x",
                            "kind": "llm",
                            "state": "cooldown",
                            "until": until,
                            "reason": "timeout",
                        },
                    },
                }
            )
        )
        self.assertEqual(self.check(doctor.check_quota).status, RED)
        self.assertEqual(self.check(doctor.check_provider_health).status, YELLOW)
        (self.state / "provider_health.json").unlink()
        self.assertEqual(self.check(doctor.check_quota).status, GREEN)
        self.assertEqual(self.check(doctor.check_provider_health).status, GREEN)

    def test_safe_mode(self):
        self.assertEqual(self.check(doctor.check_safe_mode).status, GREEN)
        self._health({"crashes": [time.time()], "safe_mode": None})
        self.assertEqual(self.check(doctor.check_safe_mode).status, YELLOW)
        self._health(
            {
                "crashes": [time.time()] * 3,
                "safe_mode": {"since": time.time(), "crashes": 3},
            }
        )
        self.assertEqual(self.check(doctor.check_safe_mode).status, RED)

    def test_doctor_runs_in_safe_mode(self):
        self._health(
            {
                "crashes": [time.time()] * 3,
                "safe_mode": {"since": time.time(), "crashes": 3},
            }
        )
        with mock.patch.object(health, "active", True):
            results = doctor.run_checks(self.ctx(quick=True))
        self.assertEqual(len(results), len(doctor.CHECKS))
        self.assertEqual(next(r for r in results if r.id == "safe_mode").status, RED)

    def test_systemd(self):
        self.assertEqual(self.check(doctor.check_systemd).status, GREEN)
        key = (
            "systemctl",
            "--user",
            "show",
            "omarchy-ai",
            "-p",
            doctor.UNIT_PROPERTIES,
        )
        for change, expected in (
            ({"NRestarts": "4"}, YELLOW),
            ({"WatchdogUSec": "0"}, YELLOW),
            ({"ActiveState": "failed"}, RED),
            ({"ActiveState": "inactive"}, RED),
            ({"LoadState": "not-found"}, YELLOW),
        ):
            unit = {**UNIT, **change}
            self.runner.table[key] = Completed(
                0, "".join(f"{k}={v}\n" for k, v in unit.items())
            )
            self.assertEqual(self.check(doctor.check_systemd).status, expected, change)
        # read-only: nothing but `systemctl --user show`
        self.assertTrue(
            all(c[:3] == ["systemctl", "--user", "show"] for c in self.runner.calls)
        )


class DeviceTests(DoctorCase):
    def test_wpctl_parse(self):
        found = doctor.parse_wpctl_status(WPCTL_STATUS)
        self.assertEqual([d["id"] for d in found["sinks"]], [57])
        self.assertEqual([d["id"] for d in found["sources"]], [58])  # not the camera
        self.assertTrue(found["sources"][0]["default"])

    def test_audio(self):
        for fn in (
            doctor.check_pipewire,
            doctor.check_microphone,
            doctor.check_speaker,
        ):
            self.assertEqual(self.check(fn).status, GREEN, fn.__name__)
        self.runner.table[("wpctl", "get-volume", "@DEFAULT_AUDIO_SOURCE@")] = (
            Completed(0, "Volume: 0.35 [MUTED]\n")
        )
        self.assertEqual(self.check(doctor.check_microphone).status, YELLOW)
        self.write_config({"mic_device": "99"})
        self.assertEqual(self.check(doctor.check_microphone).status, RED)
        self.runner.table[("wpctl", "status")] = Completed(1, "", "could not connect")
        for fn in (
            doctor.check_pipewire,
            doctor.check_microphone,
            doctor.check_speaker,
        ):
            self.assertEqual(self.check(fn).status, RED, fn.__name__)

    def test_wake_model(self):
        self.assertEqual(
            self.check(doctor.check_wake_model).status, YELLOW
        )  # bundled only
        model = self.conf / "name.onnx"
        model.write_bytes(b"\0" * 16)
        self.write_config({"custom_wake_model_paths": [str(model)]})
        self.assertEqual(self.check(doctor.check_wake_model).status, GREEN)
        model.unlink()
        self.assertEqual(self.check(doctor.check_wake_model).status, RED)

    def test_desktop(self):
        self.assertEqual(self.check(doctor.check_desktop).status, RED)  # no sockets
        (self.runtime / "wayland-1").touch()
        (self.runtime / "hypr" / "sig").mkdir(parents=True)
        (self.runtime / "hypr" / "sig" / ".socket.sock").touch()
        self.assertEqual(self.check(doctor.check_desktop).status, GREEN)
        self.runner.table[("hyprctl", "version", "-j")] = Completed(
            1, "", "Couldn't connect"
        )
        self.assertEqual(self.check(doctor.check_desktop).status, RED)

    def test_sandbox(self):
        self.assertEqual(self.check(doctor.check_sandbox).status, GREEN)
        self.assertEqual(
            self.check(doctor.check_sandbox, which=lambda n: None).status, RED
        )
        self.runner.table[
            (
                "/usr/bin/bwrap",
                "--ro-bind",
                "/",
                "/",
                "--dev-bind",
                "/dev",
                "/dev",
                "true",
            )
        ] = Completed(1, "", "setting up uid map: Permission denied")
        self.assertEqual(self.check(doctor.check_sandbox).status, RED)

    def test_dependencies(self):
        self.assertEqual(self.check(doctor.check_dependencies).status, GREEN)
        self.assertEqual(
            self.check(
                doctor.check_dependencies,
                which=lambda n: None if n == "adb" else f"/usr/bin/{n}",
            ).status,
            YELLOW,
        )
        self.assertEqual(
            self.check(
                doctor.check_dependencies,
                which=lambda n: None if n == "pw-record" else f"/usr/bin/{n}",
            ).status,
            RED,
        )


class StateTests(DoctorCase):
    def test_state_files_never_quarantined_by_doctor(self):
        self.assertEqual(self.check(doctor.check_state_files).status, GREEN)
        (self.state / "agenda.json").write_text("[{broken")
        self.assertEqual(self.check(doctor.check_state_files).status, RED)
        self.assertEqual(
            (self.state / "agenda.json").read_text(), "[{broken"
        )  # left as it was
        self.assertEqual(self.check(doctor.check_agenda).status, YELLOW)
        (self.state / "agenda.json").unlink()
        (self.state / "agenda.json.corrupt-20260101-000000").write_text("x")
        self.assertEqual(self.check(doctor.check_state_files).status, YELLOW)

    def test_pending_actions(self):
        path = self.state / "pending_actions.json"
        now = time.time()
        path.write_text(
            json.dumps(
                {"a": {"status": "waiting", "created": now, "title": "Send mail"}}
            )
        )
        self.assertEqual(self.check(doctor.check_pending_actions).status, GREEN)
        path.write_text(
            json.dumps(
                {"a": {"status": "uncertain", "created": now, "title": "Send mail"}}
            )
        )
        self.assertEqual(self.check(doctor.check_pending_actions).status, YELLOW)
        path.write_text(
            json.dumps(
                {
                    "a": {
                        "status": "running",
                        "created": now - 600,
                        "claimed": now - 600,
                        "owner": {"pid": 999999999, "start": "1"},
                        "title": "Send mail",
                    }
                }
            )
        )
        self.assertEqual(self.check(doctor.check_pending_actions).status, RED)

    def test_uncertain_effects(self):
        now = time.time()
        entry = {
            "effect_id": "eff1",
            "kind": "myapi",
            "tool": "myapi_execute",
            "target": "gmail:send",
            "state": "uncertain",
            "ts": now,
            "updated": now,
        }
        path = self.state / "effects_index.json"
        path.write_text(json.dumps({"schema_version": 1, "effects": {"k": entry}}))
        result = self.check(doctor.check_effects)
        self.assertEqual(result.status, YELLOW)
        self.assertIn("eff1", result.reason)
        entry.update(ts=now - 3 * 86400, updated=now - 3 * 86400)
        path.write_text(json.dumps({"schema_version": 1, "effects": {"k": entry}}))
        self.assertEqual(self.check(doctor.check_effects).status, GREEN)
        entry.update(
            state="dispatched",
            ts=now,
            updated=now,
            owner={"pid": 999999999, "start": "1"},
        )
        path.write_text(json.dumps({"schema_version": 1, "effects": {"k": entry}}))
        self.assertEqual(self.check(doctor.check_effects).status, YELLOW)

    def test_tasks(self):
        tasks = self.state / "tasks"
        tasks.mkdir()
        now = time.time()
        (tasks / "t1.json").write_text(
            json.dumps(
                {
                    "id": "t1",
                    "goal": "g",
                    "workspace": "/tmp",
                    "status": "waiting_approval",
                    "updated_at": now,
                }
            )
        )
        self.assertEqual(self.check(doctor.check_tasks).status, GREEN)
        (tasks / "t2.json").write_text(
            json.dumps(
                {
                    "id": "t2",
                    "goal": "g",
                    "workspace": "/tmp",
                    "status": "interrupted",
                    "updated_at": now,
                }
            )
        )
        result = self.check(doctor.check_tasks)
        self.assertEqual(result.status, YELLOW)
        self.assertIn("t2", result.reason)
        (tasks / "t2.json").write_text(
            json.dumps(
                {
                    "id": "t2",
                    "goal": "g",
                    "workspace": "/tmp",
                    "status": "running",
                    "updated_at": now,
                    "owner": {"pid": 999999999, "start": "1"},
                }
            )
        )
        self.assertEqual(self.check(doctor.check_tasks).status, YELLOW)

    def test_agenda_leases(self):
        from omarchy_ai.core import agenda
        from omarchy_ai.runtime.task import this_process

        me = this_process()
        now = time.time()
        job = {
            "id": "j1",
            "title": "Water plants",
            "status": "active",
            "started_at": now,
            "running_by": {
                "pid": me["pid"],
                "start_time": me["start"],
                "lease_until": now + 60,
            },
        }
        path = self.state / "agenda.json"
        path.write_text(json.dumps([job]))
        result = self.check(doctor.check_agenda)
        self.assertEqual(result.status, GREEN)
        self.assertEqual(result.details["running"], 1)
        job.update(started_at=now - agenda.STALE_RUN_SECONDS - 60, running_by=None)
        path.write_text(json.dumps([job]))
        self.assertEqual(self.check(doctor.check_agenda).status, YELLOW)

    def test_disk(self):
        import collections

        usage = collections.namedtuple("usage", "total used free")
        with mock.patch(
            "shutil.disk_usage", return_value=usage(100 * 2**30, 0, 50 * 2**30)
        ):
            self.assertEqual(self.check(doctor.check_disk).status, GREEN)
        with mock.patch(
            "shutil.disk_usage", return_value=usage(100 * 2**30, 0, 100 * 2**20)
        ):
            self.assertEqual(self.check(doctor.check_disk).status, RED)


class OutputTests(DoctorCase):
    def test_json_schema(self):
        results = doctor.run_checks(self.ctx(quick=True))
        doc = json.loads(json.dumps(doctor.report(results), default=str))
        self.assertEqual(doc["schema_version"], doctor.SCHEMA_VERSION)
        self.assertIn(doc["status"], (GREEN, YELLOW, RED))
        self.assertEqual(sum(doc["counts"].values()), len(doctor.CHECKS))
        self.assertEqual(
            [c["id"] for c in doc["checks"]], [cid for cid, _ in doctor.CHECKS]
        )
        for check in doc["checks"]:
            self.assertEqual(
                set(check), {"id", "title", "status", "reason", "fix", "details"}
            )
            self.assertIn(check["status"], (GREEN, YELLOW, RED))
            self.assertIsInstance(check["details"], dict)

    def test_a_crashing_check_is_red_not_fatal(self):
        def boom(ctx):
            raise RuntimeError("kaput")

        with mock.patch.object(
            doctor, "CHECKS", [("boom", boom), ("disk", doctor.check_disk)]
        ):
            results = doctor.run_checks(self.ctx())
        self.assertEqual([r.status for r in results][0], RED)
        self.assertEqual(results[1].id, "disk")

    def test_summary_puts_problems_first(self):
        results = [
            doctor.Result("a", "A", GREEN, "fine"),
            doctor.Result("b", "B", YELLOW, "meh", "do x"),
            doctor.Result("c", "C", RED, "bad", "do y"),
        ]
        text = doctor.summary(results)
        lines = text.splitlines()
        self.assertEqual(lines[0], "Diagnostics: 1 red, 1 yellow, 1 green.")
        self.assertTrue(lines[1].startswith("RED c: bad Fix: do y"))
        self.assertTrue(lines[2].startswith("YELLOW b"))
        self.assertEqual(lines[3], "Green: a.")


class TraceTests(DoctorCase):
    def test_trace_reassembles_a_turn(self):
        logs_dir = self.state / "logs"
        logs_dir.mkdir()

        def line(ts, msg, **ids):
            return (
                json.dumps(
                    {"ts": ts, "level": "INFO", "logger": "t", "msg": msg, **ids}
                )
                + "\n"
            )

        (logs_dir / "omarchy-ai.20261001T000000.jsonl").write_text(
            line(
                "2026-10-01T10:00:00.000+00:00",
                "turn start",
                session_id="s1",
                turn_id="u1",
            )
        )
        (logs_dir / "omarchy-ai.jsonl").write_text(
            line(
                "2026-10-01T10:00:02.000+00:00",
                "tool result",
                session_id="s1",
                turn_id="u1",
                tool_call_id="c1",
            )
            + line(
                "2026-10-01T10:00:03.000+00:00",
                "task started",
                tool_call_id="c1",
                task_id="k1",
            )
            + line(
                "2026-10-01T10:00:05.000+00:00", "step done", task_id="k1", step_id="p1"
            )
            + line(
                "2026-10-01T10:00:01.000+00:00",
                "other turn",
                session_id="s1",
                turn_id="u2",
            )
            + "{torn line\n"
        )
        effect_ts = (
            __import__("datetime")
            .datetime.fromisoformat("2026-10-01T10:00:04+00:00")
            .timestamp()
        )
        (self.state / "effects.jsonl").write_text(
            json.dumps(
                {
                    "effect_id": "e1",
                    "tool": "myapi_execute",
                    "target": "gmail",
                    "state": "confirmed",
                    "ts": effect_ts,
                    "log": {"task_id": "k1"},
                }
            )
            + "\n"
        )
        rows = doctor.trace("u1", self.state)
        self.assertEqual(
            [r.get("msg") or r.get("effect_id") for r in rows],
            ["turn start", "tool result", "task started", "e1", "step done"],
        )
        self.assertNotIn("other turn", doctor.render_trace("u1", rows))
        self.assertEqual(
            len(doctor.trace("k1", self.state)), 3
        )  # its start, its effect, its step
        self.assertEqual(doctor.trace("nope", self.state), [])


class BundleTests(DoctorCase):
    def test_bundle_has_no_canary_secret(self):
        canary_key = "cn" + secrets.token_hex(16)
        canary_config = "cc" + secrets.token_hex(16)
        self.key("vercel-ai-gateway-key", canary_key)
        self.write_config(
            {
                "github_token": canary_config,
                "approval_pin": canary_config,
                "wake_word": "hello",
            }
        )
        logs_dir = self.state / "logs"
        logs_dir.mkdir()
        (logs_dir / "omarchy-ai.jsonl").write_text(
            json.dumps(
                {"ts": "2026-10-01T10:00:00+00:00", "msg": f"leaked {canary_key}"}
            )
            + "\n"
        )
        out = self.root / "bundle.tar.gz"
        written = doctor.bundle(out, self.ctx(quick=True))
        self.assertEqual(written, out)
        self.assertEqual(out.stat().st_mode & 0o777, 0o600)
        names, blob = [], b""
        with tarfile.open(out) as tar:
            for member in tar.getmembers():
                names.append(member.name.split("/", 1)[1])
                blob += tar.extractfile(member).read()
        self.assertEqual(
            set(names),
            {"doctor.json", "config.yaml", "versions.txt", "logs/omarchy-ai.jsonl"},
        )
        self.assertNotIn(canary_key.encode(), blob)
        self.assertNotIn(canary_config.encode(), blob)
        self.assertIn(b"hello", blob)  # the rest of the config is kept


class CliTests(DoctorCase):
    def test_main_json(self):
        buf = io.StringIO()
        ctx = self.ctx(quick=True)
        with (
            mock.patch.object(doctor.Context, "default", return_value=ctx),
            redirect_stdout(buf),
        ):
            code = doctor.main(["--json", "--quick"])
        doc = json.loads(buf.getvalue())
        self.assertEqual(code, 1 if doc["counts"]["red"] else 0)

    def test_leaked(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(doctor.main(["--leaked", "api_key"]), 0)
        self.assertIn("Rotate", buf.getvalue())


class ToolTests(unittest.TestCase):
    def test_run_diagnostics_in_catalog_not_core(self):
        from omarchy_ai.execution import catalog, tools
        from omarchy_ai.execution.actions import ACTIONS

        self.assertIn("run_diagnostics", catalog.catalog())
        self.assertNotIn("run_diagnostics", catalog.CORE)
        self.assertNotIn("run_diagnostics", {t["name"] for t in catalog.declared(True)})
        self.assertEqual(tools.effect_class("run_diagnostics"), tools.READ)
        self.assertEqual(tools.PROVENANCE["run_diagnostics"], "local_state")
        self.assertIn("run_diagnostics", ACTIONS)
        schema = next(t for t in tools.TOOLS if t["name"] == "run_diagnostics")
        self.assertLess(len(schema["description"]), 200)

    def test_tool_returns_the_summary(self):
        from omarchy_ai.execution.actions import ACTIONS

        fake = [doctor.Result("disk", "Disk", RED, "full", "free space")]
        with mock.patch.object(doctor, "run_checks", return_value=fake):
            result = ACTIONS["run_diagnostics"]({})
        self.assertTrue(result.ok)
        self.assertIn("RED disk: full Fix: free space", result.message)


if __name__ == "__main__":
    unittest.main()
