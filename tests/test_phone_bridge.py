import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from omarchy_ai.phone import server, cast_audio


class PhoneBridgeTests(unittest.TestCase):
    def test_tailscale_offline_and_missing(self):
        with patch.object(server.subprocess, 'run', side_effect=FileNotFoundError):
            self.assertEqual(server._tailscale_address(), (None, None))
        with patch.object(server.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='{"BackendState":"Stopped"}')):
            self.assertEqual(server._tailscale_address(), (None, None))

    def test_tailscale_discovery(self):
        status = {'BackendState': 'Running', 'Self': {'TailscaleIPs': ['100.64.0.1', 'fd7a::1'], 'DNSName': 'host.tail.ts.net.'}}
        with patch.object(server.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout=json.dumps(status))):
            self.assertEqual(server._tailscale_address(), ('100.64.0.1', 'host.tail.ts.net'))

    def test_pairing_prefers_tailnet_with_lan_fallback(self):
        for address in [('100.64.0.1', 'host.tail.ts.net'), (None, None)]:
            with patch.object(server, '_write_json_0600'), patch.object(server, '_primary_lan_ip', return_value='192.0.2.20'), patch.object(server, '_tailscale_address', return_value=address):
                info = server.mint_pairing_token(SimpleNamespace(phone_bridge_port=8766))
                self.assertIn(address[0] or '192.0.2.20', info['url'])
                self.assertIn('192.0.2.20', info['lan_url'])
                self.assertIn(info['token'], info['url'])

    def test_certificate_includes_both_networks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, '_CERT_DIR', root), patch.object(server, '_CERT_PATH', root/'cert.pem'), patch.object(server, '_KEY_PATH', root/'key.pem'), patch.object(server, '_primary_lan_ip', return_value='192.0.2.20'), patch.object(server, '_tailscale_address', return_value=('100.64.0.1', 'host.tail.ts.net')), patch.object(server.shutil, 'which', return_value='/usr/bin/openssl'), patch.object(server.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
                self.assertIsNotNone(server._ensure_self_signed_cert())
                san = run.call_args.args[0][-1]
                for name in ['IP:192.0.2.20', 'IP:100.64.0.1', 'DNS:host.tail.ts.net']:
                    self.assertIn(name, san)

    @unittest.skipUnless(server.shutil.which('openssl'), 'needs openssl')
    def test_reissued_certificate_keeps_the_key(self):
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        def public_key(path):
            return x509.load_pem_x509_certificate(path.read_bytes()).public_key().public_bytes(
                serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with patch.object(server, '_CERT_DIR', root), patch.object(server, '_CERT_PATH', root/'cert.pem'), patch.object(server, '_KEY_PATH', root/'key.pem'), patch.object(server, '_tailscale_address', return_value=(None, None)):
                with patch.object(server, '_primary_lan_ip', return_value='192.0.2.20'):
                    self.assertIsNotNone(server._ensure_self_signed_cert())
                first = public_key(root/'cert.pem')
                with patch.object(server, '_primary_lan_ip', return_value='192.0.2.21'):
                    self.assertIsNotNone(server._ensure_self_signed_cert())
                self.assertEqual(public_key(root/'cert.pem'), first)
                san = x509.load_pem_x509_certificate((root/'cert.pem').read_bytes()).extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                self.assertIn('192.0.2.21', [str(ip) for ip in san.get_values_for_type(x509.IPAddress)])

    def test_pcm_rejects_invalid_sizes(self):
        for packet in [b'', b'x', bytes(8194)]:
            with self.assertRaises(ValueError):
                cast_audio.send_pcm(packet)

    def test_phone_notification_context_is_separate_from_user_input(self):
        from omarchy_ai.phone.gemini import _phone_message
        self.assertEqual(
            _phone_message(json.dumps({'type': 'omarchy.context', 'text': 'Build failed\nexit 1'})),
            ('context', 'Build failed\nexit 1'),
        )
        self.assertEqual(
            _phone_message(json.dumps({'type': 'response.item.create', 'item': {
                'type': 'message', 'role': 'user', 'content': [{'type': 'input_text', 'text': 'why?'}],
            }})),
            ('user', 'why?'),
        )
        self.assertIsNone(_phone_message(json.dumps({'type': 'response.create'})))


if __name__ == '__main__':
    unittest.main()
