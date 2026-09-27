"""JSON-only text model access for internal workers (System agent, planner,
internal reviewer). Jev never generates text; these workers do, and their
output is always parsed and checked by code before anything runs.

Backends, in the order of config task_worker_backends (default: claude,
codex, api -- user, 2026-09-26: "use claude code, codex; only if they are
not an option use the API"):

- claude: `claude -p --tools ""` -- Claude Code as a brain with every tool
  disabled, so it only answers with the next JSON step. Model alias "sonnet",
  escalating to "opus"; the aliases always name the newest models.
- codex: `codex exec --sandbox read-only` in an empty directory, told not to
  run anything. Codex has no switch that disables its shell, so its JSON
  event stream is checked: a reply after any command or file change of its
  own is discarded and the next backend answers instead.
- api: the Vercel AI Gateway chat-completions endpoint (voice/omarchy.py
  GatewayClient) on runtime/models.py's auto-chosen ladder.

Every command the worker proposes still runs through the harness's own
permission checks (runtime.py WorkContextImpl), whichever backend proposed it.
A backend that is missing, logged out, failing, or out of quota is skipped
for that call (quota: for an hour). Both CLI backends passed the worker exam
on 2026-09-26 (claude avg 5.7s/step, codex 9.0s; the API ladder 10-30s).
There is a bounded schema-retry (MiniMax's verifier retries once with "the
previous reply had no parseable verdict"; same idea here).
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
import subprocess
import tempfile
import threading
import time

log = logging.getLogger(__name__)

_TRANSIENT = re.compile(r"HTTP (429|500|502|503|504|529)\b|timed out|temporarily unavailable|unavailable", re.I)


class WorkerModelError(RuntimeError):
    pass


# The escalation step of the task being worked on (runtime.py sets it around
# each executor call; each task runs in its own thread).
TIER: contextvars.ContextVar[int] = contextvars.ContextVar("worker_model_tier", default=0)

BACKENDS = ("claude", "codex", "api")
CLAUDE_TIERS = ("sonnet", "opus")
_LIMIT = re.compile(r"usage limit|rate.?limit|quota|limit reached|too many requests|credit balance|\b429\b", re.I)
LIMIT_COOLDOWN = 3600
_cooldown: dict[str, float] = {}
_cooldown_lock = threading.Lock()
_agents: dict = {}

_UNAVAILABLE = re.compile(r"HTTP (400|404)\b.*model|model.{0,80}(not found|does not exist|not supported|unsupported|"
                          r"unknown|deprecated|no longer|retired|not available)", re.I | re.S)


class WorkerModel:
    """complete(system, user) -> dict. `transport(payload, timeout) -> raw
    chat-completions JSON` is injectable for tests.

    The model is `model` if given, else config task_agent_model if it names
    one, else ("auto", the default) the step of runtime/models.py's
    qualified ladder for the current TIER -- resolved on every call, so a
    re-qualification or an escalation takes effect at the next step."""

    def __init__(self, model: str | None = None, transport=None, *, max_tokens: int = 2500):
        self._pinned = model
        self._injected = transport is not None
        self._transport = transport
        self._fallback = None
        self._order = None
        self.max_tokens = max_tokens
        self.last_used = None  # "<backend>:<model>" of the last answer

    def backends(self) -> list[str]:
        """Usable backends for this call, in preference order."""
        if self._pinned or self._injected:
            return ["api"]
        if self._order is None:
            from ..config import load_config
            configured = getattr(load_config(), "task_worker_backends", None) or BACKENDS
            self._order = [b for b in configured if b in BACKENDS] or ["api"]
            self._setup()
            if self._pinned:  # task_agent_model names a model: the API only
                return ["api"]
        now = time.time()
        return [b for b in self._order if b == "api" or (_cooldown.get(b, 0) <= now and _cli_ready(b))]

    def _setup(self):
        if self._transport is None:
            from ..config import load_config
            from ..voice.omarchy import GatewayClient
            config = load_config()
            client = GatewayClient(config)
            configured = getattr(config, "task_agent_model", None)
            if not self._pinned and configured and configured != "auto":
                self._pinned = configured
            self._fallback = client._text_model()

            def transport(payload, timeout):
                return json.loads(client._request("/chat/completions", json.dumps(payload).encode(),
                                                  "application/json", timeout=timeout))
            self._transport = transport
        return self._transport

    @property
    def auto(self) -> bool:
        self._setup()
        return not self._pinned

    @property
    def model(self) -> str:
        """The model the next call is expected to use (a label for records)."""
        first = self.backends()[0]
        if first != "api":
            return _cli_label(first, TIER.get())
        return self._api_model()

    def _api_model(self) -> str:
        self._setup()
        if self._pinned:
            return self._pinned
        from . import models
        return models.resolve(TIER.get()) or self._fallback or "unknown"

    def complete(self, system: str, user, *, timeout: float = 60, retries: int = 2) -> dict:
        content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
        errors = []
        for backend in self.backends():
            try:
                if backend == "api":
                    value = self._api_complete(system, user, timeout=timeout, retries=retries)
                    self.last_used = f"api:{self._api_model()}"
                else:
                    value = _cli_complete(backend, system, content, TIER.get(), timeout)
                    self.last_used = _cli_label(backend, TIER.get())
                return value
            except WorkerModelError as exc:
                errors.append(f"{backend}: {exc}")
                if backend != "api" and _LIMIT.search(str(exc)):
                    with _cooldown_lock:
                        _cooldown[backend] = time.time() + LIMIT_COOLDOWN
                    log.warning("worker backend %s is out of quota; skipping it for an hour", backend)
                else:
                    log.warning("worker backend %s failed; trying the next: %s", backend, str(exc)[:200])
        raise WorkerModelError("; ".join(errors) or "no worker backend available")

    def _api_complete(self, system: str, user, *, timeout: float, retries: int) -> dict:
        transport = self._setup()
        content = user if isinstance(user, str) else json.dumps(user, ensure_ascii=False)
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        last_error = ""
        swapped = False
        attempt = 0
        while attempt <= retries:
            model = self._api_model()
            payload = {"model": model, "max_tokens": self.max_tokens,
                       "response_format": {"type": "json_object"}, "messages": messages}
            if self._temperature_ok(model):
                payload["temperature"] = 0
            try:
                raw = transport(payload, timeout)
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if not self._pinned and not swapped and _UNAVAILABLE.search(last_error):
                    # Retired or renamed under us: drop it and use the next step now.
                    from . import models
                    models.report_unavailable(model)
                    swapped = True
                    continue
                if attempt < retries and _TRANSIENT.search(last_error):
                    time.sleep(0.8 * 2 ** attempt)
                    attempt += 1
                    continue
                raise WorkerModelError(last_error) from exc
            value = parse_json_object(_content(raw))
            if value is not None:
                return value
            last_error = "reply was not a JSON object"
            messages = messages + [{"role": "user", "content":
                                    "Your previous reply was not a single valid JSON object. Reply again with only the JSON object."}]
            attempt += 1
        raise WorkerModelError(last_error)

    @staticmethod
    def _temperature_ok(model: str) -> bool:
        try:
            from . import models
            return models.supports_temperature(model)
        except Exception:  # noqa: BLE001
            return True


def _agent(name: str):
    if name not in _agents:
        from .executors.coding_agents import ClaudeCode, Codex
        _agents[name] = {"claude": ClaudeCode, "codex": Codex}[name]()
    return _agents[name]


def _cli_ready(name: str) -> bool:
    """Installed and logged in (the coding-agent executors' own check, cached)."""
    try:
        return bool(_agent(name).detect().get("authenticated"))
    except Exception:  # noqa: BLE001
        return False


def _cli_label(backend: str, tier: int) -> str:
    if backend == "claude":
        return f"claude-code:{CLAUDE_TIERS[min(max(tier, 0), len(CLAUDE_TIERS) - 1)]}"
    return "codex:default"


_JSON_ONLY = "\n\nReply with only the JSON object."


def _cli_complete(backend: str, system: str, content: str, tier: int, timeout: float) -> dict:
    """One JSON step from a CLI agent, with one schema retry."""
    prompt = content
    for attempt in range(2):
        text = (_claude if backend == "claude" else _codex)(system, prompt, tier, timeout)
        value = parse_json_object(text)
        if value is not None:
            return value
        prompt = (content + "\n\nYour previous reply was not a single valid JSON object. "
                  "Reply again with only the JSON object.")
    raise WorkerModelError(f"{backend} reply was not a JSON object: {text[:160]!r}")


def _run(argv: list[str], stdin: str, timeout: float, cwd: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except subprocess.TimeoutExpired as exc:
        raise WorkerModelError(f"{argv[0]} timed out after {timeout:.0f}s") from exc
    except OSError as exc:
        raise WorkerModelError(f"{argv[0]} could not start: {exc}") from exc


def _claude(system: str, prompt: str, tier: int, timeout: float) -> str:
    model = CLAUDE_TIERS[min(max(tier, 0), len(CLAUDE_TIERS) - 1)]
    with tempfile.TemporaryDirectory(prefix="omarchy-worker-") as tmp:
        # --tools "": no tool at all; --safe-mode: no CLAUDE.md, hooks, skills or
        # MCP servers from this machine leak into (or act from) the worker.
        proc = _run(["claude", "-p", "--tools", "", "--safe-mode", "--no-session-persistence",
                     "--output-format", "json", "--model", model, "--system-prompt", system + _JSON_ONLY],
                    prompt, timeout, tmp)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        raise WorkerModelError(f"claude exit {proc.returncode}: {(proc.stderr or proc.stdout)[-300:]}") from None
    if out.get("is_error") or proc.returncode:
        raise WorkerModelError(f"claude: {str(out.get('result') or out.get('subtype') or proc.stderr)[:300]}")
    return str(out.get("result") or "")


def _codex(system: str, prompt: str, tier: int, timeout: float) -> str:
    with tempfile.TemporaryDirectory(prefix="omarchy-worker-") as tmp:
        proc = _run(["codex", "exec", "--json", "--sandbox", "read-only", "--skip-git-repo-check", "--ephemeral",
                     "--color", "never", "-"],
                    system + "\n\nYou are only choosing the next step: do not run any command, read any file or "
                    "use any tool yourself." + _JSON_ONLY + "\n\n" + prompt, timeout, tmp)
    message, acted = "", []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        item = event.get("item") or {}
        if item.get("type") == "agent_message":
            message = str(item.get("text") or "")
        elif item.get("type") in ("command_execution", "file_change", "mcp_tool_call", "web_search"):
            acted.append(f"{item.get('type')}: {str(item.get('command') or item.get('changes') or '')[:120]}")
        elif event.get("type") in ("error", "turn.failed"):
            raise WorkerModelError(f"codex: {json.dumps(event)[:300]}")
    if acted:
        # Its own actions bypass the harness: never use such a reply.
        log.warning("codex acted on its own while choosing a step; reply discarded: %s", acted[:3])
        raise WorkerModelError("codex ran something itself; reply discarded")
    if proc.returncode and not message:
        raise WorkerModelError(f"codex exit {proc.returncode}: {proc.stderr[-300:]}")
    return message


def _content(raw) -> str:
    choices = (raw or {}).get("choices") or []
    content = (choices[0].get("message") or {}).get("content") if choices else ""
    if isinstance(content, list):
        content = "".join(str(p.get("text", "")) if isinstance(p, dict) else str(p) for p in content)
    return str(content or "")


def parse_json_object(text: str) -> dict | None:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.I | re.S).strip()
    try:
        value = json.loads(text)
    except ValueError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            return None
    return value if isinstance(value, dict) else None
