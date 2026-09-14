import tempfile
import unittest
from pathlib import Path

from paper.observed_market_log import ObservedMarketLog
from paper.raydium_cpmm_observed import collect
from paper.raydium_cpmm_observed_quote import RaydiumObservedQuoteError, quote
from test_raydium_cpmm_observed import BASE, RaydiumObservedCaptureTests


class RaydiumObservedQuoteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.fixture = RaydiumObservedCaptureTests('test_collects_hashed_free_rpc_snapshot_without_inventing_write_version')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        request, _ = self.fixture.request()
        writable = ObservedMarketLog(Path(self.tmp.name) / 'observed.sqlite')
        writable.append(collect(self.fixture.identity, self.fixture.archive, 1, observed_at=1000, request=request))
        writable.close()
        self.log = ObservedMarketLog(writable.path, view_only=True); self.addCleanup(self.log.close)

    def test_estimates_classic_spl_swap_from_immutable_observed_snapshot(self):
        before = self.fixture.archive.verify()
        result = quote(self.fixture.identity, 1, BASE, 100_000, self.fixture.archive, self.log)
        self.assertEqual(result['mode'], 'observed_snapshot_base_input_quote')
        self.assertEqual(result['assurance'], 'observed_simulation_only')
        self.assertEqual(result['amount_out'], 454_545)
        self.assertEqual(result['snapshot']['write_version'], 'unavailable_not_invented')
        self.assertIn('not executable', result['limitations'])
        self.assertEqual(self.fixture.archive.verify(), before)
        self.assertEqual(self.log.verify()['tables']['observed_snapshots']['rows'], 1)

    def test_refuses_unknown_input_or_writable_dependencies(self):
        with self.assertRaisesRegex(RaydiumObservedQuoteError, 'Input mint'):
            quote(self.fixture.identity, 1, '11111111111111111111111111111111', 1, self.fixture.archive, self.log)
        writable = ObservedMarketLog(Path(self.tmp.name) / 'writable.sqlite'); self.addCleanup(writable.close)
        with self.assertRaisesRegex(RaydiumObservedQuoteError, 'read-only observed'):
            quote(self.fixture.identity, 1, BASE, 1, self.fixture.archive, writable)
