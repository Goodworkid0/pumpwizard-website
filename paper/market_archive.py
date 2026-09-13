"""Immutable, offline Solana market archive foundations.

This module intentionally has no network client, collector hook, strategy import,
or public HTTP route.  It records validated identities and observed pool data so a
future collector can be introduced behind a stable, independently verifiable
boundary.  It never creates, signs, simulates, or submits a Solana transaction.
"""
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
import threading

from .engine import canonical


SCHEMA = 'pumpwizard.market-archive/v1'
CHAIN = 'solana-mainnet-beta'
ADDRESS = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{32,44}$')
SIGNATURE = re.compile(r'^[1-9A-HJ-NP-Za-km-z]{64,100}$')
POOL_KINDS = frozenset(('constant_product', 'concentrated_liquidity', 'bin_liquidity', 'bonding_curve'))
COMMITMENTS = frozenset(('processed', 'confirmed', 'finalized'))
EVENT_KINDS = frozenset(('pool_created', 'swap', 'liquidity_added', 'liquidity_removed', 'migration'))


class ArchiveError(ValueError):
    """A record is malformed, conflicts with an immutable row, or fails proof."""


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _string(value, name, minimum=1, maximum=160):
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ArchiveError('Invalid ' + name)
    return value


def _address(value, name):
    value = _string(value, name, 32, 44)
    if not ADDRESS.fullmatch(value):
        raise ArchiveError('Invalid ' + name)
    return value


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ArchiveError('Invalid ' + name)
    return value


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ArchiveError('Invalid ' + name)
    return float(value)


def _json(value, name):
    try:
        rendered = canonical(value)
    except (TypeError, ValueError) as exc:
        raise ArchiveError('Invalid ' + name) from exc
    return json.loads(rendered)


def token_id(mint):
    """Stable token identity; this deliberately excludes mutable display metadata."""
    return 'solana:' + _address(mint, 'mint')


def solana_address(value, name='address'):
    """Validate and return a Solana public-key address for collector adapters."""
    return _address(value, name)


def pool_id(program_id, address):
    return 'solana:' + _address(program_id, 'program_id') + ':' + _address(address, 'pool_address')


def event_id(signature, instruction_index):
    signature = _string(signature, 'signature', 64, 100)
    if not SIGNATURE.fullmatch(signature):
        raise ArchiveError('Invalid signature')
    return 'solana:' + signature + ':' + str(_integer(instruction_index, 'instruction_index'))


def normalize_token(record):
    if not isinstance(record, dict):
        raise ArchiveError('Token must be an object')
    mint = _address(record.get('mint'), 'mint')
    decimals = _integer(record.get('decimals'), 'decimals')
    if decimals > 255:
        raise ArchiveError('Invalid decimals')
    result = {'id': token_id(mint), 'chain': CHAIN, 'mint': mint, 'decimals': decimals}
    # Symbol and name are optional display hints.  They are immutable only within
    # this archive identity record and are never used for execution or matching.
    for name, maximum in (('symbol', 80), ('name', 160)):
        if name in record and record[name] is not None:
            result[name] = _string(record[name], name, 1, maximum)
    return result


def normalize_pool(record):
    if not isinstance(record, dict):
        raise ArchiveError('Pool must be an object')
    program_id = _address(record.get('program_id'), 'program_id')
    address = _address(record.get('pool_address'), 'pool_address')
    kind = record.get('pool_kind')
    if kind not in POOL_KINDS:
        raise ArchiveError('Unsupported pool_kind')
    dex = _string(record.get('dex'), 'dex', 2, 40).lower()
    if not re.fullmatch(r'[a-z0-9_-]+', dex):
        raise ArchiveError('Invalid dex')
    base_mint = _address(record.get('base_mint'), 'base_mint')
    quote_mint = _address(record.get('quote_mint'), 'quote_mint')
    if base_mint == quote_mint:
        raise ArchiveError('Pool base and quote mints must differ')
    result = {'id': pool_id(program_id, address), 'chain': CHAIN, 'dex': dex,
              'program_id': program_id, 'pool_address': address, 'pool_kind': kind,
              'base_mint': base_mint, 'quote_mint': quote_mint}
    if 'configuration' in record:
        result['configuration'] = _json(record['configuration'], 'configuration')
    return result


def normalize_event(record):
    if not isinstance(record, dict):
        raise ArchiveError('Event must be an object')
    signature = _string(record.get('signature'), 'signature', 64, 100)
    index = _integer(record.get('instruction_index'), 'instruction_index')
    event_kind = record.get('event_kind')
    if event_kind not in EVENT_KINDS:
        raise ArchiveError('Unsupported event_kind')
    commitment = record.get('commitment')
    if commitment not in COMMITMENTS:
        raise ArchiveError('Invalid commitment')
    result = {'id': event_id(signature, index), 'pool_id': _string(record.get('pool_id'), 'pool_id', 1, 200),
              'signature': signature, 'instruction_index': index, 'slot': _integer(record.get('slot'), 'slot', 1),
              'event_kind': event_kind, 'commitment': commitment,
              'occurred_at': _time(record.get('occurred_at'), 'occurred_at'),
              'received_at': _time(record.get('received_at'), 'received_at'),
              'parser': _string(record.get('parser'), 'parser', 3, 100),
              'payload': _json(record.get('payload'), 'payload')}
    if result['received_at'] < result['occurred_at']:
        raise ArchiveError('received_at precedes occurred_at')
    result['content_sha256'] = _hash({k: v for k, v in result.items() if k != 'content_sha256'})
    return result


def normalize_pool_state(record):
    if not isinstance(record, dict):
        raise ArchiveError('Pool state must be an object')
    result = {'pool_id': _string(record.get('pool_id'), 'pool_id', 1, 200),
              'slot': _integer(record.get('slot'), 'slot', 1),
              'write_version': _integer(record.get('write_version'), 'write_version'),
              'commitment': record.get('commitment'),
              'observed_at': _time(record.get('observed_at'), 'observed_at'),
              'parser': _string(record.get('parser'), 'parser', 3, 100),
              'state': _json(record.get('state'), 'state')}
    if result['commitment'] not in COMMITMENTS:
        raise ArchiveError('Invalid commitment')
    result['content_sha256'] = _hash({k: v for k, v in result.items() if k != 'content_sha256'})
    return result


class MarketArchive:
    """Append-only identity and market-observation store with a portable proof."""
    def __init__(self, path, view_only=False):
        self.path = Path(path)
        self.view_only = view_only
        if not view_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = 'file:' + str(self.path) + ('?mode=ro' if view_only else '')
        self.db = sqlite3.connect(uri, uri=view_only, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        if not view_only:
            self.db.executescript('''
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS archive_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS tokens(id TEXT PRIMARY KEY,data TEXT NOT NULL,content_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pools(id TEXT PRIMARY KEY,data TEXT NOT NULL,content_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS market_events(id TEXT PRIMARY KEY,pool_id TEXT NOT NULL,data TEXT NOT NULL,content_sha256 TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS pool_states(pool_id TEXT NOT NULL,slot INTEGER NOT NULL,write_version INTEGER NOT NULL,data TEXT NOT NULL,content_sha256 TEXT NOT NULL,PRIMARY KEY(pool_id,slot,write_version));
                CREATE INDEX IF NOT EXISTS market_events_pool_slot ON market_events(pool_id,id);
                CREATE INDEX IF NOT EXISTS pool_states_pool_slot ON pool_states(pool_id,slot,write_version);
            ''')
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO archive_meta VALUES (?,?)', ('schema', SCHEMA))
        row = self.db.execute("SELECT value FROM archive_meta WHERE key='schema'").fetchone()
        if not row or row[0] != SCHEMA:
            self.close()
            raise ArchiveError('Unsupported market archive schema')

    def close(self):
        self.db.close()

    def _append(self, table, identity, data, extra=()):
        if self.view_only:
            raise ArchiveError('Archive is read-only')
        content_hash = _hash(data)
        where = ' AND '.join(key + '=?' for key, _ in identity)
        values = tuple(value for _, value in identity)
        row = self.db.execute('SELECT data,content_sha256 FROM ' + table + ' WHERE ' + where, values).fetchone()
        if row:
            if row['content_sha256'] != content_hash or row['data'] != canonical(data):
                raise ArchiveError('Conflicting immutable ' + table + ' record')
            return False
        columns = [key for key, _ in identity] + [key for key, _ in extra] + ['data', 'content_sha256']
        args = values + tuple(value for _, value in extra) + (canonical(data), content_hash)
        self.db.execute('INSERT INTO ' + table + '(' + ','.join(columns) + ') VALUES (' + ','.join('?' for _ in columns) + ')', args)
        return True

    def add_token(self, record):
        data = normalize_token(record)
        with self.lock, self.db:
            return self._append('tokens', (('id', data['id']),), data)

    def add_pool(self, record):
        data = normalize_pool(record)
        with self.lock, self.db:
            for mint in (data['base_mint'], data['quote_mint']):
                if not self.db.execute('SELECT 1 FROM tokens WHERE id=?', (token_id(mint),)).fetchone():
                    raise ArchiveError('Unknown token for pool')
            return self._append('pools', (('id', data['id']),), data)

    def add_identities(self, tokens, pools):
        """Atomically append a normalized token/pool discovery batch.

        A catalog page has no chain slot or executable reserve state.  This method
        therefore records only immutable identities and refuses a page whose pool
        references neither an already recorded nor a newly supplied token.
        """
        normalized_tokens = [normalize_token(record) for record in tokens]
        normalized_pools = [normalize_pool(record) for record in pools]
        token_records = {record['id']: record for record in normalized_tokens}
        if len(token_records) != len(normalized_tokens):
            for record in normalized_tokens:
                if token_records[record['id']] != record:
                    raise ArchiveError('Conflicting token identities in batch')
        pool_records = {record['id']: record for record in normalized_pools}
        if len(pool_records) != len(normalized_pools):
            for record in normalized_pools:
                if pool_records[record['id']] != record:
                    raise ArchiveError('Conflicting pool identities in batch')
        if self.view_only:
            raise ArchiveError('Archive is read-only')
        with self.lock, self.db:
            known = {row[0] for row in self.db.execute('SELECT id FROM tokens')}
            known.update(token_records)
            for pool in pool_records.values():
                for mint in (pool['base_mint'], pool['quote_mint']):
                    if token_id(mint) not in known:
                        raise ArchiveError('Unknown token for pool')
            added_tokens = sum(self._append('tokens', (('id', record['id']),), record)
                               for record in token_records.values())
            added_pools = sum(self._append('pools', (('id', record['id']),), record)
                              for record in pool_records.values())
            return {'tokens': added_tokens, 'pools': added_pools}

    def add_event(self, record):
        data = normalize_event(record)
        with self.lock, self.db:
            if not self.db.execute('SELECT 1 FROM pools WHERE id=?', (data['pool_id'],)).fetchone():
                raise ArchiveError('Unknown pool for market event')
            return self._append('market_events', (('id', data['id']),), data, (('pool_id', data['pool_id']),))

    def add_pool_state(self, record):
        data = normalize_pool_state(record)
        with self.lock, self.db:
            if not self.db.execute('SELECT 1 FROM pools WHERE id=?', (data['pool_id'],)).fetchone():
                raise ArchiveError('Unknown pool for state')
            return self._append('pool_states', (('pool_id', data['pool_id']), ('slot', data['slot']), ('write_version', data['write_version'])), data)

    def add_pool_states(self, records):
        """Atomically append a batch of independently slot-versioned states."""
        normalized = [normalize_pool_state(record) for record in records]
        keyed = {(record['pool_id'], record['slot'], record['write_version']): record
                 for record in normalized}
        if len(keyed) != len(normalized):
            for record in normalized:
                key = (record['pool_id'], record['slot'], record['write_version'])
                if keyed[key] != record:
                    raise ArchiveError('Conflicting pool states in batch')
        if self.view_only:
            raise ArchiveError('Archive is read-only')
        with self.lock, self.db:
            for pool, _, _ in keyed:
                if not self.db.execute('SELECT 1 FROM pools WHERE id=?', (pool,)).fetchone():
                    raise ArchiveError('Unknown pool for state')
            return sum(self._append('pool_states',
                                    (('pool_id', record['pool_id']), ('slot', record['slot']),
                                     ('write_version', record['write_version'])), record)
                       for record in keyed.values())

    def get_pool(self, identity):
        """Read one normalized pool identity without creating or changing anything."""
        identity = _string(identity, 'pool_id', 1, 200)
        with self.lock:
            row = self.db.execute('SELECT data FROM pools WHERE id=?', (identity,)).fetchone()
        return json.loads(row['data']) if row else None

    def manifest(self):
        """Return a deterministic proof of all immutable records, not a trust claim."""
        with self.lock:
            tables = {}
            for table, order in (('tokens', 'id'), ('pools', 'id'), ('market_events', 'id'),
                                 ('pool_states', 'pool_id,slot,write_version')):
                rows = self.db.execute('SELECT data,content_sha256 FROM ' + table + ' ORDER BY ' + order).fetchall()
                proof = hashlib.sha256()
                for row in rows:
                    proof.update((row['content_sha256'] + '\n').encode())
                tables[table] = {'rows': len(rows), 'sha256': proof.hexdigest()}
            result = {'schema': SCHEMA, 'chain': CHAIN, 'tables': tables}
            result['sha256'] = _hash(result)
            return result

    def verify(self):
        """Check schema, SQLite integrity, hashes, normalization and references."""
        with self.lock:
            if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ArchiveError('SQLite integrity check failed')
            row = self.db.execute("SELECT value FROM archive_meta WHERE key='schema'").fetchone()
            if not row or row[0] != SCHEMA:
                raise ArchiveError('Unsupported market archive schema')
            normalizers = {'tokens': normalize_token, 'pools': normalize_pool,
                           'market_events': normalize_event, 'pool_states': normalize_pool_state}
            for table, normalizer in normalizers.items():
                for row in self.db.execute('SELECT data,content_sha256 FROM ' + table):
                    try:
                        data = json.loads(row['data'])
                    except json.JSONDecodeError as exc:
                        raise ArchiveError('Invalid stored ' + table + ' JSON') from exc
                    try:
                        normalized = normalizer(data)
                    except ArchiveError as exc:
                        raise ArchiveError('Invalid stored ' + table + ' record') from exc
                    if normalized != data or _hash(data) != row['content_sha256']:
                        raise ArchiveError('Invalid stored ' + table + ' proof')
            for row in self.db.execute('SELECT id,pool_id FROM market_events'):
                if not self.db.execute('SELECT 1 FROM pools WHERE id=?', (row['pool_id'],)).fetchone():
                    raise ArchiveError('Event references missing pool: ' + row['id'])
            for row in self.db.execute('SELECT id,data FROM pools'):
                pool = json.loads(row['data'])
                for mint in (pool['base_mint'], pool['quote_mint']):
                    if not self.db.execute('SELECT 1 FROM tokens WHERE id=?', (token_id(mint),)).fetchone():
                        raise ArchiveError('Pool references missing token: ' + row['id'])
            for row in self.db.execute('SELECT pool_id,slot,write_version FROM pool_states'):
                if not self.db.execute('SELECT 1 FROM pools WHERE id=?', (row['pool_id'],)).fetchone():
                    raise ArchiveError('State references missing pool')
            return self.manifest()
