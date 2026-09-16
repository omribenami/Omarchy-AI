import asyncio
import json
import os
from pathlib import Path
import socket
import tempfile
import time
import unittest
from unittest.mock import patch

from omarchy_ai.display import session
from omarchy_ai.display.signaling import Relay
from omarchy_ai.myapi.client import MyApiClient, MyApiError
from omarchy_ai.voice import tv_mic


class ProxyTests(unittest.TestCase):
    def test_query_parameters_are_text(self):
        client = MyApiClient()
        with patch.object(client, 'request', return_value={'ok': True}) as request:
            client.call_service('gmail', '/gmail/v1/users/me/messages', query={
                'maxResults': 10, 'includeSpamTrash': False, 'q': 'in:inbox', 'unused': None})
        self.assertEqual(request.call_args.kwargs['body'], {
            'path': '/gmail/v1/users/me/messages', 'method': 'GET',
            'query': {'maxResults': '10', 'includeSpamTrash': 'false', 'q': 'in:inbox'}})

    def test_nested_query_is_not_silently_stringified(self):
        with self.assertRaises(MyApiError):
            MyApiClient().call_service('gmail', '/x', query={'q': {'unexpected': 1}})

    def test_provider_failure_in_http_200_is_failure(self):
        client = MyApiClient()
        from unittest.mock import MagicMock
        response = MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":false,"error":"denied"}'
        with patch.object(client, '_sign_headers', return_value={}), patch('urllib.request.urlopen', return_value=response):
            with self.assertRaisesRegex(MyApiError, 'denied'):
                client.request('GET', '/services')


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {'XDG_RUNTIME_DIR': self.tmp.name})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_lock_is_shared_by_independent_callers(self):
        with session.locked():
            with self.assertRaisesRegex(RuntimeError, 'operation is in progress'):
                with session.locked():
                    pass

    def test_alive_sender_is_not_connection_success(self):
        session.write_state('starting')
        with patch.object(session, 'active', return_value=True):
            ok, reason = session.wait_connected(.01)
        self.assertFalse(ok)
        self.assertIn('timed out', reason)

    def test_failed_sender_reports_reason(self):
        session.write_state('failed', 'capture unavailable')
        with patch.object(session, 'active', return_value=False):
            self.assertEqual(session.wait_connected(), (False, 'capture unavailable'))

    def test_recover_wayland_socket(self):
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.bind(str(Path(self.tmp.name) / 'wayland-7'))
            with patch.dict(os.environ, {'WAYLAND_DISPLAY': 'wayland-missing'}):
                self.assertEqual(session.desktop_environment()['WAYLAND_DISPLAY'], 'wayland-7')
        finally:
            sock.close()

    def test_tv_microphone_pcm_resampling_and_fallback(self):
        import numpy as np
        receiver = tv_mic.Receiver(16000)
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            self.assertIsNone(receiver.read(2560))
            pcm = np.full(960, 1200, dtype=np.int16).tobytes()
            for _ in range(5):
                tv_mic.forward(sender, pcm)
            deadline = time.monotonic() + 2
            while len(receiver.buffer) < 2560 and time.monotonic() < deadline:
                time.sleep(.01)
            frame = receiver.read(2560)
            self.assertEqual(len(frame), 2560)
            self.assertGreater(np.frombuffer(frame, dtype=np.int16).mean(), 900)
            tv_mic.forward(sender, b'')
            time.sleep(.05)
            self.assertIsNone(receiver.read(2560))
        finally:
            sender.close()
            receiver.close()


class RelayTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_stale_offer_after_sender_disconnect(self):
        import websockets
        from websockets.asyncio.server import serve
        relay = Relay()
        async with serve(relay.handle, '127.0.0.1', 0) as server:
            url = f'ws://127.0.0.1:{server.sockets[0].getsockname()[1]}'
            async with websockets.connect(url) as sender:
                await sender.send(json.dumps({'role': 'sender'}))
                await sender.send(json.dumps({'type': 'offer', 'sdp': 'old'}))
                await asyncio.sleep(.05)
                self.assertIsNotNone(relay.pending_offer)
            await asyncio.sleep(.05)
            self.assertIsNone(relay.pending_offer)
            async with websockets.connect(url) as viewer, websockets.connect(url) as sender:
                await viewer.send(json.dumps({'role': 'viewer'}))
                await sender.send(json.dumps({'role': 'sender'}))
                await sender.send(json.dumps({'type': 'offer', 'sdp': 'new'}))
                messages = []
                while not any(m.get('type') == 'offer' for m in messages):
                    messages.append(json.loads(await asyncio.wait_for(viewer.recv(), 2)))
                self.assertEqual(messages[-1]['sdp'], 'new')
                await sender.close()
                self.assertEqual(json.loads(await asyncio.wait_for(viewer.recv(), 2))['type'], 'bye')


if __name__ == '__main__':
    unittest.main()
