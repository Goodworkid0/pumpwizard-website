import copy
import tempfile
import unittest
from pathlib import Path

from paper.observed_market_log import ObservedLogError, ObservedMarketLog
from test_raydium_cpmm_observed import RaydiumObservedCaptureTests


class ObservedMarketLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'observed.sqlite'
        self.fixture = RaydiumObservedCaptureTests('test_collects_hashed_free_rpc_snapshot_without_inventing_write_version')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        request, _ = self.fixture.request()
        self.snapshot = self.fixture_collect(request, 1)

    def fixture_collect(self, request, sequence):
        from paper.raydium_cpmm_observed import collect
        return collect(self.fixture.identity, self.fixture.archive, sequence, observed_at=1000 + sequence, request=request)

    def test_appends_idempotently_and_verifies_manifest(self):
        log = ObservedMarketLog(self.path); self.addCleanup(log.close)
        self.assertEqual(log.next_sequence(self.snapshot['pool_id']), 1)
        self.assertTrue(log.append(self.snapshot))
        self.assertFalse(log.append(self.snapshot))
        self.assertEqual(log.next_sequence(self.snapshot['pool_id']), 2)
        self.assertEqual(log.get_snapshot(self.snapshot['pool_id'], 1)['sha256'], self.snapshot['sha256'])
        self.assertEqual(len(log.recent(self.snapshot['pool_id'])), 1)
        self.assertEqual(log.verify()['tables']['observed_snapshots']['rows'], 1)

    def test_refuses_sequence_gap_and_conflicting_rewrite(self):
        log = ObservedMarketLog(self.path); self.addCleanup(log.close)
        skipped = self.fixture_collect(self.fixture.request()[0], 2)
        with self.assertRaisesRegex(ObservedLogError, 'consecutive'):
            log.append(skipped)
        self.assertTrue(log.append(self.snapshot))
        conflicting = copy.deepcopy(self.snapshot)
        conflicting['observed_at'] = 3333.0
        conflicting.pop('sha256')
        from paper.observed_market_log import _hash
        conflicting['sha256'] = _hash(conflicting)
        with self.assertRaisesRegex(ObservedLogError, 'Conflicting'):
            log.append(conflicting)

    def test_refuses_tampered_snapshot_and_read_only_writes(self):
        bad = copy.deepcopy(self.snapshot)
        bad['source']['write_version'] = 9
        writable = ObservedMarketLog(self.path)
        self.addCleanup(writable.close)
        with self.assertRaisesRegex(ObservedLogError, 'write versions'):
            writable.append(bad)
        log = ObservedMarketLog(self.path); log.append(self.snapshot); log.close()
        read_only = ObservedMarketLog(self.path, view_only=True); self.addCleanup(read_only.close)
        with self.assertRaisesRegex(ObservedLogError, 'read-only'):
            read_only.append(self.snapshot)
