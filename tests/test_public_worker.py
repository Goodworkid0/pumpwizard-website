import json
import sqlite3
import threading
import time
from unittest.mock import patch
from test_public_read import PublicReadTests, ROOT
from paper.public_worker import PublicBrain, publish_registry
from paper.public_read import PublicReadService


class WorkerTests(PublicReadTests):
    def prepare_registry(self):
        (self.brain.root/'brain.config.json').write_text((ROOT/'brain.config.json').read_text())
        self.brain.threads = [threading.current_thread()]
        with self.runner.db:
            self.runner.db.execute("INSERT INTO meta VALUES ('state',?)", (json.dumps(self.runner.state),))
        publish_registry(self.brain)

    def test_no_writer_or_provider_created(self):
        self.prepare_registry()
        with patch('paper.brain_service.BrainService.__init__', side_effect=AssertionError('writer')):
            brain = PublicBrain(self.brain.root)
        service = PublicReadService(brain, autostart=False)
        self.addCleanup(service.close)
        self.assertTrue(service.refresh_once())
        self.assertEqual(json.loads(service.response('/api/public/catalog',{}))['total'],1)
        with self.assertRaises(sqlite3.OperationalError): brain.runner.db.execute('DELETE FROM trades')
        self.assertTrue(brain.threads[0].is_alive())

    def test_stale_registry_retains_snapshot_but_degrades(self):
        self.prepare_registry()
        service = PublicReadService(PublicBrain(self.brain.root), autostart=False)
        self.addCleanup(service.close)
        self.assertTrue(service.refresh_once())
        with patch('paper.public_worker.time.time',return_value=time.time()+40):
            with self.assertLogs('paper.public_read',level='ERROR'): self.assertFalse(service.refresh_once())
            self.assertFalse(service.brain.threads[0].is_alive())
        self.assertTrue(service.snapshot()[1]['stale'])

    def test_registry_cannot_escape_data_directory(self):
        self.prepare_registry()
        path = self.brain.root/'data/public-registry.json'
        payload=json.loads(path.read_text()); payload['entries'][0]['ledger']='../service.env'
        path.write_text(json.dumps(payload))
        with self.assertRaisesRegex(ValueError,'ledger'): PublicBrain(self.brain.root)
