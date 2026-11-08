# MIT License
# Copyright (c) 2026 Shane & Contributors

"""Real TLS handshakes against a temporary local CA; no external services."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import ssl
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'files'))
from managed_api import CertificateError, Deadline, TechnitiumClient

MEMBER = 'dns-technitium-cluster-0.cluster.example.test'


class TLSHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requests.append(self.path)
        body = json.dumps({'status': 'ok', 'response': {
            'clusterInitialized': False, 'dnsServerDomain': MEMBER,
        }}).encode()
        self.send_response(200)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_):
        pass


class ProjectionTests(unittest.TestCase):
    def test_directory_projection_exposes_renewal_timestamp(self):
        # Simulate kubelet's atomic ..data swap with dummy data, not PKCS#12 keys.
        # Technitium's Unix FileInfo timestamp reads lstat of the final component.
        with tempfile.TemporaryDirectory(prefix='technitium-projection-test-') as directory:
            root = Path(directory)
            for revision, mtime in (('v1', 100), ('v2', 200)):
                target = root / revision
                (target / 'server').mkdir(parents=True)
                for path in (target / 'keystore.p12', target / 'server/keystore.p12'):
                    path.write_text(revision)
                    os.utime(path, (mtime, mtime))
            (root / '..data').symlink_to('v1', target_is_directory=True)
            (root / 'keystore.p12').symlink_to('..data/keystore.p12')
            (root / 'server').symlink_to('..data/server', target_is_directory=True)
            flat, nested = root / 'keystore.p12', root / 'server/keystore.p12'
            flat_mtime = flat.lstat().st_mtime_ns
            self.assertEqual(nested.lstat().st_mtime, 100)
            (root / '..data_tmp').symlink_to('v2', target_is_directory=True)
            (root / '..data_tmp').replace(root / '..data')
            self.assertEqual(flat.read_text(), 'v2')
            self.assertEqual(nested.read_text(), 'v2')
            self.assertEqual(flat.lstat().st_mtime_ns, flat_mtime)
            self.assertTrue(stat.S_ISREG(nested.lstat().st_mode))
            self.assertEqual(nested.lstat().st_mtime, 200)


class TLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix='technitium-tls-test-')
        cls.addClassCleanup(cls.directory.cleanup)
        cls.root = Path(cls.directory.name)
        (cls.root / 'request.cnf').write_text('[req]\ndistinguished_name=dn\n[dn]\n')

        def openssl(*args):
            subprocess.run(['openssl', *args], cwd=cls.root, check=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

        openssl('req', '-config', 'request.cnf', '-x509', '-newkey', 'rsa:2048', '-nodes', '-sha256', '-days', '2',
                '-subj', '/CN=Temporary Test CA', '-keyout', 'ca.key', '-out', 'ca.crt',
                '-addext', 'basicConstraints=critical,CA:TRUE',
                '-addext', 'keyUsage=critical,keyCertSign,cRLSign', '-addext', 'subjectKeyIdentifier=hash')
        openssl('req', '-config', 'request.cnf', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', f'/CN={MEMBER}',
                '-keyout', 'server.key', '-out', 'server.csr')
        (cls.root / 'extensions').write_text(
            f'subjectAltName=DNS:{MEMBER}\nbasicConstraints=critical,CA:FALSE\n'
            'keyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n'
            'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid,issuer\n')
        for name, days in (('server', '2'), ('expired', '-1')):
            openssl('x509', '-req', '-in', 'server.csr', '-CA', 'ca.crt', '-CAkey', 'ca.key',
                    '-CAcreateserial', '-out', f'{name}.crt', '-days', days, '-sha256',
                    '-extfile', 'extensions')

    @contextmanager
    def server(self, cert='server'):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(self.root / f'{cert}.crt'), str(self.root / 'server.key'))
        with HTTPServer(('127.0.0.1', 0), TLSHandler) as server:
            server.names, server.requests = [], []
            context.set_servername_callback(lambda sock, name, ctx: server.names.append(name))
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.02}, daemon=True)
            thread.start()
            try:
                yield server
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def client(self, server, **options):
        return TechnitiumClient('127.0.0.1', server.server_port, 'unused', token='synthetic', **options)

    def test_trusted_ca_and_member_dns_san_work_over_an_ip_connection(self):
        with self.server() as server:
            client = self.client(server, ca_file=str(self.root / 'ca.crt'), server_name=MEMBER)
            self.assertEqual(client.context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(client.context.check_hostname)
            # Peer traffic must still go directly to the configured IP.
            with mock.patch.dict('os.environ', {'HTTPS_PROXY': 'http://127.0.0.1:1', 'NO_PROXY': ''}):
                self.assertEqual(client.state(Deadline(5)).name, MEMBER)
            self.assertEqual(server.names, [MEMBER])

    def test_unknown_ca_is_rejected_without_sending_an_http_request(self):
        with self.server() as server:
            client = self.client(server, server_name=MEMBER)
            with self.assertRaises(CertificateError):
                deadline = Deadline(5)
                deadline.retry(lambda: client.state(deadline))
            self.assertEqual(server.names, [MEMBER])  # certificate errors are not retried
            self.assertEqual(server.requests, [])

    def test_wrong_member_name_is_rejected(self):
        with self.server() as server:
            client = self.client(server, ca_file=str(self.root / 'ca.crt'), server_name='other.example.test')
            with self.assertRaises(CertificateError):
                client.state(Deadline(5))
            self.assertEqual(server.requests, [])

    def test_expired_certificate_is_rejected(self):
        with self.server('expired') as server:
            client = self.client(server, ca_file=str(self.root / 'ca.crt'), server_name=MEMBER)
            with self.assertRaises(CertificateError):
                client.state(Deadline(5))
            self.assertEqual(server.requests, [])

    def test_explicit_opt_out_accepts_an_untrusted_certificate(self):
        with self.server() as server:
            client = self.client(server, verify_tls=False, server_name='other.example.test')
            self.assertFalse(client.state(Deadline(5)).initialized)
            self.assertEqual(client.context.verify_mode, ssl.CERT_NONE)


if __name__ == '__main__':
    unittest.main()
