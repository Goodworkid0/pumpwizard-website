import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper.observed_market_log import ObservedMarketLog
from paper.raydium_cpmm_observed import collect
from paper.raydium_cpmm_observed_ledger import ObservedRaydiumLedger
from paper.raydium_cpmm_pilot import ObservedRaydiumPilot
from test_raydium_cpmm_observed import BASE, QUOTE, RaydiumObservedCaptureTests


class ObservedRaydiumPilotTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        fixture = RaydiumObservedCaptureTests('test_collects_hashed_free_rpc_snapshot_without_inventing_write_version')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        request, _ = fixture.request()
        self.archive, self.pool_id = fixture.archive, fixture.identity
        path = Path(self.tmp.name) / 'observed.sqlite'
        writable = ObservedMarketLog(path)
        for sequence, observed_at in enumerate((1000, 1060, 1120, 1180), 1):
            writable.append(collect(self.pool_id, self.archive, sequence, observed_at=observed_at, request=request))
        writable.close()
        self.log = ObservedMarketLog(path, view_only=True); self.addCleanup(self.log.close)
        config = {'id': 'pilot-test-v1', 'pool_id': self.pool_id, 'base_mint': BASE, 'quote_mint': QUOTE,
                  'initial_quote_amount': 1_000_000, 'order_quote_amount': 100_000, 'base_probe_amount': 100_000,
                  'confirmation_snapshots': 3, 'minimum_growth_bps': 25, 'max_hold_snapshots': 1}
        self.pilot = ObservedRaydiumPilot(config)
        source = {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only'}
        self.ledger = ObservedRaydiumLedger(Path(self.tmp.name) / 'ledger.sqlite', {
            'pool_id': self.pool_id, 'initial_quote_amount': 1_000_000, 'max_positions': 1,
            'max_snapshot_age_seconds': 120,
        }, source)
        self.addCleanup(self.ledger.close)

    def quote(self, pool_id, sequence, input_mint, amount_in, archive, log):
        snapshot = log.get_snapshot(pool_id, sequence)
        return {'schema': 'test', 'pool_id': pool_id, 'input_mint': input_mint, 'output_mint': QUOTE,
                'amount_in': amount_in, 'amount_out': {1: 100_000, 2: 100_500, 3: 101_000, 4: 101_500}[sequence],
                'snapshot': {'sequence': sequence, 'sha256': snapshot['sha256']}, 'sha256': 'quote-' + str(sequence)}

    @patch('paper.raydium_cpmm_pilot.quote')
    def test_opens_only_from_sequenced_snapshot_evidence(self, mocked_quote):
        mocked_quote.side_effect = self.quote
        opened = self.pilot.step(self.archive, self.log, self.ledger, at=1181)
        self.assertEqual(opened['action'], 'open')
        self.assertEqual(opened['position']['entry_sequence'], 4)
        # The latest three snapshots are 2..4, all of which are immutable records.
        self.assertEqual([item['sequence'] for item in opened['snapshots']], [2, 3, 4])
        self.assertEqual(self.ledger.verify()['events'], 1)

        # A fifth source record is needed before the fixed one-snapshot holding rule can close.
        self.assertEqual(opened['position']['status'], 'open')

    @patch('paper.raydium_cpmm_pilot.quote')
    def test_waits_when_observed_trend_is_below_threshold(self, mocked_quote):
        mocked_quote.side_effect = lambda pool_id, sequence, input_mint, amount_in, archive, log: {
            'output_mint': QUOTE, 'amount_in': amount_in, 'amount_out': 100_000,
            'snapshot': {'sequence': sequence, 'sha256': log.get_snapshot(pool_id, sequence)['sha256']},
            'sha256': 'flat-' + str(sequence),
        }
        result = self.pilot.step(self.archive, self.log, self.ledger, at=1181)
        self.assertEqual(result['action'], 'wait')
        self.assertEqual(result['reason'], 'Trend threshold not met')
        self.assertEqual(self.ledger.verify()['events'], 0)
