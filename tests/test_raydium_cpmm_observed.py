import base64
import tempfile
import unittest
from pathlib import Path

from paper.market_archive import MarketArchive, pool_id
from paper.raydium_cpmm import CPMM_PROGRAM_ID
from paper.raydium_cpmm_config import ACCOUNT_DISCRIMINATOR, ACCOUNT_LENGTH
from paper.raydium_cpmm_observed import RaydiumObservedError, collect
from paper.raydium_cpmm_vaults import POOL_ACCOUNT_DISCRIMINATOR, POOL_ACCOUNT_LENGTH


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
    for char in value: number = number * 58 + ALPHABET.index(char)
    return b'\0' * (len(value) - len(value.lstrip('1'))) + number.to_bytes(32, 'big')


def encoded(data): return [base64.b64encode(data).decode(), 'base64']


def account(owner, data): return {'executable': False, 'owner': owner, 'data': encoded(data)}


def pool_data():
    data = bytearray(POOL_ACCOUNT_LENGTH)
    data[:8], data[8:40] = POOL_ACCOUNT_DISCRIMINATOR, decode58(CONFIG)
    data[72:104], data[104:136] = decode58(BASE_VAULT), decode58(QUOTE_VAULT)
    data[168:200], data[200:232] = decode58(BASE), decode58(QUOTE)
    data[232:264], data[264:296] = decode58(TOKEN), decode58(TOKEN)
    return data


def token_data(mint, amount):
    data = bytearray(165); data[:32], data[64:72], data[108] = decode58(mint), amount.to_bytes(8, 'little'), 1
    return data


def config_data():
    data = bytearray(ACCOUNT_LENGTH); data[:8] = ACCOUNT_DISCRIMINATOR
    return data


class RaydiumObservedCaptureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'archive.sqlite'
        writable = MarketArchive(self.path)
        self.identity = pool_id(CPMM_PROGRAM_ID, POOL)
        writable.add_identities([{'mint': BASE, 'decimals': 5}, {'mint': QUOTE, 'decimals': 9}], [{
            'dex': 'raydium', 'program_id': CPMM_PROGRAM_ID, 'pool_address': POOL, 'pool_kind': 'constant_product',
            'base_mint': BASE, 'quote_mint': QUOTE, 'configuration': {'cpmm_config': CONFIG}}])
        writable.close()
        self.archive = MarketArchive(self.path, view_only=True); self.addCleanup(self.archive.close)

    def request(self):
        calls = []
        pool = account(CPMM_PROGRAM_ID, pool_data())
        values = [pool, account(TOKEN, token_data(BASE, 1_000_000)),
                  account(TOKEN, token_data(QUOTE, 5_000_000)), account(CPMM_PROGRAM_ID, config_data())]
        def fake(endpoint, method, params):
            calls.append((endpoint, method, params))
            response = {'jsonrpc': '2.0', 'result': {'context': {'slot': 100 if method == 'getAccountInfo' else 101},
                        'value': pool if method == 'getAccountInfo' else values}}
            return {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, response
        return fake, calls

    def test_collects_hashed_free_rpc_snapshot_without_inventing_write_version(self):
        request, calls = self.request()
        result = collect(self.identity, self.archive, 7, 'finalized', 1000, request=request)
        self.assertEqual([call[1] for call in calls], ['getAccountInfo', 'getMultipleAccounts'])
        self.assertEqual(calls[1][2][1]['minContextSlot'], 100)
        self.assertEqual(result['context_slot'], 101)
        self.assertEqual(result['source']['write_version'], 'unavailable_not_invented')
        self.assertEqual(result['base_vault']['address'], BASE_VAULT)
        self.assertEqual(result['config']['address'], CONFIG)
        self.assertIn('not reconciled', result['limitations'])
        self.assertEqual(self.archive.verify()['tables']['pool_states']['rows'], 0)

    def test_requires_read_only_archive_and_rejects_invalid_sequence(self):
        request, _ = self.request()
        with self.assertRaisesRegex(RaydiumObservedError, 'sequence'):
            collect(self.identity, self.archive, 0, observed_at=1000, request=request)
        writable = MarketArchive(Path(self.tmp.name) / 'other.sqlite')
        self.addCleanup(writable.close)
        with self.assertRaisesRegex(RaydiumObservedError, 'read-only'):
            collect(self.identity, writable, 1, observed_at=1000, request=request)
