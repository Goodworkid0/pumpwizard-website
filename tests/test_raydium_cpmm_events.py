import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import MarketArchive, pool_id
from paper.raydium_cpmm import CPMM_PROGRAM_ID
from paper.raydium_cpmm_events import (SCHEMA, SWAP_BASE_INPUT, SWAP_BASE_OUTPUT,
                                      RaydiumEventReplayError, ingest_event_recording)


POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
CONFIG = 'D4FpefD4nWNC9QmL5FG4QqZQ9VQ1KqbDPHefBNeKDniG'
BASE_VAULT = '4k3Dyjzvzp8eMZWUXbBCjEvwSkkk59S5iCNLY3QrkX6R'
QUOTE_VAULT = 'EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v'
TOKEN = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
TOKEN_2022 = 'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'


def instruction(discriminator=SWAP_BASE_INPUT, first=1234, second=567):
    data = base64.b64encode(discriminator + first.to_bytes(8, 'little') + second.to_bytes(8, 'little')).decode()
    return {'program_id': CPMM_PROGRAM_ID,
            'accounts': [BASE, QUOTE, CONFIG, POOL, BASE, QUOTE, BASE_VAULT, QUOTE_VAULT,
                         TOKEN, TOKEN_2022, BASE, QUOTE, BASE],
            'data_base64': data}


class RaydiumEventReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root / 'market.sqlite'
        self.archive = MarketArchive(self.path)
        self.addCleanup(self.archive.close)
        self.identity = pool_id(CPMM_PROGRAM_ID, POOL)
        self.archive.add_identities(
            [{'mint': BASE, 'decimals': 5}, {'mint': QUOTE, 'decimals': 9}],
            [{'dex': 'raydium', 'program_id': CPMM_PROGRAM_ID, 'pool_address': POOL,
              'pool_kind': 'constant_product', 'base_mint': BASE, 'quote_mint': QUOTE,
              'configuration': {'cpmm_config': CONFIG}}])

    def recording(self, item=None):
        return {'schema': SCHEMA, 'transactions': [{
            'signature': '2' * 64, 'slot': 312345679, 'commitment': 'confirmed',
            'occurred_at': 1700000010, 'received_at': 1700000011, 'success': True,
            'instructions': [item or instruction()],
        }]}

    def test_decodes_swap_intent_without_claiming_a_fill(self):
        report = ingest_event_recording(self.recording(), self.archive)
        self.assertEqual(report['mode'], 'shadow_event_replay')
        self.assertEqual(report['appended_events'], 1)
        row = self.archive.db.execute('SELECT data FROM market_events').fetchone()
        event = json.loads(row['data'])
        self.assertEqual(event['pool_id'], self.identity)
        self.assertEqual(event['payload']['instruction'], 'swap_base_input')
        self.assertEqual(event['payload']['direction'], 'base_to_quote')
        self.assertEqual(event['payload']['intent'], {'amount_in': 1234, 'minimum_amount_out': 567})
        self.assertNotIn('amount_out', event['payload'])
        self.assertEqual(ingest_event_recording(self.recording(), self.archive)['appended_events'], 0)

    def test_decodes_base_output_amounts_as_limits_not_actual_transfers(self):
        report = ingest_event_recording(self.recording(instruction(SWAP_BASE_OUTPUT, 1000, 500)), self.archive)
        self.assertEqual(report['appended_events'], 1)
        event = json.loads(self.archive.db.execute('SELECT data FROM market_events').fetchone()['data'])
        self.assertEqual(event['payload']['instruction'], 'swap_base_output')
        self.assertEqual(event['payload']['intent'], {'max_amount_in': 1000, 'amount_out': 500})

    def test_wrong_pool_config_rejects_entire_recording_without_events(self):
        item = instruction()
        item['accounts'][2] = BASE
        with self.assertRaisesRegex(RaydiumEventReplayError, 'config does not match'):
            ingest_event_recording(self.recording(item), self.archive)
        self.assertEqual(self.archive.verify()['tables']['market_events']['rows'], 0)

    def test_offline_cli_writes_only_a_new_report(self):
        source = self.root / 'events.json'
        source.write_text(json.dumps(self.recording()))
        report = self.root / 'report.json'
        self.archive.close()
        result = subprocess.run([sys.executable, 'scripts/replay_raydium_cpmm_events.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        saved = json.loads(report.read_text())
        self.assertEqual(json.loads(result.stdout)['sha256'], saved['sha256'])
        second = subprocess.run([sys.executable, 'scripts/replay_raydium_cpmm_events.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
