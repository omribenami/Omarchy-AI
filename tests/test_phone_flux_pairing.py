import base64
import datetime
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID

from omarchy_ai.phone import flux_pairing


def make_device(device_id, key):
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(1)
            .not_valid_before(now - datetime.timedelta(days=1))
            .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
    return {'id': device_id, 'name': 'Phone', 'type': 'phone',
            'certificate': cert.public_bytes(serialization.Encoding.PEM).decode()}


def sign(key, text):
    if isinstance(key, rsa.RSAPrivateKey):
        signature = key.sign(text, padding.PKCS1v15(), hashes.SHA256())
    else:
        signature = key.sign(text, ec.ECDSA(hashes.SHA256()))
    return base64.b64encode(signature).decode()


class FluxPairingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.dict(os.environ, {'XDG_DATA_HOME': self.tmp.name})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.write_devices([make_device('phone1', self.key)])

    def write_devices(self, devices):
        path = Path(self.tmp.name) / 'flux' / 'devices.json'
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(devices))

    def attempt(self, device='phone1', key=None, pin='PIN', signed_pin='PIN', signed_device=None):
        nonce = flux_pairing.new_challenge()
        text = flux_pairing.signed_text(nonce, signed_pin, signed_device or device)
        return nonce, flux_pairing.verify(device, nonce, sign(key or self.key, text), pin)

    def test_paired_phone_is_accepted_once_per_challenge(self):
        nonce, ok = self.attempt()
        self.assertTrue(ok)
        text = flux_pairing.signed_text(nonce, 'PIN', 'phone1')
        self.assertFalse(flux_pairing.verify('phone1', nonce, sign(self.key, text), 'PIN'))

    def test_elliptic_curve_devices_work(self):
        key = ec.generate_private_key(ec.SECP256R1())
        self.write_devices([make_device('mac1', key)])
        self.assertTrue(self.attempt(device='mac1', key=key)[1])

    def test_refusals(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        # A key that desktop Flux did not pin for this device.
        self.assertFalse(self.attempt(key=other)[1])
        # A device that desktop Flux never paired.
        self.assertFalse(self.attempt(device='stranger')[1])
        # Signed for another server's key: a relayed signature.
        self.assertFalse(self.attempt(signed_pin='OTHER')[1])
        # Signed for another device ID.
        self.assertFalse(self.attempt(signed_device='phone2')[1])
        # A challenge this server never issued.
        text = flux_pairing.signed_text('made-up', 'PIN', 'phone1')
        self.assertFalse(flux_pairing.verify('phone1', 'made-up', sign(self.key, text), 'PIN'))
        # Garbage signature and wrong types.
        nonce = flux_pairing.new_challenge()
        self.assertFalse(flux_pairing.verify('phone1', nonce, 'not base64!', 'PIN'))
        self.assertFalse(flux_pairing.verify(None, flux_pairing.new_challenge(), 'x', 'PIN'))

    def test_expired_challenge_is_refused(self):
        with patch.object(flux_pairing.time, 'time', return_value=1000.0):
            nonce = flux_pairing.new_challenge()
        text = flux_pairing.signed_text(nonce, 'PIN', 'phone1')
        with patch.object(flux_pairing.time, 'time', return_value=1000.0 + 61):
            self.assertFalse(flux_pairing.verify('phone1', nonce, sign(self.key, text), 'PIN'))

    def test_unpaired_or_missing_flux_store_refuses(self):
        (Path(self.tmp.name) / 'flux' / 'devices.json').unlink()
        self.assertFalse(self.attempt()[1])
        self.write_devices({'not': 'a list'})
        self.assertFalse(self.attempt()[1])

    def test_key_pin_matches_the_certificate_key(self):
        cert_pem = make_device('server', self.key)['certificate']
        path = Path(self.tmp.name) / 'cert.pem'
        path.write_text(cert_pem)
        import hashlib
        spki = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        self.assertEqual(flux_pairing.key_pin(path), base64.b64encode(hashlib.sha256(spki).digest()).decode())


class FluxPairingOverHttpsTests(unittest.TestCase):
    """The real handler over real TLS: challenge, signed pair, then the page."""

    def test_signed_pairing_opens_the_page(self):
        import http.client
        import ssl
        import threading
        from omarchy_ai.phone import server
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            phone_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            (root / 'flux').mkdir()
            (root / 'flux' / 'devices.json').write_text(json.dumps([make_device('phone1', phone_key)]))
            with patch.dict(os.environ, {'XDG_DATA_HOME': str(root)}), \
                    patch.object(server, '_CERT_DIR', root), patch.object(server, '_CERT_PATH', root / 'cert.pem'), \
                    patch.object(server, '_KEY_PATH', root / 'key.pem'), patch.object(server, '_SESSIONS_PATH', root / 'sessions.json'), \
                    patch.object(server, '_primary_lan_ip', return_value='127.0.0.1'), patch.object(server, '_tailscale_address', return_value=(None, None)):
                cert, key = server._ensure_self_signed_cert()
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
                context.load_cert_chain(cert, key)
                httpd = server.PhoneHTTPServer(('127.0.0.1', 0), server._Handler)
                httpd.tls_context = context
                thread = threading.Thread(target=httpd.serve_forever, daemon=True)
                thread.start()
                try:
                    def request(method, path, body=None, headers=None):
                        conn = http.client.HTTPSConnection('127.0.0.1', httpd.server_port, context=ssl._create_unverified_context(), timeout=5)
                        conn.request(method, path, body=body, headers=headers or {})
                        response = conn.getresponse()
                        return response, response.read()

                    response, body = request('GET', '/api/flux/challenge')
                    challenge = json.loads(body)
                    self.assertEqual(challenge['key'], flux_pairing.key_pin(Path(cert)))
                    text = flux_pairing.signed_text(challenge['nonce'], challenge['key'], 'phone1')
                    payload = json.dumps({'device': 'phone1', 'nonce': challenge['nonce'], 'signature': sign(phone_key, text)})
                    response, _ = request('POST', '/api/flux/pair', payload, {'Content-Type': 'application/json'})
                    self.assertEqual(response.status, 200)
                    cookie = response.getheader('Set-Cookie').split(';', 1)[0]
                    response, page = request('GET', '/', headers={'Cookie': cookie})
                    self.assertIn(b'id="talkBtn"', page)

                    # The desktop's wake word, for paired phones only.
                    self.assertEqual(request('GET', '/api/wake')[0].status, 403)
                    from types import SimpleNamespace
                    with patch.object(server._Handler, 'config', SimpleNamespace(wake_threshold=0.5), create=True), \
                            patch.object(server.wake_models, 'files', return_value={
                            'melspectrogram': Path(cert), 'embedding': Path(cert), 'wake': Path(cert)}):
                        response, body = request('GET', '/api/wake', headers={'Cookie': cookie})
                        self.assertEqual(response.status, 200)
                        self.assertEqual(json.loads(body)['name'], 'cert')
                        response, body = request('GET', '/api/wake/wake.onnx', headers={'Cookie': cookie})
                        self.assertEqual(body, Path(cert).read_bytes())
                        self.assertEqual(request('GET', '/api/wake/other.onnx', headers={'Cookie': cookie})[0].status, 404)

                    # Mirror to TV and "ask Omarchy", for paired phones only.
                    from omarchy_ai.execution.actions import ActionResult
                    self.assertEqual(request('GET', '/api/tvs')[0].status, 403)
                    self.assertEqual(request('POST', '/api/ask', '{"request": "x"}')[0].status, 403)
                    tvs = [{'name': 'Living Room TV', 'address': '192.0.2.9', 'status': 'online', 'last_seen': 1}]
                    with patch('omarchy_ai.display.registry.get_or_refresh', return_value=tvs), \
                            patch('omarchy_ai.display.session.active', return_value=False):
                        body = json.loads(request('GET', '/api/tvs', headers={'Cookie': cookie})[1])
                    self.assertEqual(body, {'tvs': tvs, 'casting': None})
                    with patch.object(server, 'run_action', return_value=ActionResult(True, 'casting connected')) as run:
                        response, body = request('POST', '/api/cast', '{"target": "Living Room TV"}', {'Cookie': cookie})
                        self.assertEqual((response.status, json.loads(body)['ok']), (200, True))
                        run.assert_called_with('start_casting', {'target': 'Living Room TV'})
                        request('POST', '/api/cast/stop', '{}', {'Cookie': cookie})
                        run.assert_called_with('stop_casting', {})
                    with patch('omarchy_ai.runtime.service.start_task', return_value=ActionResult(True, '{}')) as task:
                        response, _ = request('POST', '/api/ask', '{"request": "turn on remote input"}', {'Cookie': cookie})
                        self.assertEqual(response.status, 200)
                        task.assert_called_once_with({'goal': 'turn on remote input'})
                        self.assertEqual(request('POST', '/api/ask', '{"request": " "}', {'Cookie': cookie})[0].status, 400)

                    # The same signature again: the challenge is spent.
                    response, _ = request('POST', '/api/flux/pair', payload, {'Content-Type': 'application/json'})
                    self.assertEqual(response.status, 403)
                    response, _ = request('POST', '/api/flux/pair', 'x' * 9000, {'Content-Type': 'application/json'})
                    self.assertEqual(response.status, 400)
                finally:
                    httpd.shutdown()
                    httpd.server_close()


if __name__ == '__main__':
    unittest.main()
