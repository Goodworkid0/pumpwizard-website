import tempfile
import unittest
from pathlib import Path

from paper.observed_market_log import ObservedMarketLog
from paper.raydium_cpmm_observed import RaydiumObservedError
from paper.raydium_cpmm_observed_monitor import RaydiumObservedMonitorError, capture_once
from test_raydium_cpmm_observed import RaydiumObservedCaptureTests


class RaydiumObservedMonitorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.fixture = RaydiumObservedCaptureTests('test_collects_hashed_free_rpc_snapshot_without_inventing_write_version')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        self.log = ObservedMarketLog(Path(self.tmp.name) / 'observed.sqlite'); self.addCleanup(self.log.close)

    def test_records_consecutive_snapshots_and_reports_observed_health(self):
        request, calls = self.fixture.request()
        first = capture_once(self.fixture.identity, self.fixture.archive, self.log, cadence_seconds=60,
                             observed_at=1000, checked_at=1001, request=request)
        second = capture_once(self.fixture.identity, self.fixture.archive, self.log, cadence_seconds=60,
                              observed_at=1061, checked_at=1062, request=request)
        self.assertEqual(first['health']['sequence_status'], 'initial')
        self.assertEqual(second['health']['sequence_status'], 'continuous')
        self.assertEqual(second['health']['cadence_status'], 'within_interval')
        self.assertEqual(second['health']['observed_gap_seconds'], 61.0)
        self.assertEqual(second['health']['write_version'], 'unavailable_not_invented')
        self.assertEqual([call[1] for call in calls], ['getAccountInfo', 'getMultipleAccounts'] * 2)
        self.assertEqual(self.log.verify()['tables']['observed_snapshots']['rows'], 2)

    def test_marks_cadence_gap_without_smoothing_it_over(self):
        request, _ = self.fixture.request()
        capture_once(self.fixture.identity, self.fixture.archive, self.log, cadence_seconds=60,
                     observed_at=1000, checked_at=1000, request=request)
        result = capture_once(self.fixture.identity, self.fixture.archive, self.log, cadence_seconds=60,
                              observed_at=1100, checked_at=1101, request=request)
        self.assertEqual(result['health']['cadence_status'], 'gap_detected')
        self.assertEqual(result['health']['observed_gap_seconds'], 100.0)

    def test_refuses_writable_identity_archive_or_read_only_log(self):
        request, _ = self.fixture.request()
        from paper.market_archive import MarketArchive
        writable_archive = MarketArchive(Path(self.tmp.name) / 'writable.sqlite'); self.addCleanup(writable_archive.close)
        with self.assertRaisesRegex(RaydiumObservedMonitorError, 'read-only'):
            capture_once(self.fixture.identity, writable_archive, self.log, request=request)
        self.log.close()
        readonly_log = ObservedMarketLog(self.log.path, view_only=True); self.addCleanup(readonly_log.close)
        with self.assertRaisesRegex(RaydiumObservedMonitorError, 'writable'):
            capture_once(self.fixture.identity, self.fixture.archive, readonly_log, request=request)

    def test_rpc_failure_leaves_no_partial_observation(self):
        def unavailable(*_):
            raise RaydiumObservedError('injected unavailable RPC')
        with self.assertRaisesRegex(RaydiumObservedMonitorError, 'unavailable'):
            capture_once(self.fixture.identity, self.fixture.archive, self.log, request=unavailable)
        self.assertEqual(self.log.verify()['tables']['observed_snapshots']['rows'], 0)
