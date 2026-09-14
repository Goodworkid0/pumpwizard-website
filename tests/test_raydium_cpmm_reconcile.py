import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import MarketArchive, event_id, pool_id
from paper.raydium_cpmm import CPMM_PROGRAM_ID
from paper.raydium_cpmm_events import PARSER as EVENT_PARSER
from paper.raydium_cpmm_reconcile import (SCHEMA, RaydiumReconciliationError,
                                         reconcile)
from paper.raydium_cpmm_vaults import PARSER as VAULT_PARSER


POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
CONFIG = 'D4FpefD4nWNC9QmL5FG4QqZQ9VQ1KqbDPHefBNeKDniG'
SIGNATURE = '3' * 64


class RaydiumReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'market.sqlite'
        self.archive = MarketArchive(self.path)
        self.addCleanup(self.archive.close)
        self.pool_id = pool_id(CPMM_PROGRAM_ID, POOL)
        self.event_id = event_id(SIGNATURE, 0)
        self.archive.add_identities(
            [{'mint': BASE, 'decimals': 5}, {'mint': QUOTE, 'decimals': 9}],
            [{'dex': 'raydium', 'program_id': CPMM_PROGRAM_ID, 'pool_address': POOL,
              'pool_kind': 'constant_product', 'base_mint': BASE, 'quote_mint': QUOTE,
              'configuration': {'cpmm_config': CONFIG}}])
        self.archive.add_event({'pool_id': self.pool_id, 'signature': SIGNATURE, 'instruction_index': 0,
                                'slot': 11, 'event_kind': 'swap', 'commitment': 'confirmed',
                                'occurred_at': 100, 'received_at': 101, 'parser': EVENT_PARSER,
                                'payload': {'instruction': 'swap_base_input', 'direction': 'base_to_quote',
                                            'intent': {'amount_in': 120, 'minimum_amount_out': 70}}})
        self.add_state(10, 0, 1000, 2000)
        self.add_state(12, 0, 1100, 1920)

    def add_state(self, slot, version, base, quote):
        self.archive.add_pool_state({'pool_id': self.pool_id, 'slot': slot, 'write_version': version,
                                     'commitment': 'confirmed', 'observed_at': 100 + slot,
                                     'parser': VAULT_PARSER,
                                     'state': {'base_vault': {'amount': base}, 'quote_vault': {'amount': quote}}})

    def recording(self):
        return {'schema': SCHEMA, 'comparisons': [{'event_id': self.event_id,
                                                   'before': {'slot': 10, 'write_version': 0},
                                                   'after': {'slot': 12, 'write_version': 0}}]}

    def test_read_only_comparison_proves_direction_and_intent_bounds(self):
        before = self.archive.verify()
        report = reconcile(self.recording(), self.archive)
        comparison = report['comparisons'][0]
        self.assertEqual(report['mode'], 'shadow_event_state_reconciliation')
        self.assertEqual((comparison['base_delta'], comparison['quote_delta']), (100, -80))
        self.assertEqual((comparison['input_vault_delta'], comparison['output_vault_delta']), (100, 80))
        self.assertEqual(before, self.archive.verify())

    def test_insufficient_output_fails_without_archive_mutation(self):
        self.add_state(13, 0, 1100, 1940)
        recording = self.recording()
        recording['comparisons'][0]['after'] = {'slot': 13, 'write_version': 0}
        before = self.archive.verify()
        with self.assertRaisesRegex(RaydiumReconciliationError, 'do not satisfy'):
            reconcile(recording, self.archive)
        self.assertEqual(before, self.archive.verify())

    def test_cli_opens_the_archive_read_only_and_writes_new_report(self):
        source = self.root / 'reconcile.json'
        source.write_text(json.dumps(self.recording()))
        report = self.root / 'report.json'
        self.archive.close()
        result = subprocess.run([sys.executable, 'scripts/reconcile_raydium_cpmm.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        saved = json.loads(report.read_text())
        self.assertEqual(json.loads(result.stdout)['sha256'], saved['sha256'])
        second = subprocess.run([sys.executable, 'scripts/reconcile_raydium_cpmm.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
