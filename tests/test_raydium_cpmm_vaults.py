import base64
import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import MarketArchive, pool_id
from paper.raydium_cpmm import CPMM_PROGRAM_ID
from paper.raydium_cpmm_vaults import (PARSER, SCHEMA, RaydiumVaultReplayError,
                                      ingest_vault_recording)


POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
CONFIG = 'D4FpefD4nWNC9QmL5FG4QqZQ9VQ1KqbDPHefBNeKDniG'
BASE_VAULT = '4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R'
QUOTE_VAULT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
ALPHABET = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


def decode58(value):
    number = 0
    for char in value:
        number = number * 58 + ALPHABET.index(char)
    return b'\0' * (len(value) - len(value.lstrip('1'))) + number.to_bytes(32, 'big')


def token_account(mint, amount):
    data = bytearray(165)
    data[:32] = decode58(mint)
    data[64:72] = amount.to_bytes(8, 'little')
    data[108] = 1
    return base64.b64encode(data).decode()


class RaydiumVaultReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'market.sqlite'
        self.archive = MarketArchive(self.path)
        self.addCleanup(self.archive.close)
        self.pool_id = pool_id(CPMM_PROGRAM_ID, POOL)
        self.archive.add_identities(
            [{'mint': BASE, 'decimals': 5}, {'mint': QUOTE, 'decimals': 9}],
            [{'dex': 'raydium', 'program_id': CPMM_PROGRAM_ID, 'pool_address': POOL,
              'pool_kind': 'constant_product', 'base_mint': BASE, 'quote_mint': QUOTE,
              'configuration': {'cpmm_config': CONFIG}}])

    def recording(self):
        return {'schema': SCHEMA, 'captures': [{
            'pool_id': self.pool_id, 'slot': 312345678, 'write_version': 9,
            'commitment': 'confirmed', 'observed_at': 1700000000,
            'base_vault': {'address': BASE_VAULT, 'owner': TOKEN, 'data_base64': token_account(BASE, 1234567)},
            'quote_vault': {'address': QUOTE_VAULT, 'owner': TOKEN, 'data_base64': token_account(QUOTE, 8901234)},
        }]}

    def test_replays_verified_binary_vault_balances_as_state_only(self):
        report = ingest_vault_recording(self.recording(), self.archive)
        self.assertEqual(report['mode'], 'shadow_vault_state_replay')
        self.assertEqual(report['appended_pool_states'], 1)
        self.assertEqual(report['state_ids'], [{'pool_id': self.pool_id, 'slot': 312345678, 'write_version': 9}])
        row = self.archive.db.execute('SELECT data FROM pool_states').fetchone()
        state = json.loads(row['data'])['state']
        self.assertEqual(state['base_vault']['amount'], 1234567)
        self.assertEqual(state['quote_vault']['amount'], 8901234)
        self.assertEqual(json.loads(row['data'])['parser'], PARSER)
        self.assertEqual(self.archive.verify()['tables']['market_events']['rows'], 0)
        repeated = ingest_vault_recording(self.recording(), self.archive)
        self.assertEqual(repeated['appended_pool_states'], 0)

    def test_wrong_vault_mint_rejects_recording_without_mutation(self):
        recording = self.recording()
        recording['captures'][0]['base_vault']['data_base64'] = token_account(QUOTE, 1)
        with self.assertRaisesRegex(RaydiumVaultReplayError, 'Invalid base vault account'):
            ingest_vault_recording(recording, self.archive)
        self.assertEqual(self.archive.verify()['tables']['pool_states']['rows'], 0)

    def test_offline_cli_writes_only_a_new_report(self):
        source = self.root / 'recording.json'
        source.write_text(json.dumps(self.recording()))
        report = self.root / 'report.json'
        self.archive.close()
        result = subprocess.run([sys.executable, 'scripts/replay_raydium_cpmm_vaults.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        saved = json.loads(report.read_text())
        self.assertEqual(json.loads(result.stdout)['sha256'], saved['sha256'])
        second = subprocess.run([sys.executable, 'scripts/replay_raydium_cpmm_vaults.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
