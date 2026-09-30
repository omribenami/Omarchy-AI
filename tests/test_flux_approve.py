import base64
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

from omarchy_ai.execution import flux_approve


def key_file(public, device='phone1', name='Test Phone'):
    der = public.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    body = base64.encodebytes(der).decode()
    return (f"-----BEGIN PUBLIC KEY-----\nDevice-Id: {device}\nDevice-Name: {name}\n\n{body}"
            "-----END PUBLIC KEY-----\n").encode()


class FakeFluxd:
    """fluxd's approve.request / approve.wait over a real Unix socket. The
    phone's behaviour is `answer(params) -> result dict`."""

    def __init__(self, directory, answer):
        self.path = str(Path(directory) / 'fluxd.sock')
        self.answer = answer
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.thread = threading.Thread(target=self.serve, daemon=True)
        self.thread.start()

    def serve(self):
        conn, _ = self.server.accept()
        params = None
        with conn, conn.makefile('rb') as reader:
            for line in reader:
                req = json.loads(line)
                if req['method'] == 'approve.request':
                    params = req['params']
                    result = {'id': 'req1', 'timeout': 5, 'name': 'Test Phone'}
                else:
                    result = self.answer(params)
                # An unrelated event first: the client must skip it.
                conn.sendall(json.dumps({'event': 'noise'}).encode() + b'\n')
                conn.sendall(json.dumps({'id': req['id'], 'result': result}).encode() + b'\n')

    def close(self):
        self.server.close()


class FluxApproveTests(unittest.TestCase):
    def setUp(self):
        self.phone = ec.generate_private_key(ec.SECP256R1())
        self.key = flux_approve.parse_key(key_file(self.phone.public_key()))
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def sign(self, params, **changes):
        fields = {**params, **changes}
        signed = flux_approve.message(fields['host'], fields['user'], fields['service'], fields['tty'],
                                      fields['rhost'], fields['time'], fields['nonce'])
        return base64.b64encode(self.phone.sign(signed, ec.ECDSA(hashes.SHA256()))).decode()

    def run_request(self, answer, what='Omarchy AI: push the fix'):
        fluxd = FakeFluxd(self.tmp.name, answer)
        self.addCleanup(fluxd.close)
        with patch.dict(os.environ, {'FLUX_SOCKET': fluxd.path}), patch.object(flux_approve, 'read_key', return_value=self.key):
            return flux_approve.request(what)

    def test_parse_key_reads_flux_headers(self):
        self.assertEqual((self.key.device_id, self.key.device_name), ('phone1', 'Test Phone'))
        rsa_like = b"-----BEGIN PUBLIC KEY-----\nDevice-Id: x\n\nAAAA\n-----END PUBLIC KEY-----\n"
        self.assertIsNone(flux_approve.parse_key(rsa_like))
        other_curve = ec.generate_private_key(ec.SECP384R1()).public_key()
        self.assertIsNone(flux_approve.parse_key(key_file(other_curve)))
        no_device = key_file(self.phone.public_key()).replace(b'Device-Id: phone1\n', b'')
        self.assertIsNone(flux_approve.parse_key(no_device))

    def test_message_matches_flux_layout(self):
        self.assertEqual(flux_approve.message('h', 'u', 's', '', '', 12, 'ab'),
                         b'flux-approve-v1\nhost=h\nuser=u\nservice=s\ntty=\nrhost=\ntime=12\nnonce=ab\n')

    def test_fingerprint_approval_is_verified(self):
        seen = {}

        def answer(params):
            seen.update(params)
            return {'state': 'approved', 'signature': self.sign(params)}
        ok, message = self.run_request(answer, what='Omarchy AI: line1\nline2')
        self.assertTrue(ok, message)
        self.assertEqual(seen['device'], 'phone1')
        # A newline in the request cannot add a line to the signed message.
        self.assertNotIn('\n', seen['service'])
        self.assertEqual(len(seen['nonce']), 64)

    def test_denied_on_the_phone(self):
        self.assertEqual(self.run_request(lambda p: {'state': 'denied'})[0], False)

    def test_signature_over_other_fields_is_refused(self):
        # fluxd (or anything running as the user) changes what the phone saw.
        ok, message = self.run_request(lambda p: {'state': 'approved', 'signature': self.sign(p, service='something else')})
        self.assertIsNone(ok)
        self.assertIn('does not match', message)

    def test_signature_from_another_key_is_refused(self):
        other = ec.generate_private_key(ec.SECP256R1())

        def answer(params):
            signed = flux_approve.message(params['host'], params['user'], params['service'], '', '', params['time'], params['nonce'])
            return {'state': 'approved', 'signature': base64.b64encode(other.sign(signed, ec.ECDSA(hashes.SHA256()))).decode()}
        self.assertIsNone(self.run_request(answer)[0])

    def test_no_key_or_no_fluxd_falls_back(self):
        with patch.object(flux_approve, 'read_key', return_value=None):
            self.assertIsNone(flux_approve.request('x')[0])
        with patch.dict(os.environ, {'FLUX_SOCKET': str(Path(self.tmp.name) / 'missing.sock')}), \
                patch.object(flux_approve, 'read_key', return_value=self.key):
            ok, message = flux_approve.request('x')
        self.assertIsNone(ok)
        self.assertIn('not reachable', message)

    def test_read_key_refuses_a_user_owned_file(self):
        path = Path(self.tmp.name) / 'me.pub'
        path.write_bytes(key_file(self.phone.public_key()))
        self.assertIsNone(flux_approve.read_key(path))


class RuntimeFingerprintTests(unittest.TestCase):
    def test_sudo_goes_to_the_phone_without_the_saved_password(self):
        from omarchy_ai.runtime import runtime
        with patch.object(flux_approve, 'sudo_ready', return_value=True), \
                patch('omarchy_ai.execution.sudo_approval.retrieve', return_value='secret') as retrieve:
            command, stdin = runtime._sudo('sudo pacman -Syu')
        self.assertEqual((command, stdin), ("sudo -S -p '' pacman -Syu", ''))
        retrieve.assert_not_called()

    def test_fingerprint_answers_the_waiting_task(self):
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from omarchy_ai.runtime import runtime
        from omarchy_ai.runtime.task import WAITING_APPROVAL
        task = SimpleNamespace(id='t1', goal='push the fix', status=WAITING_APPROVAL,
                               pending_approval={'fingerprint': 'f1', 'risk': 'HIGH'})
        for answer, expected in [((True, 'ok'), True), ((False, 'denied'), False)]:
            rt = MagicMock()
            rt.store.load.return_value = task
            with patch.object(flux_approve, 'request', return_value=answer) as request:
                runtime._fingerprint_prompt(rt, task, 'f1')
            self.assertIn('push the fix', request.call_args.args[0])
            rt.respond.assert_called_once_with('t1', approve=expected, channel='fingerprint')
        # No answer, or a request that changed meanwhile: nothing is decided.
        for answer, fingerprint in [((None, 'timeout'), 'f1'), ((True, 'ok'), 'stale')]:
            rt = MagicMock()
            rt.store.load.return_value = task
            with patch.object(flux_approve, 'request', return_value=answer):
                runtime._fingerprint_prompt(rt, task, fingerprint)
            rt.respond.assert_not_called()

    def test_one_phone_ping_per_approval_and_no_web_page(self):
        # The user, 2026-09-30: Flux got an approval notification and a
        # fingerprint prompt for the same step, and taps opened the web GUI.
        from types import SimpleNamespace
        from unittest.mock import MagicMock
        from omarchy_ai.runtime import runtime
        task = SimpleNamespace(id='t1', goal='push the fix', question='', result='', conversation='',
                               pending_approval={'fingerprint': 'f1', 'risk': 'HIGH', 'subject': 'git push'})
        for fingerprint in (True, False):
            with patch.object(runtime, '_flux_approval_available', return_value=fingerprint), \
                    patch.object(runtime, '_phone') as phone, \
                    patch.object(runtime.threading, 'Thread') as thread:
                runtime._notify(MagicMock(), task, 'waiting_approval')
            targets = [c.kwargs['target'] for c in thread.call_args_list]
            if fingerprint:
                phone.assert_not_called()
                self.assertIn(runtime._fingerprint_prompt, targets)
            else:
                self.assertNotIn(runtime._fingerprint_prompt, targets)
                body = phone.call_args.args[1]
                self.assertIn('on the desktop', body)
                self.assertNotIn('PIN', body)
                self.assertNotIn('in Flux', body)


if __name__ == '__main__':
    unittest.main()


class FluxSettingsTests(unittest.TestCase):
    """Flux's Omarchy button beside a feature that config.toml keeps off."""

    def serve(self, directory, sticky=False):
        path = str(Path(directory) / 'fluxd.sock')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(path)
        server.listen(1)
        self.addCleanup(server.close)
        settings, calls = {'remoteDesktop': False, 'remoteInput': False}, []

        def run():
            conn, _ = server.accept()
            with conn, conn.makefile('rb') as reader:
                for line in reader:
                    req = json.loads(line)
                    calls.append((req['method'], req['params']))
                    if req['method'] == 'settings.set' and not sticky:
                        settings[req['params']['key']] = req['params']['value']
                    result = {'settings': dict(settings)} if req['method'] == 'state' else {}
                    conn.sendall(json.dumps({'id': req['id'], 'result': result}).encode() + b'\n')
        threading.Thread(target=run, daemon=True).start()
        return path, calls

    def test_turns_on_through_fluxd_and_confirms(self):
        from omarchy_ai.phone import flux_settings
        with tempfile.TemporaryDirectory() as directory:
            path, calls = self.serve(directory)
            with patch.dict(os.environ, {'FLUX_SOCKET': path}):
                ok, message = flux_settings.enable(['remote_desktop'])
        self.assertTrue(ok, message)
        self.assertEqual(calls[0], ('settings.set', {'key': 'remoteDesktop', 'value': True}))
        self.assertEqual(calls[-1][0], 'state')

    def test_refusals(self):
        from omarchy_ai.phone import flux_settings
        self.assertFalse(flux_settings.enable(['share_home'])[0])
        self.assertFalse(flux_settings.enable([])[0])
        with tempfile.TemporaryDirectory() as directory:
            path, _ = self.serve(directory, sticky=True)
            with patch.dict(os.environ, {'FLUX_SOCKET': path}):
                ok, message = flux_settings.enable(['remote_input'])
            self.assertFalse(ok)
            self.assertIn('still reports remote_input as off', message)
            with patch.dict(os.environ, {'FLUX_SOCKET': str(Path(directory) / 'none.sock')}):
                self.assertIn('not running', flux_settings.enable(['remote_input'])[1])


class FluxNotifyTests(unittest.TestCase):
    def test_goes_to_each_paired_connected_phone(self):
        from omarchy_ai.phone import flux_notify
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / 'fluxd.sock')
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(path)
            server.listen(1)
            self.addCleanup(server.close)
            sent = []
            devices = [{'id': 'p1', 'name': 'Phone', 'paired': True, 'online': True},
                       {'id': 'p2', 'name': 'Old', 'paired': True, 'online': False},
                       {'id': 'p3', 'name': 'Stranger', 'paired': False, 'online': True}]

            def run():
                conn, _ = server.accept()
                with conn, conn.makefile('rb') as reader:
                    for line in reader:
                        req = json.loads(line)
                        if req['method'] == 'notify.send':
                            sent.append(req['params'])
                        result = {'devices': devices} if req['method'] == 'state' else {}
                        conn.sendall(json.dumps({'id': req['id'], 'result': result}).encode() + b'\n')
            threading.Thread(target=run, daemon=True).start()
            with patch.dict(os.environ, {'FLUX_SOCKET': path}):
                self.assertEqual(flux_notify.deliver('Routine ran', 'backup done'), 1)
        self.assertEqual(sent, [{'device': 'p1', 'title': 'Omarchy AI · Routine ran', 'body': 'backup done'}])

    def test_approval_has_a_fixed_marker(self):
        from omarchy_ai.phone import flux_notify
        with patch.object(flux_notify, '_enabled', return_value=True), \
                patch('omarchy_ai.execution.flux_approve.Fluxd') as fluxd:
            client = fluxd.return_value
            client.call.side_effect = [
                {'devices': [{'id': 'p1', 'paired': True, 'online': True}]},
                {},
            ]
            self.assertEqual(flux_notify.deliver('Task needs approval', 'HIGH: command', 'approval'), 1)
            self.assertEqual(
                client.call.call_args_list[1].args[:2],
                ('notify.send', {'device': 'p1', 'title': 'Omarchy AI · Approval: Task needs approval', 'body': 'HIGH: command'}),
            )

    def test_without_flux_nothing_happens(self):
        from omarchy_ai.phone import flux_notify
        with patch.dict(os.environ, {'FLUX_SOCKET': '/nonexistent/fluxd.sock'}):
            self.assertEqual(flux_notify.deliver('Omarchy task done', ''), 0)
