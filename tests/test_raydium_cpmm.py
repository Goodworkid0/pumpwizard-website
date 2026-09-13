import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import MarketArchive
from paper.raydium_cpmm import RaydiumCatalogError, catalog_url, ingest_catalog


PROGRAM = 'CPMMoo8L3F4NbTegBCKVNwqH6Jm6zLxZCNZfY8Y7dP'
POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
CONFIG = 'D4FpefD4nWNC9QmL5FG4QqZQ9VQ1KqbDPHefBNeKDniG'


def cpmm(pool=POOL):
    return {'type': 'Standard', 'id': pool, 'programId': PROGRAM,
            'mintA': {'address': BASE, 'decimals': 5, 'symbol': 'BONK', 'name': 'Bonk'},
            'mintB': {'address': QUOTE, 'decimals': 9, 'symbol': 'SOL', 'name': 'Wrapped SOL'},
            'config': {'id': CONFIG, 'tradeFeeRate': 2500, 'protocolFeeRate': 120000},
            'price': 0.0000001, 'tvl': 100000, 'mintAmountA': 1000000, 'mintAmountB': 100}


class RaydiumCpmmTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.archive = MarketArchive(self.root / 'market.sqlite')
        self.addCleanup(self.archive.close)

    def test_catalog_url_is_fixed_and_bounded(self):
        self.assertEqual(catalog_url(2), 'https://api-v3.raydium.io/pools/info/list-v2?size=2&hasReward=false&poolType=Standard&sortField=liquidity&sortType=desc')
        with self.assertRaises(RaydiumCatalogError):
            catalog_url(101)

    def test_cpmm_catalog_records_only_identities_and_proves_raw_page(self):
        legacy = copy.deepcopy(cpmm())
        legacy['config'] = None
        legacy['marketId'] = '9xQeWvG816bUx9EPfBybboqB2LQf3g92TLJGwvCz6Tw'
        page = {'count': 3, 'hasNextPage': False, 'data': [cpmm(), legacy, {'type': 'Concentrated'}]}
        report = ingest_catalog(page, self.archive, 1000, 'fixture://raydium')
        self.assertEqual(report['mode'], 'shadow_catalog_only')
        self.assertEqual(report['appended'], {'tokens': 2, 'pools': 1})
        self.assertEqual(len(report['accepted_pool_ids']), 1)
        self.assertEqual(len(report['rejected']), 2)
        repeated = ingest_catalog(page, self.archive, 1000, 'fixture://raydium')
        self.assertEqual(repeated['accepted_pool_ids'], report['accepted_pool_ids'])
        self.assertEqual(repeated['rejected'], report['rejected'])
        self.assertEqual(repeated['raw_sha256'], report['raw_sha256'])
        self.assertEqual(repeated['appended'], {'tokens': 0, 'pools': 0})
        self.assertEqual(self.archive.verify()['tables']['pool_states']['rows'], 0)
        self.assertEqual(self.archive.verify()['tables']['market_events']['rows'], 0)

    def test_malformed_dynamic_metric_rejects_only_that_pool(self):
        bad = cpmm()
        bad['tvl'] = 'not-a-number'
        report = ingest_catalog({'data': [bad]}, self.archive, 1000)
        self.assertFalse(report['accepted_pool_ids'])
        self.assertEqual(report['appended'], {'tokens': 0, 'pools': 0})
        self.assertEqual(report['rejected'][0]['reason'], 'Invalid tvl')

    def test_malformed_identity_rejects_only_that_pool(self):
        bad = cpmm('not-a-solana-address')
        report = ingest_catalog({'data': [cpmm(), bad]}, self.archive, 1000)
        self.assertEqual(len(report['accepted_pool_ids']), 1)
        self.assertEqual(report['appended'], {'tokens': 2, 'pools': 1})
        self.assertEqual(report['rejected'][0]['reason'], 'Invalid pool_address')

    def test_offline_cli_writes_new_raw_report_and_archive(self):
        source = self.root / 'catalog.json'
        source.write_text(json.dumps({'data': [cpmm()]}))
        report = self.root / 'report.json'
        archive = self.root / 'cli.sqlite'
        result = subprocess.run([sys.executable, 'scripts/shadow_raydium_cpmm.py', '--input', str(source),
                                 '--archive', str(archive), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        summary = json.loads(result.stdout)
        saved = json.loads(report.read_text())
        self.assertEqual(summary['sha256'], saved['sha256'])
        self.assertEqual(saved['raw'], {'data': [cpmm()]})
        second = subprocess.run([sys.executable, 'scripts/shadow_raydium_cpmm.py', '--input', str(source),
                                 '--archive', str(archive), '--report', str(report)],
                                cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True)
        self.assertNotEqual(second.returncode, 0)
