import base64
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import MarketArchive, pool_id
from paper.raydium_cpmm import CPMM_PROGRAM_ID
from paper.raydium_cpmm_config import (ACCOUNT_DISCRIMINATOR, ACCOUNT_LENGTH, SCHEMA,
                                      RaydiumConfigReplayError, ingest_config_recording)


POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
CONFIG = 'D4FpefD4nWNC9QmL5FG4QqZQ9VQ1KqbDPHefBNeKDniG'


def config_account(trade=2500, protocol=120000, fund=0, creator=0):
    data = bytearray(ACCOUNT_LENGTH)
    data[:8] = ACCOUNT_DISCRIMINATOR
    data[9] = 0
    data[10:12] = (7).to_bytes(2, 'little')
    data[12:20] = trade.to_bytes(8, 'little')
    data[20:28] = protocol.to_bytes(8, 'little')
    data[28:36] = fund.to_bytes(8, 'little')
    data[108:116] = creator.to_bytes(8, 'little')
    return base64.b64encode(data).decode()


class RaydiumConfigReplayTests(unittest.TestCase):
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

    def recording(self, data=None):
        return {'schema': SCHEMA, 'captures': [{
            'pool_id': self.pool_id, 'address': CONFIG, 'owner': CPMM_PROGRAM_ID,
            'slot': 312345680, 'write_version': 1, 'commitment': 'confirmed',
            'observed_at': 1700000020, 'data_base64': data or config_account(),
        }]}

    def test_parses_linked_amm_config_fee_snapshot_read_only(self):
        before = self.archive.verify()
        report = ingest_config_recording(self.recording(), self.archive)
        config = report['configs'][0]
        self.assertEqual(config['index'], 7)
        self.assertEqual(config['fees'], {'trade_fee_rate': 2500, 'protocol_fee_rate': 120000,
                                          'fund_fee_rate': 0, 'creator_fee_rate': 0})
        self.assertEqual(before, self.archive.verify())

    def test_rejects_invalid_rate_without_changing_archive(self):
        before = self.archive.verify()
        with self.assertRaisesRegex(RaydiumConfigReplayError, 'Invalid CPMM fee rates'):
            ingest_config_recording(self.recording(config_account(trade=1_000_000)), self.archive)
        self.assertEqual(before, self.archive.verify())

    def test_cli_uses_read_only_archive_and_new_report(self):
        source = self.root / 'config.json'
        source.write_text(json.dumps(self.recording()))
        report = self.root / 'report.json'
        self.archive.close()
        result = subprocess.run([sys.executable, 'scripts/replay_raydium_cpmm_config.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        saved = json.loads(report.read_text())
        self.assertEqual(json.loads(result.stdout)['sha256'], saved['sha256'])
        second = subprocess.run([sys.executable, 'scripts/replay_raydium_cpmm_config.py', '--input', str(source),
                                 '--archive', str(self.path), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
