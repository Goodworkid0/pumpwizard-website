import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.engine import canonical
from paper.market_archive import MarketArchive, event_id, pool_id
from paper.raydium_cpmm import CPMM_PROGRAM_ID
from paper.raydium_cpmm_config import ACCOUNT_DISCRIMINATOR, ACCOUNT_LENGTH, SCHEMA as CONFIG_SCHEMA
from paper.raydium_cpmm_events import PARSER as EVENT_PARSER
from paper.raydium_cpmm_quote import (SCHEMA, SPL_TOKEN_PROGRAM, RaydiumShadowQuoteError,
                                     quote)
from paper.raydium_cpmm_reconcile import SCHEMA as RECONCILIATION_SCHEMA
from paper.raydium_cpmm_vaults import PARSER as VAULT_PARSER


POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
CONFIG = 'D4FpefD4nWNC9QmL5FG4QqZQ9VQ1KqbDPHefBNeKDniG'
SIGNATURE = '4' * 64


def config_data():
    data = bytearray(ACCOUNT_LENGTH)
    data[:8] = ACCOUNT_DISCRIMINATOR
    data[12:20] = (2500).to_bytes(8, 'little')
    data[20:28] = (120000).to_bytes(8, 'little')
    return base64.b64encode(data).decode()


class RaydiumShadowQuoteTests(unittest.TestCase):
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
                                'slot': 100, 'event_kind': 'swap', 'commitment': 'confirmed',
                                'occurred_at': 1000, 'received_at': 1001, 'parser': EVENT_PARSER,
                                'payload': {'instruction': 'swap_base_input', 'direction': 'base_to_quote',
                                            'intent': {'amount_in': 100, 'minimum_amount_out': 1}}})
        self.archive.add_pool_state({'pool_id': self.pool_id, 'slot': 100, 'write_version': 3,
                                     'commitment': 'confirmed', 'observed_at': 1001, 'parser': VAULT_PARSER,
                                     'state': {'base_vault': {'owner_program': SPL_TOKEN_PROGRAM, 'amount': 1_000_000},
                                               'quote_vault': {'owner_program': SPL_TOKEN_PROGRAM, 'amount': 5_000_000},
                                               'pool_account': {'config_address': CONFIG, 'swap_enabled': True,
                                                                'creator_fee_mode': 0, 'creator_fee_enabled': False,
                                                                'base_trade_reserve': 1_000_000,
                                                                'quote_trade_reserve': 5_000_000}}})

    def reconciliation_report(self):
        event = self.archive.get_event(self.event_id)
        state = self.archive.get_pool_state(self.pool_id, 100, 3)
        report = {'schema': RECONCILIATION_SCHEMA, 'mode': 'shadow_event_state_reconciliation',
                  'comparisons': [{'event_id': self.event_id, 'pool_id': self.pool_id,
                                   'after': {'slot': 100, 'write_version': 3},
                                   'event_sha256': event['content_sha256'],
                                   'after_state_sha256': state['content_sha256']}]}
        report['sha256'] = hashlib.sha256(canonical(report).encode()).hexdigest()
        return report

    def request(self):
        return {'schema': SCHEMA, 'pool_id': self.pool_id, 'state': {'slot': 100, 'write_version': 3},
                'input_mint': BASE, 'amount_in': 10_000,
                'reconciliation_report': self.reconciliation_report(),
                'config_recording': {'schema': CONFIG_SCHEMA, 'captures': [{
                    'pool_id': self.pool_id, 'address': CONFIG, 'owner': CPMM_PROGRAM_ID,
                    'slot': 100, 'write_version': 2, 'commitment': 'confirmed',
                    'observed_at': 1001, 'data_base64': config_data(),
                }]}}

    def test_quotes_exact_base_input_math_from_proven_state(self):
        before = self.archive.verify()
        result = quote(self.request(), self.archive)
        expected = 9_975 * 5_000_000 // (1_000_000 + 9_975)
        self.assertEqual(result['amount_out'], expected)
        self.assertEqual(result['fees'], {'trade': 25, 'protocol': 3, 'fund': 0,
                                          'creator': 0, 'creator_on_input': True})
        self.assertIn('not executable', result['limitations'])
        self.assertEqual(before, self.archive.verify())

    def test_rejects_token_2022_until_transfer_fee_evidence_exists(self):
        state = self.archive.get_pool_state(self.pool_id, 100, 3)
        state['state']['quote_vault']['owner_program'] = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'
        self.archive.db.execute('DELETE FROM pool_states')
        self.archive.add_pool_state(state)
        with self.assertRaisesRegex(RaydiumShadowQuoteError, 'Token-2022'):
            quote(self.request(), self.archive)

    def test_cli_uses_read_only_archive_and_writes_new_report(self):
        source = self.root / 'quote.json'
        source.write_text(json.dumps(self.request()))
        report = self.root / 'report.json'
        self.archive.close()
        result = subprocess.run([sys.executable, 'scripts/quote_raydium_cpmm.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        saved = json.loads(report.read_text())
        self.assertEqual(json.loads(result.stdout)['sha256'], saved['sha256'])
        second = subprocess.run([sys.executable, 'scripts/quote_raydium_cpmm.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
