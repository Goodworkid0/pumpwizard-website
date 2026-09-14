import json
import shutil
import tempfile
import unittest
from pathlib import Path

from paper.observed_market_log import ObservedMarketLog
from paper.market_archive import token_id
from paper.raydium_cpmm_observed import collect
from paper.raydium_cpmm_observed_ledger import ObservedRaydiumLedger
from paper.raydium_cpmm_public_read import RaydiumCPMMPublicRead
from test_raydium_cpmm_observed import BASE, QUOTE, RaydiumObservedCaptureTests


class RaydiumCPMMPublicReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'data').mkdir(); (self.root / 'deploy').mkdir()
        fixture = RaydiumObservedCaptureTests('test_collects_hashed_free_rpc_snapshot_without_inventing_write_version')
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        request, _ = fixture.request()
        archive_path = self.root / 'data/raydium-identities.sqlite'
        shutil.copyfile(fixture.archive.path, archive_path)
        quote_token = json.loads(fixture.archive.db.execute(
            'SELECT data FROM tokens WHERE id=?', (token_id(QUOTE),)).fetchone()['data'])
        quote_scale = 10 ** quote_token['decimals']
        log_path = self.root / 'data/raydium-observed.sqlite'
        log = ObservedMarketLog(log_path)
        for sequence, at in enumerate((1000, 1060, 1120), 1):
            log.append(collect(fixture.identity, fixture.archive, sequence, observed_at=at, request=request))
        log.close()
        self.config = {'schema': 'pumpwizard.raydium-cpmm-pilot/v1', 'id': 'public-pilot-v1',
                       'pool_id': fixture.identity, 'base_mint': BASE, 'quote_mint': QUOTE,
                       'initial_quote_amount': 500 * quote_scale, 'order_quote_amount': 50 * quote_scale,
                       'base_probe_amount': 1_000_000_000, 'confirmation_snapshots': 3,
                       'minimum_growth_bps': 50, 'max_hold_snapshots': 10}
        (self.root / 'deploy/raydium-near-usdc-pilot.json').write_text(json.dumps(self.config))
        ledger = ObservedRaydiumLedger(self.root / 'data/raydium-near-usdc-pilot.sqlite', {
            'pool_id': fixture.identity, 'initial_quote_amount': 500 * quote_scale,
            'max_positions': 1, 'max_snapshot_age_seconds': 120,
        }, {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only'})
        ledger.close()

    def test_projects_only_the_isolated_observed_ledger(self):
        reader = RaydiumCPMMPublicRead(self.root)
        document = reader.document()
        self.assertEqual(document['service']['market'], 'raydium-cpmm')
        self.assertIn('no USD FX valuation', document['service']['denomination'])
        self.assertEqual(document['portfolio']['initial_usd'], 500)
        self.assertEqual(document['portfolio']['equity_usd'], 500)
        self.assertEqual(document['strategies'][0]['id'], 'public-pilot-v1')
        self.assertEqual(document['observed_evidence']['snapshot_count'], 3)
        self.assertEqual(document['observed_evidence']['ledger_proof']['events'], 0)
        self.assertEqual(reader.export('/api/public/history.csv').decode().splitlines(),
                         ['event_id,at,kind,data_json,content_sha256'])

    def test_catalog_and_strategy_detail_are_market_scoped(self):
        reader = RaydiumCPMMPublicRead(self.root)
        catalog = json.loads(reader.response('/api/public/catalog', {'q': ['near']}))
        self.assertEqual(catalog['total'], 1)
        self.assertEqual(catalog['strategies'][0]['id'], 'public-pilot-v1')
        detail = json.loads(reader.response('/api/public/strategies/public-pilot-v1', {}))
        self.assertEqual([item['id'] for item in detail['strategies']], ['public-pilot-v1'])
        self.assertEqual(detail['strategies'][0]['family'], 'observed_trend')
