"""Real local sockets: an idle TLS connection must not freeze the phone UI."""
import datetime
import socket
import ssl
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from omarchy_ai.phone.server import PhoneHTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header('Content-Length', '2')
        self.end_headers()
        self.wfile.write(b'ok')

    def log_message(self, *_):
        pass


class PhoneTLSTests(unittest.TestCase):
    def test_idle_handshake_does_not_block_https_and_expires(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'localhost')])
        now = datetime.datetime.now(datetime.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                .public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - datetime.timedelta(minutes=1))
                .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'key.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            (root/'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(root/'cert.pem', root/'key.pem')
            httpd = PhoneHTTPServer(('127.0.0.1', 0), Handler)
            httpd.tls_context = context
            httpd.handshake_timeout = .5
            accepted = threading.Event()
            original = httpd.process_request_thread
            def worker(request, address):
                accepted.set()
                original(request, address)
            httpd.process_request_thread = worker
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            idle = socket.create_connection(httpd.server_address, timeout=2)
            try:
                self.assertTrue(accepted.wait(1))
                # The first socket never sends a ClientHello. A separate HTTPS
                # client must still complete before the idle socket is closed.
                with urllib.request.urlopen('https://127.0.0.1:%d/' % httpd.server_port,
                        context=ssl._create_unverified_context(), timeout=2) as response:
                    self.assertEqual(response.read(), b'ok')
                self.assertEqual(idle.recv(1), b'')
            finally:
                idle.close()
                httpd.shutdown()
                httpd.server_close()
                thread.join(2)
            self.assertFalse(thread.is_alive())
