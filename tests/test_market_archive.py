import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import ArchiveError, MarketArchive, event_id, pool_id, token_id


PROGRAM = '675kPX9MHTjS2zt1qfr1NYHuzefQsY8m1LvL1u3s5K7'
POOL = '7YttLkHqSW1veWDdtYKDdRSZG6p25LoVCmmLsoq2inD4'
BASE = 'DezXAZ8z7PnrnRJjz3wXBoRgixCa6z5L7fegQY5W6B5z'
QUOTE = 'So11111111111111111111111111111111111111112'
SIGNATURE = '5NfDvExmaSCeiMvf8LZeyxkZE4WDEYNBBkWW4AsMrmE5Gpx1q7dzixEQvaNWqZ8F47PLeizpkiNpt1mpmbJs9PZq'


def pool():
    return {'dex': 'raydium', 'program_id': PROGRAM, 'pool_address': POOL,
            'pool_kind': 'constant_product', 'base_mint': BASE, 'quote_mint': QUOTE,
            'configuration': {'fee_bps': 25}}


class MarketArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'market.sqlite'
        self.archive = MarketArchive(self.path)
        self.addCleanup(self.archive.close)
        self.pool_id = pool_id(PROGRAM, POOL)

    def add_pool(self):
        self.assertTrue(self.archive.add_token({'mint': BASE, 'decimals': 5, 'symbol': 'BONK'}))
        self.assertTrue(self.archive.add_token({'mint': QUOTE, 'decimals': 9, 'symbol': 'SOL'}))
        self.assertTrue(self.archive.add_pool(pool()))

    def test_identity_helpers_are_stable(self):
        self.assertEqual(token_id(BASE), 'solana:' + BASE)
        self.assertEqual(self.pool_id, 'solana:' + PROGRAM + ':' + POOL)
        self.assertEqual(event_id(SIGNATURE, 3), 'solana:' + SIGNATURE + ':3')

    def test_append_only_event_state_and_proof(self):
        self.add_pool()
        event = {'pool_id': self.pool_id, 'signature': SIGNATURE, 'instruction_index': 3,
                 'slot': 301000000, 'event_kind': 'swap', 'commitment': 'confirmed',
                 'occurred_at': 1760000000, 'received_at': 1760000000.4,
                 'parser': 'raydium-cpmm/v1', 'payload': {'base_in': '10', 'quote_out': '20'}}
        state = {'pool_id': self.pool_id, 'slot': 301000000, 'write_version': 2,
                 'commitment': 'confirmed', 'observed_at': 1760000000.4,
                 'parser': 'raydium-cpmm/v1', 'state': {'base_reserve': '100', 'quote_reserve': '200'}}
        self.assertTrue(self.archive.add_event(event))
        self.assertFalse(self.archive.add_event(event))
        self.assertTrue(self.archive.add_pool_state(state))
        first = self.archive.verify()
        self.assertEqual(first['tables']['market_events']['rows'], 1)
        self.assertEqual(first, self.archive.manifest())
        self.archive.close()
        self.archive = MarketArchive(self.path)
        self.addCleanup(self.archive.close)
        self.assertEqual(first, self.archive.verify())

    def test_rejects_conflicting_append_and_unknown_pool(self):
        event = {'pool_id': self.pool_id, 'signature': SIGNATURE, 'instruction_index': 0,
                 'slot': 1, 'event_kind': 'swap', 'commitment': 'confirmed', 'occurred_at': 1,
                 'received_at': 1, 'parser': 'raydium-cpmm/v1', 'payload': {}}
        with self.assertRaisesRegex(ArchiveError, 'Unknown pool'):
            self.archive.add_event(event)
        self.add_pool()
        self.archive.add_event(event)
        changed = dict(event, payload={'different': True})
        with self.assertRaisesRegex(ArchiveError, 'Conflicting immutable'):
            self.archive.add_event(changed)

    def test_pool_requires_known_token_identities(self):
        with self.assertRaisesRegex(ArchiveError, 'Unknown token'):
            self.archive.add_pool(pool())

    def test_verify_detects_row_tampering(self):
        self.add_pool()
        with self.archive.db:
            self.archive.db.execute("UPDATE pools SET data=? WHERE id=?", (json.dumps({'bad': True}), self.pool_id))
        with self.assertRaisesRegex(ArchiveError, 'Invalid stored pools'):
            self.archive.verify()

    def test_never_opens_missing_read_only_archive(self):
        with self.assertRaises(sqlite3.OperationalError):
            MarketArchive(Path(self.tmp.name) / 'missing.sqlite', view_only=True)

    def test_read_only_verifier_returns_archive_manifest(self):
        self.add_pool()
        expected = self.archive.verify()
        result = subprocess.run(
            [sys.executable, 'scripts/verify_market_archive.py', '--archive', str(self.path)],
            cwd=Path(__file__).resolve().parents[1], text=True, capture_output=True, check=True)
        self.assertEqual(json.loads(result.stdout), expected)
