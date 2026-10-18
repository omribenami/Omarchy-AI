"""The voice satellite's conversation (voice/satellite.py _run_session)
reconnects once when Gemini closes it with a transient code, and she says
she is back (voice/fallback.py DROPPED). Fakes only: no device, no Gemini."""

import asyncio
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from google.genai import errors

from omarchy_ai.config import Config
from omarchy_ai.voice import fallback
from omarchy_ai.voice.satellite import Satellite


def api_error(code, message):
    return errors.APIError(code, {"error": {"message": message}})


class SatelliteReconnectTests(unittest.IsolatedAsyncioTestCase):
    async def converse(self, fails):
        """One satellite conversation; connection n raises the real 1011 when fails(n)."""
        satellite = Satellite(Config(provider="gemini", jev_fast_path=False))
        satellite._event = lambda *a, **k: None
        satellite._show = lambda *a, **k: None
        sessions = []

        def fake_session():
            n = len(sessions)

            async def receive():
                await asyncio.sleep(0.05)
                if fails(n):
                    raise api_error(1011, "Internal error encountered.")
                satellite._adapter._hangup.set()
                await asyncio.sleep(3600)
                yield  # pragma: no cover

            session = SimpleNamespace(
                receive=receive,
                send_client_content=AsyncMock(),
                send_realtime_input=AsyncMock(),
                send_tool_response=AsyncMock(),
            )
            sessions.append(session)
            return session

        @asynccontextmanager
        async def connect(client, config, model, live_config):
            yield fake_session()

        folder = Path(tempfile.mkdtemp())
        with (
            patch("omarchy_ai.voice.gemini_live.live_connect", connect),
            patch(
                "omarchy_ai.voice.gemini_live.gemini_client",
                return_value=SimpleNamespace(aio=SimpleNamespace(aclose=AsyncMock())),
            ),
            patch(
                "omarchy_ai.voice.gemini_live.build_live_config",
                return_value={"system_instruction": ""},
            ),
            patch("omarchy_ai.voice.gemini_live.origin_note", return_value=""),
            patch("omarchy_ai.core.conversations.DIR", folder),
            patch("omarchy_ai.core.conversations.flush"),
            patch("omarchy_ai.core.history.append_session"),
            patch("omarchy_ai.core.agenda.mark_briefed"),
            patch("omarchy_ai.core.agenda.attach_call"),
            patch("omarchy_ai.core.agenda.detach_call"),
            patch("omarchy_ai.core.agenda.mark_delivered"),
        ):
            await asyncio.wait_for(satellite._run_session(None, 0), 5)
        satellite._pool.shutdown(wait=False)
        return sessions

    async def test_a_1011_reconnects_once_and_she_says_she_is_back(self):
        with self.assertLogs("omarchy_ai.voice.gemini", "WARNING") as logged:
            sessions = await self.converse(lambda n: n == 0)
        self.assertEqual(len(sessions), 2)
        kwargs = sessions[1].send_client_content.call_args.kwargs
        self.assertTrue(kwargs["turn_complete"])
        self.assertTrue(
            kwargs["turns"]["parts"][0]["text"].startswith(fallback.DROPPED)
        )
        self.assertTrue(
            any(
                "Gemini session reconnected after 1011" in line
                for line in logged.output
            )
        )

    async def test_a_second_1011_ends_the_conversation(self):
        with self.assertLogs("omarchy_ai.voice.satellite", "ERROR"):
            sessions = await self.converse(lambda n: True)
        self.assertEqual(len(sessions), 2)


if __name__ == "__main__":
    unittest.main()
