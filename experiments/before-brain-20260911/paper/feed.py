"""Read-only public index feed. No keys, signing, trade endpoints or paid streams."""
import concurrent.futures
import json
import math
import re
import time
import urllib.error
import urllib.request

BASE = 'https://frontend-api-v3.pump.fun'
SOL_QUOTES = {'So11111111111111111111111111111111111111112',
              '11111111111111111111111111111111'}
MINT = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')


class FeedError(Exception):
    pass


def get_json(path):
    """URLs are fixed to public read-only endpoints on a single host."""
    if not (path == '/sol-price' or path.startswith('/coins?') or
            path.startswith('/coins/') and MINT.fullmatch(path.split('/')[-1])):
        raise ValueError('Non-market-data path refused')
    req = urllib.request.Request(BASE + path, headers={
        'User-Agent': 'PumpPaperResearch/1.0', 'Accept': 'application/json',
        'Cache-Control': 'no-cache'})
    try:
        with urllib.request.urlopen(req, timeout=8) as response:
            if response.geturl().split('/')[2] != 'frontend-api-v3.pump.fun':
                raise FeedError('Unexpected redirect host')
            body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise FeedError('Oversized response')
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        raise FeedError(f'HTTP {exc.code} for {path.split("?")[0]}') from None
    except (urllib.error.URLError, TimeoutError, ValueError, OSError):
        raise FeedError(f'Market-data request failed for {path.split("?")[0]}') from None


def normalize(coin, fx, observed_at):
    mint = coin.get('mint', '')
    if not MINT.fullmatch(mint):
        raise FeedError('Invalid token address')
    if fx.get('stale', True):
        raise FeedError('Provider marks SOL/USD stale')
    decimals = int(coin.get('base_decimals', 6))
    quote_decimals = int(coin.get('quote_decimals', 9))
    if not 0 <= decimals <= 12 or quote_decimals != 9:
        raise FeedError('Unsupported decimals')
    # Newer API uses quote reserves; legacy SOL fields remain supported.
    y = coin.get('virtual_quote_reserves', coin.get('virtual_sol_reserves'))
    real_y = coin.get('real_quote_reserves', coin.get('real_sol_reserves'))
    quote = coin.get('quote_mint')
    is_sol = quote in SOL_QUOTES
    result = {
        'mint': mint, 'symbol': str(coin.get('symbol', mint[:8]))[:80],
        'observed_at': observed_at, 'source_at': float(coin['updated_at']),
        'fx_at': float(fx['asOfTimestamp']) / 1000, 'quote_usd': float(fx['solPrice']),
        'x': float(coin['virtual_token_reserves']) / 10 ** decimals,
        'y': float(y) / 1e9, 'real_x': float(coin['real_token_reserves']) / 10 ** decimals,
        'real_y': float(real_y) / 1e9,
        'supply': float(coin.get('total_supply_str', coin['total_supply'])) / 10 ** decimals,
        'market_cap_usd': float(coin.get('market_cap_usd', coin.get('usd_market_cap'))),
        'valuation_basis': 'provider_total_supply_times_index_price',
        'decimals': decimals, 'complete': coin.get('complete') is not False,
        'route': 'pump_bonding_curve_sol' if is_sol and coin.get('program') == 'pump' else 'unsupported',
        'mayhem': coin.get('mayhem_state') not in (None, 'disabled', 'none'),
        'token_program': coin.get('token_program'), 'curve_address': coin.get('bonding_curve'),
        'holder_and_mint_checks': 'unavailable_in_snapshot_feed',
        'source': 'pump_public_index_snapshot',
    }
    if any(isinstance(v, (int, float)) and not math.isfinite(v) for v in result.values()):
        raise FeedError('Non-finite market data')
    return result


class PublicFeed:
    def __init__(self, config):
        self.config = config
        self.watch = {}
        self.last_snapshots = {}
        self.last_in_range = {}
        self.discovered = set()
        self.next_discovery = 0
        self.backoff_until = 0
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=4)

    def close(self):
        self.pool.shutdown(wait=True, cancel_futures=True)

    def refresh_watch(self, incoming, now, priority=(), blocks=None):
        """Rotate slots without evicting positions or pending entries."""
        c = self.config
        protected = set(priority)
        blocked = {mint for mint, until in (blocks or {}).items() if until is None or until > now}
        for mint, added in list(self.watch.items()):
            if mint in protected:
                continue
            snap = self.last_snapshots.get(mint)
            stale = snap and now - min(snap['source_at'], snap['observed_at']) > c['watch_stale_seconds']
            unsupported = snap and (snap['complete'] or snap['mayhem'] or snap['route'] != 'pump_bonding_curve_sol')
            outside = snap and not c['market_cap_min_usd'] <= snap['market_cap_usd'] <= c['market_cap_max_usd']
            out_too_long = outside and now - self.last_in_range.get(mint, added) >= c['watch_out_of_range_seconds']
            if (mint in blocked or stale or unsupported or out_too_long or
                    now - added > c['watch_retention_seconds']):
                del self.watch[mint]
        for mint in protected:
            self.watch.setdefault(mint, now)
        ranked = []
        for coin in incoming.values():
            try:
                mint = coin['mint']
                cap = float(coin.get('market_cap_usd', coin.get('usd_market_cap')))
                updated = float(coin['updated_at'])
                last_trade = float(coin.get('last_trade_timestamp') or 0)
                if not all(math.isfinite(v) for v in (cap, updated, last_trade)):
                    continue
                if (mint in blocked or coin.get('complete') is not False or
                        coin.get('quote_mint') not in SOL_QUOTES or
                        coin.get('mayhem_state') not in (None, 'none', 'disabled') or
                        not 0 <= now - updated <= c['max_source_age_seconds'] or
                        not c['market_cap_min_usd'] * 0.5 <= cap <= c['market_cap_max_usd'] * 1.2):
                    continue
                in_range = c['market_cap_min_usd'] <= cap <= c['market_cap_max_usd']
                # A new fresh observation in range can requalify a previously retired coin.
                if mint not in self.watch and not in_range and mint in self.last_snapshots:
                    continue
                ranked.append((not in_range, -last_trade, mint))
            except (KeyError, TypeError, ValueError):
                continue
        for _, _, mint in sorted(ranked):
            if mint not in self.watch and len(self.watch) < c['max_watch_tokens']:
                self.watch[mint] = now
                self.last_snapshots.pop(mint, None)
                self.last_in_range.pop(mint, None)
        # Retain metadata briefly to avoid rediscovering the same out-of-range coin.
        for mint, snap in list(self.last_snapshots.items()):
            if mint not in self.watch and now - snap['observed_at'] > c['watch_retention_seconds']:
                self.last_snapshots.pop(mint, None)
                self.last_in_range.pop(mint, None)

    def collect(self, priority=(), blocks=None):
        start = time.time()
        issues, snapshots = [], []
        if start < self.backoff_until:
            return {'at': start, 'snapshots': [], 'issues': ['Source backoff after failed request'],
                    'status': 'backoff', 'discovered_count': len(self.discovered)}
        try:
            fx = get_json('/sol-price')
        except FeedError as exc:
            self.backoff_until = time.time() + 30
            return {'at': time.time(), 'snapshots': [], 'issues': [str(exc)],
                    'status': 'unavailable', 'discovered_count': len(self.discovered)}
        if start >= self.next_discovery:
            self.next_discovery = start + self.config['discovery_seconds']
            paths = [f'/coins?offset=0&limit=70&sort={sort}&order=DESC&includeNsfw=false&complete=false'
                     for sort in ('created_timestamp', 'last_trade_timestamp')]
            futures = [self.pool.submit(get_json, p) for p in paths]
            incoming = {}
            for future in futures:
                try:
                    coins = future.result()
                    if not isinstance(coins, list):
                        raise FeedError('Unexpected discovery schema')
                    for coin in coins:
                        mint = coin.get('mint', '')
                        if MINT.fullmatch(mint):
                            self.discovered.add(mint)
                            incoming[mint] = coin
                except (FeedError, TypeError, AttributeError) as exc:
                    issues.append(str(exc))
            self.refresh_watch(incoming, start, priority, blocks)
        for mint in priority:
            self.watch[mint] = start
        futures = {self.pool.submit(get_json, '/coins/' + mint): mint for mint in self.watch}
        for future in concurrent.futures.as_completed(futures):
            mint = futures[future]
            try:
                raw = future.result()
                if raw.get('mint') != mint:
                    raise FeedError('Token response address mismatch')
                snap = normalize(raw, fx, time.time())
                snapshots.append(snap)
                self.last_snapshots[mint] = snap
                if self.config['market_cap_min_usd'] <= snap['market_cap_usd'] <= self.config['market_cap_max_usd']:
                    self.last_in_range[mint] = snap['observed_at']
            except (FeedError, KeyError, ValueError, TypeError, AttributeError) as exc:
                issues.append(f'{mint[:8]}: {type(exc).__name__}: {exc}')
        if any('HTTP 429' in x for x in issues):
            self.backoff_until = time.time() + 60
        return {'at': time.time(), 'snapshots': sorted(snapshots, key=lambda x: x['mint']),
                'issues': issues, 'status': 'degraded' if issues else 'ok',
                'discovered_count': len(self.discovered), 'watch_count': len(self.watch),
                'collection_seconds': time.time() - start}
