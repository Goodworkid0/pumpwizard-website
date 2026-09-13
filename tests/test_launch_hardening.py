import io
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from paper.public_read import PublicReadService
from paper.server import serve
from scripts.backup_lab import backup, restore, verify
from scripts.check_lab import backup_age, check


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)/'lab'; (self.root/'data').mkdir(parents=True)
        self.source = sqlite3.connect(self.root/'data/long-hold-test.sqlite')
        self.addCleanup(self.source.close)
        self.source.execute('PRAGMA journal_mode=WAL')
        self.source.execute('CREATE TABLE trades(id INTEGER PRIMARY KEY, data TEXT)')
        self.source.execute('INSERT INTO trades(data) VALUES (?)', ('{"pnl": 2.5}',))
        self.source.commit()
        (self.root/'brain.config.json').write_text('{"example":true}')
        (self.root/'data/service.env').write_text('SYNTHETIC_SECRET=must-not-copy')
        self.destination = Path(self.tmp.name)/'backup'

    def test_online_backup_restore_counts_wal_and_no_secrets(self):
        manifest = backup(self.root, self.destination)
        self.assertEqual(manifest['consistency'], 'online-per-database-not-cross-ledger-atomic')
        self.assertEqual(len(manifest['files']), 2)
        self.assertEqual(manifest['files'][0]['table_counts'], {'trades': 1})
        self.assertFalse((self.destination/'data/service.env').exists())
        self.source.execute('INSERT INTO trades(data) VALUES (\'later\')'); self.source.commit()
        recovery = Path(self.tmp.name)/'rehearsal'
        self.assertEqual(restore(self.destination, recovery), manifest)
        self.assertEqual(verify(recovery), manifest)
        self.assertEqual(self.source.execute('SELECT COUNT(*) FROM trades').fetchone()[0], 2)
        with self.assertRaises(ValueError): restore(self.destination, self.root)
        with self.assertRaises(ValueError): backup(self.root, self.destination)
        with self.assertRaises(ValueError): backup(self.root, self.root/'backup')
        self.assertIsNotNone(backup_age(Path(self.tmp.name), time.time()))

    def test_tampered_incomplete_and_unsafe_backups_fail_closed(self):
        backup(self.root, self.destination)
        target = self.destination/'config/brain.config.json'
        original = target.read_bytes(); target.write_bytes(b'changed')
        with self.assertRaises(ValueError): verify(self.destination)
        target.write_bytes(original)
        file = self.destination/'manifest.json'; manifest = json.loads(file.read_text())
        manifest['files'][0]['path'] = '../data/service.env'; file.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): verify(self.destination)
        manifest.pop('completed_at'); file.write_text(json.dumps(manifest))
        with self.assertRaises(ValueError): verify(self.destination)

    def test_insufficient_space_does_not_create_directory(self):
        with patch('scripts.backup_lab.shutil.disk_usage', return_value=SimpleNamespace(free=0)):
            with self.assertRaises(ValueError): backup(self.root, self.destination)
        self.assertFalse(self.destination.exists())


class HealthTests(unittest.TestCase):
    def test_readiness_tracks_cycles_not_trades(self):
        now = time.time()
        alive = SimpleNamespace(is_alive=lambda: True)
        brain = SimpleNamespace(threads=[alive, alive], research_timing={'completed_at': now, 'input_observed_at': now})
        service = PublicReadService(brain, autostart=False)
        service.thread = alive
        service.generation = {'started': now, 'published': now}
        self.assertEqual(service.health()['status'], 'ok')
        brain.research_timing['completed_at'] = now-181
        self.assertFalse(service.health()['checks']['research_cycle'])
        brain.research_timing['completed_at'] = now
        brain.research_timing['input_observed_at'] = now-301
        self.assertFalse(service.health()['checks']['market_input'])
        brain.research_timing['input_observed_at'] = now
        service.last_error = True
        self.assertFalse(service.health()['checks']['snapshot'])
        service.generation = None
        self.assertEqual(service.health()['status'], 'degraded')

    def test_monitor_checks_disk_and_backup_missing_even_if_service_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            body = json.dumps({'status':'ok', 'checks':{'snapshot':True}}).encode()
            with patch('scripts.check_lab.urllib.request.urlopen', return_value=io.BytesIO(body)):
                result = check('http://localhost', Path(tmp), Path(tmp))
            self.assertTrue(result['checks']['readiness'])
            self.assertFalse(result['checks']['backup_recent'])
            self.assertEqual(result['status'], 'attention')
            with patch('scripts.check_lab.urllib.request.urlopen', side_effect=OSError('secret-url')):
                result = check('http://localhost', Path(tmp))
            self.assertNotIn('secret-url', json.dumps(result))
            self.assertFalse(result['checks']['readiness'])
            error = urllib.error.HTTPError('http://localhost',503,'degraded',{},io.BytesIO(
                json.dumps({'status':'degraded','checks':{'market_input':False}}).encode()))
            with patch('scripts.check_lab.urllib.request.urlopen',side_effect=error):
                result = check('http://localhost',Path(tmp))
            self.assertFalse(result['details']['service']['checks']['market_input'])


class PublicBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.server = serve(None, 0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = 'http://127.0.0.1:'+str(self.server.server_port)

    def request(self, path, method='GET', headers=None):
        req = urllib.request.Request(self.url+path, method=method, headers=headers or {})
        try: response = urllib.request.urlopen(req, timeout=3)
        except urllib.error.HTTPError as exc: response = exc
        with response: return response.status, response.headers, response.read()

    def test_public_pages_and_external_navigation_work(self):
        for path in ('/', '/engine', '/engine/test', '/research', '/affiliate', '/contact', '/legal', '/contact.js'):
            status, headers, _ = self.request(path)
            self.assertEqual(status, 200, path)
            self.assertEqual(headers['X-Frame-Options'], 'DENY')
        status, _, _ = self.request('/engine', headers={'Sec-Fetch-Site':'cross-site', 'Sec-Fetch-Mode':'navigate'})
        self.assertEqual(status, 200)
        self.assertEqual(self.request('/engine', headers={'Host':'attacker.invalid'})[0], 403)
        status, _, body = self.request('/engine', method='HEAD')
        self.assertEqual(status, 200); self.assertEqual(body, b'')

    def test_operator_routes_secrets_and_writes_are_denied(self):
        for path in ('/api/public', '/api/state', '/api/dashboard', '/api/account.csv', '/api/trades.csv',
                     '/api/acp/community', '/api/acp/strategies', '/acp', '/acp.js', '/data/service.env',
                     '/api/public/../state', '/api/publicity', '/%61pi/state'):
            self.assertEqual(self.request(path)[0], 404, path)
        for method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            self.assertEqual(self.request('/api/acp/community', method)[0], 405)
        self.assertEqual(self.request('/engine?q='+'x'*2050)[0], 414)
        status, _, data = self.request('/api/public/health')
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(data)['status'], 'unavailable')


if __name__ == '__main__': unittest.main()
