"""Append-only storage for lower-assurance, standard-RPC market observations.

This is deliberately separate from :mod:`paper.market_archive`.  Standard RPC
does not expose account write versions, so an observed snapshot cannot honestly
be represented as a strict, transaction-reconcilable pool state.  The log
preserves every self-contained source report and its local collection sequence;
it does not create a fill, run a strategy, or expose a public route.
"""
import base64
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading

from .engine import canonical
from .market_archive import solana_address
from .raydium_cpmm_observed import SCHEMA as RAYDIUM_CPMM_SNAPSHOT_SCHEMA


SCHEMA = 'pumpwizard.observed-market-log/v1'


class ObservedLogError(ValueError):
    """An observation is malformed, conflicts, or fails its portable proof."""


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ObservedLogError('Invalid ' + name)
    return value


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise ObservedLogError('Invalid ' + name)
    return float(value)


def _string(value, name, minimum=1, maximum=200):
    if not isinstance(value, str) or not minimum <= len(value) <= maximum:
        raise ObservedLogError('Invalid ' + name)
    return value


def _json(value, name):
    try:
        return json.loads(canonical(value))
    except (TypeError, ValueError) as exc:
        raise ObservedLogError('Invalid ' + name) from exc


def _account(value, name):
    if not isinstance(value, dict):
        raise ObservedLogError('Invalid ' + name)
    try:
        address = solana_address(value.get('address'), name + ' address')
        owner = solana_address(value.get('owner'), name + ' owner')
    except ValueError as exc:
        raise ObservedLogError('Invalid ' + name + ' address') from exc
    encoded = value.get('data_base64')
    digest = value.get('data_sha256')
    if not isinstance(encoded, str) or not isinstance(digest, str) or len(digest) != 64:
        raise ObservedLogError('Invalid ' + name + ' proof')
    try:
        raw = base64.b64decode(encoded.encode('ascii'), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ObservedLogError('Invalid ' + name + ' encoding') from exc
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ObservedLogError('Invalid ' + name + ' proof')
    return {'address': address, 'owner': owner, 'data_base64': encoded, 'data_sha256': digest}


def normalize_snapshot(record):
    """Validate a Raydium free-RPC report without upgrading its assurance claim."""
    if not isinstance(record, dict) or record.get('schema') != RAYDIUM_CPMM_SNAPSHOT_SCHEMA:
        raise ObservedLogError('Unsupported observed snapshot schema')
    if record.get('mode') != 'observed_standard_rpc_snapshot':
        raise ObservedLogError('Unsupported observed snapshot mode')
    source = record.get('source')
    if not isinstance(source, dict) or source.get('transport') != 'standard_solana_rpc':
        raise ObservedLogError('Invalid observed source')
    if source.get('endpoint_kind') not in ('official_public', 'operator_configured'):
        raise ObservedLogError('Invalid observed source')
    if source.get('write_version') != 'unavailable_not_invented':
        raise ObservedLogError('Observed snapshot must not invent write versions')
    pool = _string(record.get('pool_id'), 'pool_id')
    result = {
        'schema': RAYDIUM_CPMM_SNAPSHOT_SCHEMA,
        'mode': 'observed_standard_rpc_snapshot',
        'source': {'transport': 'standard_solana_rpc', 'endpoint_kind': source['endpoint_kind'],
                   'write_version': 'unavailable_not_invented'},
        'pool_id': pool,
        'sequence': _integer(record.get('sequence'), 'sequence', 1),
        'context_slot': _integer(record.get('context_slot'), 'context_slot', 1),
        'bootstrap_context_slot': _integer(record.get('bootstrap_context_slot'), 'bootstrap_context_slot', 1),
        'commitment': record.get('commitment'),
        'observed_at': _time(record.get('observed_at'), 'observed_at'),
        'pool_account': _account(record.get('pool_account'), 'pool account'),
        'base_vault': _account(record.get('base_vault'), 'base vault'),
        'quote_vault': _account(record.get('quote_vault'), 'quote vault'),
        'config': _account(record.get('config'), 'config'),
        'raw_sha256': record.get('raw_sha256'),
        'raw': _json(record.get('raw'), 'raw'),
        'limitations': _json(record.get('limitations'), 'limitations'),
    }
    if result['commitment'] not in ('confirmed', 'finalized'):
        raise ObservedLogError('Invalid commitment')
    if result['context_slot'] < result['bootstrap_context_slot']:
        raise ObservedLogError('Snapshot context precedes bootstrap context')
    if len({result['pool_account']['address'], result['base_vault']['address'],
            result['quote_vault']['address'], result['config']['address']}) != 4:
        raise ObservedLogError('Observed accounts must be distinct')
    if not isinstance(result['raw_sha256'], str) or len(result['raw_sha256']) != 64:
        raise ObservedLogError('Invalid raw proof')
    if _hash(result['raw']) != result['raw_sha256']:
        raise ObservedLogError('Invalid raw proof')
    expected = _hash(result)
    if record.get('sha256') != expected:
        raise ObservedLogError('Invalid observed snapshot proof')
    result['sha256'] = expected
    return result


class ObservedMarketLog:
    """Append-only observation log with sequence/gap proof per market pool."""
    def __init__(self, path, view_only=False):
        self.path, self.view_only = Path(path), view_only
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
                CREATE TABLE IF NOT EXISTS observed_log_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observed_snapshots(
                  pool_id TEXT NOT NULL,sequence INTEGER NOT NULL,context_slot INTEGER NOT NULL,
                  observed_at REAL NOT NULL,data TEXT NOT NULL,content_sha256 TEXT NOT NULL,
                  PRIMARY KEY(pool_id,sequence));
                CREATE INDEX IF NOT EXISTS observed_snapshots_pool_time
                  ON observed_snapshots(pool_id,observed_at,sequence);
            ''')
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO observed_log_meta VALUES (?,?)', ('schema', SCHEMA))
        row = self.db.execute("SELECT value FROM observed_log_meta WHERE key='schema'").fetchone()
        if not row or row[0] != SCHEMA:
            self.close()
            raise ObservedLogError('Unsupported observed market log schema')

    def close(self):
        self.db.close()

    def next_sequence(self, pool_id):
        pool_id = _string(pool_id, 'pool_id')
        with self.lock:
            row = self.db.execute('SELECT MAX(sequence) AS sequence FROM observed_snapshots WHERE pool_id=?',
                                  (pool_id,)).fetchone()
        return (row['sequence'] or 0) + 1

    def append(self, snapshot):
        data = normalize_snapshot(snapshot)
        if self.view_only:
            raise ObservedLogError('Observed market log is read-only')
        rendered, digest = canonical(data), _hash(data)
        with self.lock, self.db:
            row = self.db.execute('SELECT data,content_sha256 FROM observed_snapshots WHERE pool_id=? AND sequence=?',
                                  (data['pool_id'], data['sequence'])).fetchone()
            if row:
                if row['data'] != rendered or row['content_sha256'] != digest:
                    raise ObservedLogError('Conflicting immutable observed snapshot')
                return False
            expected = self.next_sequence(data['pool_id'])
            if data['sequence'] != expected:
                raise ObservedLogError('Observed snapshot sequence must be consecutive; expected ' + str(expected))
            self.db.execute('INSERT INTO observed_snapshots VALUES (?,?,?,?,?,?)',
                            (data['pool_id'], data['sequence'], data['context_slot'], data['observed_at'], rendered, digest))
            return True

    def get_snapshot(self, pool_id, sequence):
        pool_id, sequence = _string(pool_id, 'pool_id'), _integer(sequence, 'sequence', 1)
        with self.lock:
            row = self.db.execute('SELECT data FROM observed_snapshots WHERE pool_id=? AND sequence=?',
                                  (pool_id, sequence)).fetchone()
        return json.loads(row['data']) if row else None

    def recent(self, pool_id, limit=20):
        pool_id, limit = _string(pool_id, 'pool_id'), _integer(limit, 'limit', 1)
        if limit > 1000:
            raise ObservedLogError('Invalid limit')
        with self.lock:
            rows = self.db.execute('SELECT data FROM observed_snapshots WHERE pool_id=? ORDER BY sequence DESC LIMIT ?',
                                   (pool_id, limit)).fetchall()
        return [json.loads(row['data']) for row in rows]

    def manifest(self):
        with self.lock:
            rows = self.db.execute('SELECT content_sha256 FROM observed_snapshots ORDER BY pool_id,sequence').fetchall()
        proof = hashlib.sha256()
        for row in rows:
            proof.update((row['content_sha256'] + '\n').encode())
        result = {'schema': SCHEMA, 'tables': {'observed_snapshots': {
            'rows': len(rows), 'sha256': proof.hexdigest()}}}
        result['sha256'] = _hash(result)
        return result

    def verify(self):
        """Verify SQLite, source proofs, immutable hashes, and sequence continuity."""
        with self.lock:
            if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise ObservedLogError('SQLite integrity check failed')
            row = self.db.execute("SELECT value FROM observed_log_meta WHERE key='schema'").fetchone()
            if not row or row[0] != SCHEMA:
                raise ObservedLogError('Unsupported observed market log schema')
            sequences = {}
            for row in self.db.execute('SELECT pool_id,sequence,data,content_sha256 FROM observed_snapshots ORDER BY pool_id,sequence'):
                try:
                    data = json.loads(row['data'])
                except json.JSONDecodeError as exc:
                    raise ObservedLogError('Invalid stored observed snapshot JSON') from exc
                try:
                    normalized = normalize_snapshot(data)
                except ObservedLogError as exc:
                    raise ObservedLogError('Invalid stored observed snapshot') from exc
                if normalized != data or _hash(data) != row['content_sha256']:
                    raise ObservedLogError('Invalid stored observed snapshot proof')
                expected = sequences.get(row['pool_id'], 0) + 1
                if row['sequence'] != expected:
                    raise ObservedLogError('Observed snapshot sequence gap')
                sequences[row['pool_id']] = row['sequence']
        return self.manifest()
