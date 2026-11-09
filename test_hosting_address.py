"""公网源站 IP 与 CDN 入口独立、失败缓存、托管清单准备回归。

使用固定模拟公网地址与临时目录，不调用外部探测服务或部署凭据。
Test: python3 -B -m unittest test_hosting_address
"""
import ast
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import MagicMock, patch

from build_single import build
from test_support import fixture_app, configure, ROOT


class HostingAddressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='emos-hosting-address-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        spec = importlib.util.spec_from_file_location('fresh_runtime_' + str(id(self)), ROOT / 'runtime_info.py')
        self.info = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.info)

    @staticmethod
    def response(body, status=200):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.read.return_value = body
        return response

    def test_public_ip_probe_runs_even_with_cdn_management_address_and_is_cached(self):
        app = fixture_app(self, self.root / 'app', 'cdn_address_app', single_owner=True)
        configure(app, public_base='https://cdn.fixture.invalid', http_enabled=1)
        stopped = threading.Event()
        callback = MagicMock()
        with patch.object(self.info.urllib.request, 'urlopen', return_value=self.response(b'8.8.8.8')) as fetch:
            probe = self.info.start_public_ip_probe('172.19.0.203', stopped, callback)
            probe.join(2)
            self.assertFalse(probe.is_alive())
            self.assertEqual(self.info.public_ip_status(), {'server_public_ip': '8.8.8.8', 'public_ip_status': 'ready'})
            self.assertEqual(self.info.management_url(app.cfg_all()['public_base'], '8.8.8.8', 8080),
                             'https://cdn.fixture.invalid/admin')
            self.assertIs(self.info.start_public_ip_probe('172.19.0.203', stopped), probe)
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.kwargs['timeout'], 2)
            request = fetch.call_args.args[0]
            self.assertIsNone(request.data)
            self.assertFalse(request.has_header('Authorization'))
            self.assertFalse(request.has_header('Cookie'))
            callback.assert_called_once_with('8.8.8.8')

    def test_global_local_ip_has_no_external_call_and_private_ip_is_never_public(self):
        with patch.object(self.info.urllib.request, 'urlopen') as fetch:
            self.assertEqual(self.info.detect_public_ip('8.8.4.4'), '8.8.4.4')
            fetch.assert_not_called()
        self.assertIsNone(self.info.management_url('', '172.19.0.203', 8080))
        self.assertEqual(self.info.management_url('', '8.8.4.4', 8080), 'http://8.8.4.4:8080/admin')

    def test_invalid_empty_html_private_and_oversize_results_are_not_public_ip(self):
        for payload in (b'', b'172.19.0.203', b'127.0.0.1', b'224.0.0.1', b'239.1.2.3',
                        b'240.0.0.1', b'<html>denied</html>', b'x' * 97, b'2001:4860:4860::8888'):
            with self.subTest(payload=payload), patch.object(self.info.urllib.request, 'urlopen',
                    return_value=self.response(payload)) as fetch:
                self.assertIsNone(self.info.detect_public_ip('172.19.0.203'))
                self.assertEqual(fetch.call_count, 2)

    def test_network_failure_has_bounded_attempts_and_failure_is_cached(self):
        stopped = threading.Event()
        with patch.object(self.info.urllib.request, 'urlopen', side_effect=OSError('offline')) as fetch:
            probe = self.info.start_public_ip_probe('172.19.0.203', stopped)
            probe.join(2)
            self.assertEqual(self.info.public_ip_status(), {'server_public_ip': None, 'public_ip_status': 'unavailable'})
            self.assertIs(self.info.start_public_ip_probe('172.19.0.203', stopped), probe)
            self.assertEqual(fetch.call_count, 2)
        stopped.set()
        with patch.object(self.info.urllib.request, 'urlopen') as fetch:
            self.assertIsNone(self.info.detect_public_ip('172.19.0.203', stopped))
            fetch.assert_not_called()

    def test_nonessential_probe_thread_failure_is_cached_not_fatal(self):
        with patch.object(self.info.threading.Thread, 'start', side_effect=RuntimeError('thread unavailable')):
            thread = self.info.start_public_ip_probe('172.19.0.203', threading.Event())
            self.assertFalse(thread.is_alive())
            self.assertEqual(self.info.public_ip_status(),
                             {'server_public_ip': None, 'public_ip_status': 'unavailable'})
            self.assertIs(self.info.start_public_ip_probe('172.19.0.203', threading.Event()), thread)

    def test_prepare_requirements_preserves_existing_file_does_not_touch_env(self):
        entry_root, cwd = self.root / 'runtime', self.root / 'cwd'
        entry_root.mkdir()
        cwd.mkdir()
        entry = entry_root / 'emoskata.py'
        env = entry_root / '.env'
        env.write_bytes(b'# deployment configuration\n')
        requirements = entry_root / 'requirements.txt'
        requirements.write_bytes(b'# supplied deployment list\n')
        with patch.object(self.info.Path, 'cwd', return_value=cwd):
            first = self.info.prepare_requirements(entry)
            second = self.info.prepare_requirements(entry)
        self.assertEqual(env.read_bytes(), b'# deployment configuration\n')
        self.assertEqual(requirements.read_bytes(), b'# supplied deployment list\n')
        self.assertTrue((cwd / 'requirements.txt').is_file())
        self.assertEqual([created for _, created in first], [False, True])
        self.assertEqual([created for _, created in second], [False, False])
        requirements.unlink()
        requirements.symlink_to(env)
        with patch.object(self.info.Path, 'cwd', return_value=cwd), self.assertRaises(OSError):
            self.info.prepare_requirements(entry)
        self.assertEqual(env.read_bytes(), b'# deployment configuration\n')

    def test_single_entry_prepare_host_needs_no_env_no_dependencies_no_network(self):
        runtime, cwd = self.root / 'runtime', self.root / 'cwd'
        runtime.mkdir()
        cwd.mkdir()
        entry = runtime / 'emoskata.py'
        entry.write_text(build(write=False))
        environment = dict(os.environ, EMOS_ENV_FILE=str(runtime / '.env'))
        result = subprocess.run([sys.executable, '-I', '-S', '-B', str(entry), '--prepare-host'],
                                cwd=cwd, env=environment, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((runtime / 'requirements.txt').is_file())
        self.assertTrue((cwd / 'requirements.txt').is_file())
        self.assertFalse((runtime / '.env').exists())
        self.assertFalse((runtime / 'data').exists())
        self.assertFalse((runtime / '.emoskata.py.last-good').exists())

    def test_real_single_process_with_cdn_still_probes_and_reports_public_ip(self):
        hits = []
        class Probe(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                hits.append(dict(self.headers))
                raw = b'8.8.8.8'
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
        probe = ThreadingHTTPServer(('127.0.0.1', 0), Probe)
        thread = threading.Thread(target=probe.serve_forever, daemon=True)
        thread.start()
        try:
            self._run_cdn_process(probe.server_port, hits)
        finally:
            probe.shutdown()
            probe.server_close()
            thread.join(3)

    def _run_cdn_process(self, probe_port, hits):
        source = build(write=False)
        node = next(node for node in ast.parse(source).body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == '_SOURCES' for target in node.targets))
        modules = ast.literal_eval(node.value)
        modules['runtime_info'] += '\n_PUBLIC_IP_URLS = (%r,)\n' % ('http://127.0.0.1:%d/ip' % probe_port)
        modules['runtime_info'] += '\ndef container_ip():\n    return "172.19.0.203"\n'
        lines = source.splitlines(keepends=True)
        lines[node.lineno - 1:node.end_lineno] = ['_SOURCES = ' + repr(modules) + '\n']
        source = ''.join(lines)
        compile(source, 'emoskata.py', 'exec')
        entry = self.root / 'emoskata.py'
        entry.write_text(source)
        self._verify_live_cdn(entry, hits)

    def _verify_live_cdn(self, entry, hits):
        import socket
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        config = self.root / '.env'
        config.write_text('PANEL_PASSWORD="process_fixture_password"\nHTTP_HOST="127.0.0.1"\n'
                          'PORT="%d"\nPUBLIC_BASE="https://cdn.fixture.invalid"\n'
                          'TG_ALLOWED_USER_IDS=""\nSCHEDULER_INTERVAL="300"\n' % port)
        config.chmod(0o600)
        before = config.read_bytes()
        log_path = self.root / 'process.log'
        def request(path, body=None, cookie=''):
            conn = HTTPConnection('127.0.0.1', port, timeout=2)
            try:
                headers = {'Content-Type': 'application/json', 'Cookie': cookie}
                conn.request('GET' if body is None else 'POST', path,
                             json.dumps(body) if body is not None else None, headers)
                reply = conn.getresponse()
                raw = reply.read()
                return reply.status, dict(reply.getheaders()), json.loads(raw) if raw else {}
            finally:
                conn.close()
        with log_path.open('wb') as log:
            process = subprocess.Popen([sys.executable, '-I', '-S', '-B', '-u', str(entry)], cwd=self.root,
                env=dict(os.environ, EMOS_ENV_FILE=str(config)), stdout=log, stderr=subprocess.STDOUT)
        try:
            until = time.monotonic() + 15
            while time.monotonic() < until:
                self.assertIsNone(process.poll(), log_path.read_text())
                try:
                    if request('/api/health')[0] == 200 and hits:
                        break
                except OSError:
                    pass
                time.sleep(0.05)
            else:
                self.fail(log_path.read_text())
            self._check_cdn_reply(request, process, config, before, hits)
        finally:
            process.terminate()
            process.wait(timeout=15)

    def _check_cdn_reply(self, request, process, config, before, hits):
        status, headers, _ = request('/api/login', {'password': 'process_fixture_password'})
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie'].split(';')[0]
        until = time.monotonic() + 5
        while time.monotonic() < until:
            status, _, data = request('/api/runtime', cookie=cookie)
            if data['public_ip_status'] != 'pending':
                break
            time.sleep(0.05)
        self.assertEqual(status, 200)
        self.assertEqual(data['pid'], process.pid)
        self.assertEqual(data['server_public_ip'], '8.8.8.8')
        self.assertEqual(data['container_ip'], '172.19.0.203')
        self.assertEqual(data['management_url'], 'https://cdn.fixture.invalid/admin')
        self.assertEqual(config.read_bytes(), before)
        self.assertTrue((self.root / 'requirements.txt').is_file())
        self.assertEqual(len(hits), 1)
        self.assertNotIn('Authorization', hits[0])
        self.assertNotIn('Cookie', hits[0])
        request('/api/runtime', cookie=cookie)
        self.assertEqual(len(hits), 1)
        logs = (self.root / 'process.log').read_text()
        self.assertIn('服务器公网 IP：8.8.8.8', logs)
        self.assertIn('管理入口：https://cdn.fixture.invalid/admin', logs)
        self.assertNotIn('管理入口：http://172.19.0.203', logs)
        config.write_text(config.read_text().replace('PUBLIC_BASE="https://cdn.fixture.invalid"', 'PUBLIC_BASE=""'))
        status, _, direct = request('/api/runtime', cookie=cookie)
        self.assertEqual(status, 200)
        self.assertEqual(direct['server_public_ip'], '8.8.8.8')
        self.assertEqual(direct['management_url'], 'http://8.8.8.8:%d/admin' % direct['port'])
        self.assertEqual(len(hits), 1)

    def test_dashboard_contains_distinct_origin_ip_and_cdn_entry(self):
        tree = ast.parse((ROOT / 'app.py').read_text())
        page = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == 'PAGE' for target in node.targets))
        self.assertIn('服务器公网 IP：', page)
        self.assertIn('管理访问地址：', page)
        self.assertIn('内网 IP（仅诊断）', page)
