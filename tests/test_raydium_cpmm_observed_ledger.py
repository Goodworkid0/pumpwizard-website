import tempfile
import unittest
from pathlib import Path

from paper.observed_market_log import ObservedMarketLog
from paper.raydium_cpmm_observed import collect
from paper.raydium_cpmm_observed_ledger import ObservedRaydiumLedger, RaydiumObservedLedgerError
from test_raydium_cpmm_observed import RaydiumObservedCaptureTests


class RaydiumObservedLedgerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.fixture = RaydiumObservedCaptureTests('test_collects_hashed_free_rpc_snapshot_without_inventing_write_version')
        self.fixture.setUp(); self.addCleanup(self.fixture.doCleanups)
        request, _ = self.fixture.request()
        path = Path(self.tmp.name) / 'observed.sqlite'
        writable = ObservedMarketLog(path)
        writable.append(collect(self.fixture.identity, self.fixture.archive, 1, observed_at=1000, request=request))
        writable.append(collect(self.fixture.identity, self.fixture.archive, 2, observed_at=1060, request=request))
        writable.close()
        self.log = ObservedMarketLog(path, view_only=True); self.addCleanup(self.log.close)
        config = {'pool_id': self.fixture.identity, 'initial_quote_amount': 1_000_000,
                  'max_positions': 1, 'max_snapshot_age_seconds': 120}
        self.ledger = ObservedRaydiumLedger(Path(self.tmp.name) / 'ledger.sqlite', config,
                                            {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only'})
        self.addCleanup(self.ledger.close)

    def test_records_isolated_quote_denomination_entry_exit_and_replay(self):
        entry = self.ledger.open_position('first', 1, 100_000, self.fixture.archive, self.log, at=1001)
        self.assertEqual(entry['base_amount'], 19_607)
        marked = self.ledger.summary(self.fixture.archive, self.log, mark_sequence=2, at=1061)
        self.assertEqual(marked['denomination']['usd_valuation'], 'unavailable_without_separate_fx_evidence')
        self.assertEqual(marked['open_position_count'], 1)
        closed = self.ledger.close_position('first', 2, self.fixture.archive, self.log, at=1061)
        self.assertEqual(closed['exit_quote_amount'], 96_149)
        self.assertEqual(closed['pnl_quote_amount'], -3_851)
        summary = self.ledger.summary(self.fixture.archive, self.log, mark_sequence=2, at=1061)
        self.assertEqual(summary['cash_quote_amount'], 996_149)
        self.assertEqual(summary['realized_pnl_quote_amount'], -3_851)
        self.assertEqual(self.ledger.verify()['events'], 2)

    def test_rejects_stale_snapshots_and_unsafe_or_invalid_exits_without_mutation(self):
        with self.assertRaisesRegex(RaydiumObservedLedgerError, 'future'):
            self.ledger.open_position('future', 1, 100_000, self.fixture.archive, self.log, at=999)
        with self.assertRaisesRegex(RaydiumObservedLedgerError, 'stale'):
            self.ledger.open_position('old', 1, 100_000, self.fixture.archive, self.log, at=1121)
        self.assertEqual(self.ledger.verify()['events'], 0)
        self.ledger.open_position('first', 1, 100_000, self.fixture.archive, self.log, at=1001)
        with self.assertRaisesRegex(RaydiumObservedLedgerError, 'follow'):
            self.ledger.close_position('first', 1, self.fixture.archive, self.log, at=1001)
        self.assertEqual(self.ledger.verify()['events'], 1)
