import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import MarketArchive, event_id, pool_id
from paper.raydium_cpmm import CPMM_PROGRAM_ID
from paper.raydium_cpmm_config import ACCOUNT_DISCRIMINATOR, ACCOUNT_LENGTH, SCHEMA as CONFIG_SCHEMA
from paper.raydium_cpmm_events import PARSER as EVENT_PARSER
from paper.raydium_cpmm_quote import SCHEMA as QUOTE_SCHEMA, SPL_TOKEN_PROGRAM
from paper.raydium_cpmm_reconcile import SCHEMA as RECONCILIATION_SCHEMA, reconcile
from paper.raydium_cpmm_validation import SCHEMA, RaydiumShadowValidationError, validate
from paper.raydium_cpmm_vaults import PARSER as VAULT_PARSER


POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
CONFIG = 'D4FpefD4nWNC9QmL5FG4QqZQ9VQ1KqbDPHefBNeKDniG'
SIGNATURE = '5' * 64


def config_data():
    data = bytearray(ACCOUNT_LENGTH)
    data[:8] = ACCOUNT_DISCRIMINATOR
    data[12:20] = (2500).to_bytes(8, 'little')
    data[20:28] = (120000).to_bytes(8, 'little')
    return base64.b64encode(data).decode()


class RaydiumShadowValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'market.sqlite'
        self.archive = MarketArchive(self.path)
        self.addCleanup(self.archive.close)
        self.identity = pool_id(CPMM_PROGRAM_ID, POOL)
        self.event = event_id(SIGNATURE, 0)
        self.archive.add_identities(
            [{'mint': BASE, 'decimals': 5}, {'mint': QUOTE, 'decimals': 9}],
            [{'dex': 'raydium', 'program_id': CPMM_PROGRAM_ID, 'pool_address': POOL,
              'pool_kind': 'constant_product', 'base_mint': BASE, 'quote_mint': QUOTE,
              'configuration': {'cpmm_config': CONFIG}}])
        self.archive.add_event({'pool_id': self.identity, 'signature': SIGNATURE, 'instruction_index': 0,
                                'slot': 100, 'event_kind': 'swap', 'commitment': 'confirmed',
                                'occurred_at': 1000, 'received_at': 1001, 'parser': EVENT_PARSER,
                                'payload': {'instruction': 'swap_base_input', 'direction': 'base_to_quote',
                                            'input_mint': BASE, 'output_mint': QUOTE,
                                            'intent': {'amount_in': 10_000, 'minimum_amount_out': 1}}})
        self.expected = 9_975 * 5_000_000 // (1_000_000 + 9_975)
        self._states(self.expected)

    def _state(self, slot, version, base, quote):
        return {'pool_id': self.identity, 'slot': slot, 'write_version': version,
                'commitment': 'confirmed', 'observed_at': 1001, 'parser': VAULT_PARSER,
                'state': {'base_vault': {'owner_program': SPL_TOKEN_PROGRAM, 'amount': base},
                          'quote_vault': {'owner_program': SPL_TOKEN_PROGRAM, 'amount': quote},
                          'pool_account': {'config_address': CONFIG, 'swap_enabled': True,
                                           'creator_fee_mode': 0, 'creator_fee_enabled': False,
                                           'base_trade_reserve': base, 'quote_trade_reserve': quote}}}

    def _states(self, actual_out):
        self.archive.add_pool_state(self._state(99, 1, 1_000_000, 5_000_000))
        self.archive.add_pool_state(self._state(100, 2, 1_010_000, 5_000_000 - actual_out))

    def recording(self):
        reconciliation = reconcile({'schema': RECONCILIATION_SCHEMA, 'comparisons': [{
            'event_id': self.event, 'before': {'slot': 99, 'write_version': 1},
            'after': {'slot': 100, 'write_version': 2},
        }]}, self.archive)
        request = {'schema': QUOTE_SCHEMA, 'pool_id': self.identity,
                   'state': {'slot': 99, 'write_version': 1}, 'input_mint': BASE, 'amount_in': 10_000,
                   'reconciliation_report': reconciliation,
                   'config_recording': {'schema': CONFIG_SCHEMA, 'captures': [{
                       'pool_id': self.identity, 'address': CONFIG, 'owner': CPMM_PROGRAM_ID,
                       'slot': 99, 'write_version': 2, 'commitment': 'confirmed',
                       'observed_at': 1001, 'data_base64': config_data(),
                   }]}}
        return {'schema': SCHEMA, 'cases': [{'event_id': self.event, 'quote_request': request}]}

    def test_validates_exact_quote_against_replayed_evidence_without_mutation(self):
        before = self.archive.verify()
        report = validate(self.recording(), self.archive)
        self.assertEqual(report['summary'], {'total': 1, 'matched': 1, 'mismatched': 0, 'passed': True})
        self.assertTrue(report['cases'][0]['matched'])
        self.assertEqual(report['cases'][0]['observed_amount_out'], self.expected)
        self.assertEqual(before, self.archive.verify())

    def test_reports_a_real_mismatch_but_never_passes_it(self):
        self.archive.db.execute('DELETE FROM pool_states')
        self._states(self.expected - 1)
        report = validate(self.recording(), self.archive)
        self.assertEqual(report['summary'], {'total': 1, 'matched': 0, 'mismatched': 1, 'passed': False})
        self.assertEqual(report['cases'][0]['output_difference'], 1)

    def test_rejects_a_non_reproducible_reconciliation_proof(self):
        recording = self.recording()
        recording['cases'][0]['quote_request']['reconciliation_report'].pop('raw')
        with self.assertRaisesRegex(RaydiumShadowValidationError, 'reproducible'):
            validate(recording, self.archive)

    def test_cli_uses_read_only_archive_and_writes_new_report(self):
        source = self.root / 'corpus.json'
        source.write_text(json.dumps(self.recording()))
        report = self.root / 'report.json'
        self.archive.close()
        result = subprocess.run([sys.executable, 'scripts/validate_raydium_cpmm_quotes.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        saved = json.loads(report.read_text())
        self.assertTrue(json.loads(result.stdout)['passed'])
        self.assertEqual(saved['summary']['matched'], 1)
        second = subprocess.run([sys.executable, 'scripts/validate_raydium_cpmm_quotes.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
