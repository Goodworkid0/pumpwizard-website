"""Isolated, quote-denominated paper ledger for observed Raydium CPMM data.

This is not the Pump.fun engine and it does not model a live wallet.  Explicit
paper entries and exits are estimated from immutable standard-RPC snapshots;
USD P/L is intentionally unavailable until a separately evidenced FX source is
introduced.
"""
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import threading
import time

from .engine import canonical
from .market_archive import MarketArchive
from .observed_market_log import ObservedMarketLog
from .raydium_cpmm_observed_quote import RaydiumObservedQuoteError, quote


SCHEMA = 'pumpwizard.raydium-cpmm-observed-ledger/v1'


class RaydiumObservedLedgerError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RaydiumObservedLedgerError('Invalid ' + name)
    return value


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RaydiumObservedLedgerError('Invalid ' + name)
    return float(value)


def _config(value):
    if not isinstance(value, dict):
        raise RaydiumObservedLedgerError('Ledger config must be an object')
    pool_id = value.get('pool_id')
    if not isinstance(pool_id, str) or not 1 <= len(pool_id) <= 200:
        raise RaydiumObservedLedgerError('Invalid pool_id')
    result = {'pool_id': pool_id,
              'initial_quote_amount': _integer(value.get('initial_quote_amount'), 'initial_quote_amount', 1),
              'max_positions': _integer(value.get('max_positions', 1), 'max_positions', 1),
              'max_snapshot_age_seconds': _time(value.get('max_snapshot_age_seconds', 120),
                                                'max_snapshot_age_seconds')}
    if result['max_positions'] > 100 or result['max_snapshot_age_seconds'] > 3600:
        raise RaydiumObservedLedgerError('Invalid ledger config')
    return result


def _source(value):
    if not isinstance(value, dict) or value.get('market') != 'raydium-cpmm' or value.get('assurance') != 'observed_simulation_only':
        raise RaydiumObservedLedgerError('Invalid observed simulation source')
    return {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only'}


class ObservedRaydiumLedger:
    """Persistent manual paper entries/exits, isolated by a fixed market contract."""
    def __init__(self, path, config, source, view_only=False):
        self.path, self.config, self.source, self.view_only = Path(path), _config(config), _source(source), view_only
        if not view_only:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = 'file:' + str(self.path) + ('?mode=ro' if view_only else '')
        self.db = sqlite3.connect(uri, uri=view_only, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        implementation_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        self.contract = canonical({'schema': SCHEMA, 'config': self.config, 'source': self.source,
                                   'implementation_hash': implementation_hash})
        if not view_only:
            self.db.executescript('''
                PRAGMA journal_mode=WAL;
                PRAGMA synchronous=FULL;
                CREATE TABLE IF NOT EXISTS observed_ledger_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observed_ledger_events(
                  id INTEGER PRIMARY KEY,at REAL NOT NULL,kind TEXT NOT NULL,data TEXT NOT NULL,content_sha256 TEXT NOT NULL);
            ''')
            with self.db:
                self.db.execute('INSERT OR IGNORE INTO observed_ledger_meta VALUES (?,?)', ('schema', SCHEMA))
                existing = self.db.execute("SELECT value FROM observed_ledger_meta WHERE key='contract'").fetchone()
                if existing and existing['value'] != self.contract:
                    raise RaydiumObservedLedgerError('Simulation config/source changed; use a new ledger')
                self.db.execute('INSERT OR IGNORE INTO observed_ledger_meta VALUES (?,?)', ('contract', self.contract))
        schema = self.db.execute("SELECT value FROM observed_ledger_meta WHERE key='schema'").fetchone()
        contract = self.db.execute("SELECT value FROM observed_ledger_meta WHERE key='contract'").fetchone()
        if not schema or schema['value'] != SCHEMA or not contract:
            self.close(); raise RaydiumObservedLedgerError('Unsupported observed ledger schema')
        if view_only:
            stored = json.loads(contract['value'])
            if stored.get('schema') != SCHEMA or stored.get('config') != self.config or stored.get('source') != self.source:
                self.close(); raise RaydiumObservedLedgerError('Observed ledger contract does not match')
        self.state = self._load_state()

    def close(self):
        self.db.close()

    def _load_state(self):
        state = {'cash_quote_amount': self.config['initial_quote_amount'], 'positions': {}, 'last_sequence': 0,
                 'started_at': None, 'base_mint': None, 'quote_mint': None}
        rows = self.db.execute('SELECT at,kind,data FROM observed_ledger_events ORDER BY id').fetchall()
        for row in rows:
            data = json.loads(row['data'])
            if row['kind'] == 'entry':
                position = data['position']
                state['cash_quote_amount'] -= position['cost_quote_amount']
                state['positions'][position['id']] = position
                state['last_sequence'] = max(state['last_sequence'], position['entry_sequence'])
                state['started_at'] = state['started_at'] or row['at']
                state['base_mint'], state['quote_mint'] = position['base_mint'], position['quote_mint']
            elif row['kind'] == 'exit':
                position = state['positions'].get(data['position_id'])
                if not position or position.get('status') != 'open':
                    raise RaydiumObservedLedgerError('Invalid stored observed ledger exit')
                position.update(status='closed', exit_sequence=data['sequence'], exit_at=row['at'],
                                exit_quote_amount=data['amount_out'], pnl_quote_amount=data['pnl_quote_amount'],
                                exit_quote_sha256=data['quote_sha256'])
                state['cash_quote_amount'] += data['amount_out']
                state['last_sequence'] = max(state['last_sequence'], data['sequence'])
            else:
                raise RaydiumObservedLedgerError('Invalid stored observed ledger event')
        return state

    def _assert_sources(self, archive, log):
        if not isinstance(archive, MarketArchive) or not archive.view_only:
            raise RaydiumObservedLedgerError('Observed ledger requires a read-only market archive')
        if not isinstance(log, ObservedMarketLog) or not log.view_only:
            raise RaydiumObservedLedgerError('Observed ledger requires a read-only observed market log')

    def _fresh_quote(self, sequence, input_mint, amount_in, archive, log, at):
        try:
            result = quote(self.config['pool_id'], sequence, input_mint, amount_in, archive, log)
        except RaydiumObservedQuoteError as exc:
            raise RaydiumObservedLedgerError('Observed quote refused: ' + str(exc)) from exc
        if result['snapshot']['observed_at'] > at:
            raise RaydiumObservedLedgerError('Observed snapshot is from the future')
        if at - result['snapshot']['observed_at'] > self.config['max_snapshot_age_seconds']:
            raise RaydiumObservedLedgerError('Observed snapshot is stale for simulation')
        return result

    def _append(self, at, kind, data):
        if self.view_only:
            raise RaydiumObservedLedgerError('Observed ledger is read-only')
        rendered, digest = canonical(data), _hash(data)
        self.db.execute('INSERT INTO observed_ledger_events(at,kind,data,content_sha256) VALUES (?,?,?,?)',
                        (at, kind, rendered, digest))

    def open_position(self, position_id, sequence, quote_amount, archive, log, at=None):
        """Model a manual quote-token-to-base-token entry from one fresh snapshot."""
        if not isinstance(position_id, str) or not 1 <= len(position_id) <= 80:
            raise RaydiumObservedLedgerError('Invalid position_id')
        at = _time(time.time() if at is None else at, 'at')
        quote_amount = _integer(quote_amount, 'quote_amount', 1)
        with self.lock, self.db:
            self._assert_sources(archive, log)
            if position_id in self.state['positions']:
                raise RaydiumObservedLedgerError('Position id already exists')
            if sum(position['status'] == 'open' for position in self.state['positions'].values()) >= self.config['max_positions']:
                raise RaydiumObservedLedgerError('Maximum open positions reached')
            if quote_amount > self.state['cash_quote_amount']:
                raise RaydiumObservedLedgerError('Insufficient virtual quote balance')
            pool = archive.get_pool(self.config['pool_id'])
            if not pool:
                raise RaydiumObservedLedgerError('Unknown Raydium CPMM pool')
            result = self._fresh_quote(sequence, pool['quote_mint'], quote_amount, archive, log, at)
            position = {'id': position_id, 'status': 'open', 'base_mint': pool['base_mint'],
                        'quote_mint': pool['quote_mint'], 'entry_sequence': sequence, 'entry_at': at,
                        'cost_quote_amount': quote_amount, 'base_amount': result['amount_out'],
                        'entry_quote_sha256': result['sha256']}
            self._append(at, 'entry', {'position': position})
            self.state['cash_quote_amount'] -= quote_amount
            self.state['positions'][position_id] = position
            self.state['last_sequence'] = max(self.state['last_sequence'], sequence)
            self.state['started_at'] = self.state['started_at'] or at
            self.state['base_mint'], self.state['quote_mint'] = pool['base_mint'], pool['quote_mint']
            return dict(position)

    def close_position(self, position_id, sequence, archive, log, at=None):
        """Model an explicit base-token exit at a strictly later fresh snapshot."""
        at = _time(time.time() if at is None else at, 'at')
        with self.lock, self.db:
            self._assert_sources(archive, log)
            position = self.state['positions'].get(position_id)
            if not position or position['status'] != 'open':
                raise RaydiumObservedLedgerError('No open position with this id')
            if not isinstance(sequence, int) or sequence <= position['entry_sequence']:
                raise RaydiumObservedLedgerError('Exit sequence must follow entry sequence')
            result = self._fresh_quote(sequence, position['base_mint'], position['base_amount'], archive, log, at)
            pnl = result['amount_out'] - position['cost_quote_amount']
            event = {'position_id': position_id, 'sequence': sequence, 'amount_out': result['amount_out'],
                     'pnl_quote_amount': pnl, 'quote_sha256': result['sha256']}
            self._append(at, 'exit', event)
            position.update(status='closed', exit_sequence=sequence, exit_at=at, exit_quote_amount=result['amount_out'],
                            pnl_quote_amount=pnl, exit_quote_sha256=result['sha256'])
            self.state['cash_quote_amount'] += result['amount_out']
            self.state['last_sequence'] = max(self.state['last_sequence'], sequence)
            return dict(position)

    def summary(self, archive, log, mark_sequence=None, at=None):
        """Return quote-token P/L and impaired marks; it never changes the ledger."""
        at = _time(time.time() if at is None else at, 'at')
        with self.lock:
            self._assert_sources(archive, log)
            open_positions = [position for position in self.state['positions'].values() if position['status'] == 'open']
            if mark_sequence is None and open_positions:
                latest = log.recent(self.config['pool_id'], limit=1)
                if not latest:
                    raise RaydiumObservedLedgerError('No observed snapshots available for mark')
                mark_sequence = latest[0]['sequence']
            elif mark_sequence is None:
                mark_sequence = self.state['last_sequence'] or None
            equity, impaired, marked = self.state['cash_quote_amount'], 0, []
            for position in open_positions:
                try:
                    estimate = self._fresh_quote(mark_sequence, position['base_mint'], position['base_amount'], archive, log, at)
                    value = estimate['amount_out']
                    equity += value
                    marked.append({'id': position['id'], 'status': 'marked', 'quote_amount': value,
                                   'unrealized_pnl_quote_amount': value - position['cost_quote_amount'],
                                   'quote_sha256': estimate['sha256']})
                except RaydiumObservedLedgerError as exc:
                    impaired += 1
                    marked.append({'id': position['id'], 'status': 'impaired', 'reason': str(exc)})
            closed = [position for position in self.state['positions'].values() if position['status'] == 'closed']
            return {'schema': SCHEMA, 'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only',
                    'denomination': {'mint': self.state['quote_mint'], 'unit': 'raw_quote_token_amount',
                                     'usd_valuation': 'unavailable_without_separate_fx_evidence'},
                    'pool_id': self.config['pool_id'], 'cash_quote_amount': self.state['cash_quote_amount'],
                    'marked_equity_quote_amount': equity, 'impaired_position_count': impaired,
                    'open_position_count': len(marked), 'closed_position_count': len(closed),
                    'realized_pnl_quote_amount': sum(position.get('pnl_quote_amount', 0) for position in closed),
                    'last_sequence': self.state['last_sequence'], 'mark_sequence': mark_sequence,
                    'positions': marked, 'limitations': ['observed simulation only', 'standard RPC snapshot',
                                                          'not executable', 'USD valuation unavailable']}

    def verify(self):
        """Validate persistent event hashes and replay the complete isolated ledger."""
        with self.lock:
            if self.db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
                raise RaydiumObservedLedgerError('SQLite integrity check failed')
            for row in self.db.execute('SELECT data,content_sha256 FROM observed_ledger_events ORDER BY id'):
                try:
                    data = json.loads(row['data'])
                except json.JSONDecodeError as exc:
                    raise RaydiumObservedLedgerError('Invalid stored observed ledger event') from exc
                if _hash(data) != row['content_sha256']:
                    raise RaydiumObservedLedgerError('Invalid stored observed ledger proof')
            state = self._load_state()
            rows = self.db.execute('SELECT content_sha256 FROM observed_ledger_events ORDER BY id').fetchall()
        proof = hashlib.sha256()
        for row in rows:
            proof.update((row['content_sha256'] + '\n').encode())
        result = {'schema': SCHEMA, 'contract_sha256': _hash(json.loads(self.contract)),
                  'events': len(rows), 'open_positions': sum(item['status'] == 'open' for item in state['positions'].values()),
                  'sha256': proof.hexdigest()}
        return result
