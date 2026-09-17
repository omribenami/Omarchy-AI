import asyncio
import os
import tempfile
import threading
import unittest
from unittest.mock import patch

from omarchy_ai.myapi import dashboard
from omarchy_ai.voice import control


class DashboardTests(unittest.TestCase):
    def test_capability_demo_detection_requires_a_demo_request(self):
        from omarchy_ai.voice.live import _is_capability_demo

        self.assertTrue(_is_capability_demo("Please demonstrate your capabilities"))
        self.assertTrue(_is_capability_demo("Show me what you can do"))
        self.assertFalse(_is_capability_demo("What can you do?"))

    def test_real_account_endpoint_and_period(self):
        payload = {'grand': 123, 'devices': [], 'allDaily': [123], 'serviceTotals': []}
        with patch.object(dashboard.MyApiClient, 'request', return_value={'data': payload}) as request:
            result = dashboard.fetch('30d')
        request.assert_called_once_with('GET', '/dashboard/device-activity', query={'range': '30d'})
        self.assertEqual(result['grand'], 123)

    def test_missing_dashboard_is_not_zero_usage(self):
        with patch.object(dashboard.MyApiClient, 'request', return_value={}):
            with self.assertRaises(dashboard.MyApiError):
                dashboard.fetch()

    def test_invalid_period_never_reaches_network(self):
        with patch.object(dashboard.MyApiClient, 'request') as request:
            with self.assertRaises(ValueError):
                dashboard.fetch('year')
            request.assert_not_called()


class ControlTests(unittest.IsolatedAsyncioTestCase):
    async def test_local_activation_ack_and_busy_guard(self):
        from omarchy_ai.core.daemon import OmaDaemon
        daemon = OmaDaemon.__new__(OmaDaemon)
        daemon._state = 'listening'
        daemon._manual = False
        daemon._listen_stop = threading.Event()
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, XDG_RUNTIME_DIR=directory):
            server = await control.serve(daemon.panel_command)
            async with server:
                self.assertEqual(control.socket_path().stat().st_mode & 0o777, 0o600)
                result = await asyncio.to_thread(control.request, 'status')
                self.assertEqual(result['state'], 'listening')
                self.assertFalse(daemon._manual)
                result = await asyncio.to_thread(control.request, 'activate')
                self.assertEqual(result['state'], 'starting')
                self.assertTrue(daemon._manual)
                self.assertTrue(daemon._listen_stop.is_set())
                result = await asyncio.to_thread(control.request, 'activate')
                self.assertIn('error', result)

    async def test_manual_activation_uses_existing_session_loop(self):
        from omarchy_ai.core.daemon import OmaDaemon
        from unittest.mock import MagicMock, AsyncMock
        daemon = OmaDaemon.__new__(OmaDaemon)
        from types import SimpleNamespace
        daemon.config = SimpleNamespace(provider="openai")
        daemon._stop = threading.Event()
        daemon._listen_stop = threading.Event()
        daemon._manual = True
        daemon._state = 'starting'
        daemon.wake_detector = MagicMock()
        daemon.wake_detector.model_names = ['omachy']
        daemon.wake_detector.listen.return_value = False
        session = MagicMock()
        async def finish():
            daemon._stop.set()
        session.run = AsyncMock(side_effect=finish)
        with patch('omarchy_ai.core.daemon.LiveSession', return_value=session) as factory, patch('omarchy_ai.core.daemon.feedback.play'):
            await daemon._run_sessions()
        factory.assert_called_once_with(daemon.config, mic_source='desktop')
        session.run.assert_awaited_once()
        self.assertEqual(daemon._state, 'listening')


if __name__ == '__main__':
    unittest.main()
