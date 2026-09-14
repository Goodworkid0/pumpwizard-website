import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import MarketArchive, event_id, pool_id
from paper.raydium_cpmm import CPMM_PROGRAM_ID, ingest_catalog
from paper.raydium_cpmm_config import ACCOUNT_DISCRIMINATOR, ACCOUNT_LENGTH, SCHEMA as CONFIG_SCHEMA
from paper.raydium_cpmm_events import SCHEMA as EVENT_SCHEMA, SWAP_BASE_INPUT, ingest_event_recording
from paper.raydium_cpmm_evidence import SCHEMA, RaydiumEvidenceError, seal
from paper.raydium_cpmm_quote import SCHEMA as QUOTE_SCHEMA
from paper.raydium_cpmm_reconcile import SCHEMA as RECONCILIATION_SCHEMA, reconcile
from paper.raydium_cpmm_validation import SCHEMA as VALIDATION_SCHEMA
from paper.raydium_cpmm_vaults import (POOL_ACCOUNT_DISCRIMINATOR, POOL_ACCOUNT_LENGTH,
                                       SCHEMA as VAULT_SCHEMA, ingest_vault_recording)


POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
CONFIG = 'D4FpefD4nWNC9QmL5FG4QqZQ9VQ1KqbDPHefBNeKDniG'
BASE_VAULT = '4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R'
QUOTE_VAULT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
SIGNATURE = '6' * 64
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def decode58(value):
    number = 0
    for char in value:
        number = number * 58 + ALPHABET.index(char)
    return b'\0' * (len(value) - len(value.lstrip('1'))) + number.to_bytes(32, 'big')


def token_account(mint, amount):
    data = bytearray(165)
    data[:32], data[64:72], data[108] = decode58(mint), amount.to_bytes(8, 'little'), 1
    return base64.b64encode(data).decode()


def pool_account():
    data = bytearray(POOL_ACCOUNT_LENGTH)
    data[:8], data[8:40] = POOL_ACCOUNT_DISCRIMINATOR, decode58(CONFIG)
    data[72:104], data[104:136] = decode58(BASE_VAULT), decode58(QUOTE_VAULT)
    data[168:200], data[200:232] = decode58(BASE), decode58(QUOTE)
    data[232:264], data[264:296] = decode58(TOKEN), decode58(TOKEN)
    return base64.b64encode(data).decode()


def config_account():
    data = bytearray(ACCOUNT_LENGTH)
    data[:8], data[12:20], data[20:28] = (ACCOUNT_DISCRIMINATOR, (2500).to_bytes(8, 'little'),
                                           (120000).to_bytes(8, 'little'))
    return base64.b64encode(data).decode()


class RaydiumEvidenceCorpusTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.identity = pool_id(CPMM_PROGRAM_ID, POOL)
        self.event = event_id(SIGNATURE, 0)
        self.expected = 9_975 * 5_000_000 // (1_000_000 + 9_975)

    def catalog(self):
        return {'data': [{'type': 'Standard', 'id': POOL, 'programId': CPMM_PROGRAM_ID,
                          'mintA': {'address': BASE, 'decimals': 5},
                          'mintB': {'address': QUOTE, 'decimals': 9}, 'config': {'id': CONFIG}}]}

    def events(self):
        data = base64.b64encode(SWAP_BASE_INPUT + (10_000).to_bytes(8, 'little') + (1).to_bytes(8, 'little')).decode()
        return {'schema': EVENT_SCHEMA, 'transactions': [{'signature': SIGNATURE, 'slot': 100,
                'commitment': 'finalized', 'occurred_at': 1000, 'received_at': 1001, 'success': True,
                'instructions': [{'program_id': CPMM_PROGRAM_ID,
                    'accounts': [BASE, QUOTE, CONFIG, POOL, BASE, QUOTE, BASE_VAULT, QUOTE_VAULT,
                                 TOKEN, TOKEN, BASE, QUOTE, BASE], 'data_base64': data}]}]}

    def vaults(self, slot, version, base, quote):
        return {'schema': VAULT_SCHEMA, 'captures': [{'pool_id': self.identity, 'slot': slot,
                'write_version': version, 'commitment': 'finalized', 'observed_at': 1001,
                'base_vault': {'address': BASE_VAULT, 'owner': TOKEN, 'data_base64': token_account(BASE, base)},
                'quote_vault': {'address': QUOTE_VAULT, 'owner': TOKEN, 'data_base64': token_account(QUOTE, quote)},
                'pool_account': {'address': POOL, 'owner': CPMM_PROGRAM_ID, 'data_base64': pool_account()}}]}

    def evidence(self):
        path = self.root / 'source.sqlite'
        archive = MarketArchive(path)
        try:
            ingest_catalog(self.catalog(), archive, 900, 'raydium-api-v3-fixed-catalog')
            events = self.events()
            before = self.vaults(99, 1, 1_000_000, 5_000_000)
            after = self.vaults(100, 2, 1_010_000, 5_000_000 - self.expected)
            ingest_event_recording(events, archive)
            ingest_vault_recording(before, archive)
            ingest_vault_recording(after, archive)
            reconciliation = reconcile({'schema': RECONCILIATION_SCHEMA, 'comparisons': [{
                'event_id': self.event, 'before': {'slot': 99, 'write_version': 1},
                'after': {'slot': 100, 'write_version': 2}}]}, archive)
            request = {'schema': QUOTE_SCHEMA, 'pool_id': self.identity,
                       'state': {'slot': 99, 'write_version': 1}, 'input_mint': BASE, 'amount_in': 10_000,
                       'reconciliation_report': reconciliation,
                       'config_recording': {'schema': CONFIG_SCHEMA, 'captures': [{
                           'pool_id': self.identity, 'address': CONFIG, 'owner': CPMM_PROGRAM_ID,
                           'slot': 99, 'write_version': 0, 'commitment': 'finalized', 'observed_at': 1001,
                           'data_base64': config_account()}]}}
            return {'schema': SCHEMA, 'capture': {'network': 'solana-mainnet-beta',
                    'source_label': 'recorded-observer-1', 'captured_at': 1002},
                    'catalog': {'source': 'raydium-api-v3-fixed-catalog', 'captured_at': 900,
                                'page': self.catalog()}, 'event_recordings': [events],
                    'vault_recordings': [before, after],
                    'validation': {'schema': VALIDATION_SCHEMA,
                                   'cases': [{'event_id': self.event, 'quote_request': request}]}}
        finally:
            archive.close()

    def test_seals_finalized_reproducible_evidence_without_enabling_simulation(self):
        report = seal(self.evidence())
        self.assertTrue(report['validation']['passed'])
        self.assertTrue(report['review']['ready_for_human_review'])
        self.assertFalse(report['review']['simulation_eligible'])
        self.assertEqual(report['coverage']['directions'], ['base_to_quote'])
        self.assertEqual(report['coverage']['creator_fee_modes'], ['0:disabled'])
        self.assertEqual(report['archive_manifest']['tables']['pool_states']['rows'], 2)

    def test_rejects_non_mainnet_capture_label(self):
        corpus = self.evidence()
        corpus['capture']['network'] = 'devnet'
        with self.assertRaisesRegex(RaydiumEvidenceError, 'mainnet-beta'):
            seal(corpus)

    def test_cli_writes_a_new_report(self):
        source, report = self.root / 'corpus.json', self.root / 'report.json'
        source.write_text(json.dumps(self.evidence()))
        result = subprocess.run([sys.executable, 'scripts/seal_raydium_cpmm_evidence.py', '--input', str(source),
                                 '--report', str(report)], cwd=Path(__file__).resolve().parents[1],
                                text=True, capture_output=True, check=True)
        self.assertTrue(json.loads(result.stdout)['passed'])
        self.assertFalse(json.loads(report.read_text())['review']['simulation_eligible'])
        repeated = subprocess.run([sys.executable, 'scripts/seal_raydium_cpmm_evidence.py', '--input', str(source),
                                   '--report', str(report)], cwd=Path(__file__).resolve().parents[1],
                                  text=True, capture_output=True)
        self.assertNotEqual(repeated.returncode, 0)
