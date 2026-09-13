"""Opt-in Raydium CPMM catalog discovery for shadow research only.

The public Raydium catalog is a convenient source of pool identities.  It does
not carry a slot, account write version, confirmed reserve balances, or decoded
swap instructions, so this module deliberately does not create market events,
pool states, quotes, signals, or simulated fills.  A later stream/RPC collector
must provide those separately.
"""
import hashlib
import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request

from .engine import canonical
from .market_archive import (ArchiveError, MarketArchive, normalize_pool,
                             normalize_token, pool_id, solana_address)


BASE = 'https://api-v3.raydium.io'
PATH = '/pools/info/list-v2'
SCHEMA = 'pumpwizard.raydium-cpmm-shadow/v1'
MAX_BODY_BYTES = 4_000_000


class RaydiumCatalogError(ValueError):
    pass


def _finite(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise RaydiumCatalogError('Invalid ' + name)
    return value


def _token(item, label):
    if not isinstance(item, dict):
        raise RaydiumCatalogError('Missing ' + label)
    result = {'mint': item.get('address'), 'decimals': item.get('decimals')}
    for key in ('symbol', 'name'):
        if item.get(key) is not None:
            result[key] = item[key]
    return result


def _pool(item):
    if not isinstance(item, dict):
        raise RaydiumCatalogError('Pool is not an object')
    # Raydium labels both legacy AMMs and CPMM pools "Standard".  A CPMM
    # catalog record carries its config object; legacy AMMs instead carry a
    # marketId/configId pair.  Do not guess from fee/TVL fields.
    if item.get('type') != 'Standard':
        raise RaydiumCatalogError('Not a standard pool')
    config = item.get('config')
    if not isinstance(config, dict) or 'id' not in config:
        raise RaydiumCatalogError('Not a CPMM pool')
    if item.get('marketId') is not None:
        raise RaydiumCatalogError('Legacy standard AMM is unsupported')
    a = normalize_token(_token(item.get('mintA'), 'mintA'))
    b = normalize_token(_token(item.get('mintB'), 'mintB'))
    # These dynamic values remain in the raw shadow report only. They receive no
    # execution semantics and cannot mutate the immutable pool identity.
    for name in ('price', 'tvl', 'mintAmountA', 'mintAmountB'):
        if name in item and item[name] is not None:
            _finite(item[name], name)
    configuration = {'catalog_source': 'raydium-api-v3/v1',
                     'cpmm_config': solana_address(config['id'], 'cpmm_config')}
    pool = normalize_pool({'dex': 'raydium', 'program_id': item.get('programId'),
                           'pool_address': item.get('id'), 'pool_kind': 'constant_product',
                           'base_mint': a['mint'], 'quote_mint': b['mint'],
                           'configuration': configuration})
    return a, b, pool


def catalog_url(size=100):
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= 100:
        raise RaydiumCatalogError('size must be an integer from 1 to 100')
    query = urllib.parse.urlencode({'size': size, 'hasReward': 'false', 'poolType': 'Standard',
                                    'sortField': 'liquidity', 'sortType': 'desc'})
    return BASE + PATH + '?' + query


def fetch_catalog(size=100):
    """Fetch one fixed, public catalog page; no keys or transaction endpoints."""
    url = catalog_url(size)
    request = urllib.request.Request(url, headers={'Accept': 'application/json',
                                                    'User-Agent': 'PumpWizardShadow/1.0',
                                                    'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            parts = urllib.parse.urlsplit(response.geturl())
            if parts.scheme != 'https' or parts.hostname != 'api-v3.raydium.io' or parts.path != PATH:
                raise RaydiumCatalogError('Unexpected catalog redirect')
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise RaydiumCatalogError('Catalog response is too large')
            return json.loads(body), url
    except urllib.error.HTTPError as exc:
        raise RaydiumCatalogError('Raydium catalog HTTP ' + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise RaydiumCatalogError('Raydium catalog is unavailable or invalid') from exc


def ingest_catalog(page, archive, received_at=None, source_url=None):
    """Record CPMM identities and return an auditable shadow-run report.

    ``page`` is preserved verbatim in the report hash.  The archive receives only
    validated immutable token/pool identities; raw catalog metrics are not stored
    as a pool state and never reach the simulator.
    """
    if not isinstance(archive, MarketArchive):
        raise TypeError('archive must be a MarketArchive')
    if not isinstance(page, dict) or not isinstance(page.get('data'), list):
        raise RaydiumCatalogError('Catalog page must contain a data list')
    if len(page['data']) > 100:
        raise RaydiumCatalogError('Catalog page exceeds the configured bound')
    # Preserve only evidence we can serialize canonically.  Validate this before
    # touching the archive so a malformed recorded input cannot cause a partial
    # identity import with no verifiable source page.
    try:
        raw = canonical(page)
    except (TypeError, ValueError) as exc:
        raise RaydiumCatalogError('Catalog page is not canonical JSON') from exc
    if received_at is None:
        received_at = time.time()
    _finite(received_at, 'received_at', minimum=1)
    tokens, pools, accepted, rejected = [], [], [], []
    for index, item in enumerate(page['data']):
        try:
            mint_a, mint_b, pool = _pool(item)
            tokens.extend((mint_a, mint_b))
            pools.append(pool)
            accepted.append(pool_id(pool['program_id'], pool['pool_address']))
        except (RaydiumCatalogError, ArchiveError) as exc:
            rejected.append({'index': index, 'reason': str(exc)})
    try:
        appended = archive.add_identities(tokens, pools)
    except ArchiveError as exc:
        raise RaydiumCatalogError('Archive refused catalog identities: ' + str(exc)) from exc
    report = {'schema': SCHEMA, 'mode': 'shadow_catalog_only',
              'source_url': source_url or 'recorded_input', 'received_at': float(received_at),
              'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'raw': page,
              'accepted_pool_ids': accepted, 'rejected': rejected, 'appended': appended,
              'archive_manifest': archive.verify()}
    report['sha256'] = hashlib.sha256(canonical(report).encode()).hexdigest()
    return report
