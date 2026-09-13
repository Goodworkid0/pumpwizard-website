"""Persistent paper ledger. All mutations are atomic and replayable."""
import copy
import csv
import hashlib
import io
import json
import sqlite3
import threading
import zlib
from datetime import datetime, timezone
from pathlib import Path

from .model import usable, buy_quote, sell_quote


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(',', ':'), allow_nan=False)


class Engine:
    def __init__(self, db_path, config, source, view_only=False):
        self.config = config
        self.source = source
        self.view_only = view_only
        self.lock = threading.RLock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
          PRAGMA journal_mode=WAL;
          PRAGMA synchronous=FULL;
          CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS batches (id TEXT PRIMARY KEY, at REAL, payload BLOB);
          CREATE TABLE IF NOT EXISTS trades (id INTEGER PRIMARY KEY, mint TEXT, data TEXT);
          CREATE TABLE IF NOT EXISTS decisions
            (id INTEGER PRIMARY KEY, at REAL, mint TEXT, action TEXT, reason TEXT, data TEXT);
          CREATE TABLE IF NOT EXISTS equity
            (id INTEGER PRIMARY KEY, at REAL, value REAL, cash REAL, impaired INTEGER);
        ''')
        code = b''.join((Path(__file__).parent / name).read_bytes()
                        for name in ('engine.py', 'model.py', 'feed.py'))
        self.implementation_hash = hashlib.sha256(code).hexdigest()
        contract = canonical({'config': config, 'source': source, 'schema': 1,
                              'implementation_hash': self.implementation_hash})
        old = self.db.execute("SELECT value FROM meta WHERE key='contract'").fetchone()
        if old and old[0] != contract and not view_only:
            self.db.close()
            raise ValueError('Experiment config/source changed. Use a new database; never mix runs.')
        if not old:
            with self.db:
                self.db.execute('INSERT INTO meta VALUES (?, ?)', ('contract', contract))
        elif view_only:
            self.implementation_hash = json.loads(old[0]).get('implementation_hash', 'unrecorded')
        stored = self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
        self.state = json.loads(stored[0]) if stored else {
            'cash_usd': config['virtual_balance_usd'], 'positions': {}, 'pending_entries': {},
            'traded_mints': [], 'snapshots': {}, 'histories': {}, 'candidates': {},
            'last_at': None, 'started_at': None, 'day': None,
            'day_start_equity': config['virtual_balance_usd'], 'day_halted': False,
            'peak_equity': config['virtual_balance_usd'], 'drawdown_halted': False,
            'max_drawdown_fraction': 0, 'failed_fees_usd': 0, 'failed_attempts': 0,
            'operating_cost_usd': 0, 'attempts': 0, 'observations': 0, 'batches': 0,
            'discovered_count': 0, 'last_issues': [], 'last_source_status': 'not_started'
        }
        self.state.setdefault('entry_attempts_by_mint', {})
        self.state.setdefault('last_flat_at', {})
        self.state.setdefault('recent_buy_attempts', [])
        self.state.setdefault('profit_exit_pending', False)
        self.state.setdefault('run_result', None)

    def _entry_block_reason(self):
        s = self.state
        if s['run_result']:
            return s['run_result']
        if s['profit_exit_pending']:
            return 'profit_target_exit'
        if s['drawdown_halted']:
            return 'drawdown'
        if s['day_halted']:
            return 'daily_loss'
        return None

    def _run_policy(self, t, equity):
        """Finish only on settled cash; a profitable mark first schedules real-model exits."""
        s, c = self.state, self.config
        if s['run_result']:
            return
        target = c.get('profit_target_usd', 0)
        threshold = c['virtual_balance_usd'] + target
        if target > 0 and equity >= threshold and not s['profit_exit_pending']:
            s['profit_exit_pending'] = True
            self.event(t, '', 'run_status', 'profit_target_exit')
        if s['profit_exit_pending']:
            for mint, p in s['positions'].items():
                if not p.get('exit_reason'):
                    p.update(exit_reason='portfolio_profit_target', exit_signal_at=t,
                             exit_ready_at=t+c['exit_latency_seconds'], status='exit_pending')
                    self.event(t, mint, 'signal_exit', 'portfolio_profit_target')
            if not s['positions']:
                s['profit_exit_pending'] = False
                if s['cash_usd'] >= threshold:
                    s['run_result'] = 'profit_target'
                    self.event(t, '', 'run_status', 'profit_target')
                else:
                    self.event(t, '', 'run_status', 'profit_target_missed_after_fills')
        if (c.get('stop_when_unfunded', 0) and not s['positions'] and
                s['cash_usd'] < c['position_usd'] and not s['run_result']):
            s['run_result'] = 'bankroll_depleted'
            self.event(t, '', 'run_status', 'bankroll_depleted')

    def close(self):
        self.db.close()

    def watch_priority(self):
        with self.lock:
            return list(self.state['positions']) + list(self.state['pending_entries'])

    def watch_blocks(self):
        """Do not spend watch slots on exhausted tokens or tokens in cooldown."""
        with self.lock:
            result = {}
            for mint, count in self.state['entry_attempts_by_mint'].items():
                if count >= self.config['max_entry_attempts_per_mint']:
                    result[mint] = None  # blocked for this entire experiment
                elif mint in self.state['last_flat_at']:
                    result[mint] = self.state['last_flat_at'][mint] + self.config['reentry_cooldown_seconds']
            return result

    def _reentry_reason(self, mint, t):
        if self.state['entry_attempts_by_mint'].get(mint, 0) >= self.config['max_entry_attempts_per_mint']:
            return 'entry_attempt_limit'
        last = self.state['last_flat_at'].get(mint)
        if last is not None and t < last + self.config['reentry_cooldown_seconds']:
            return 'reentry_cooldown'
        return None

    def _buy_rate_limited(self, t, reserve_pending=False):
        """Rolling 60-second attempt budget; pending signals reserve available slots."""
        limit = self.config.get('max_buy_attempts_per_minute', 0)
        if not limit:
            return False
        recent = sum(at > t - 60 for at in self.state['recent_buy_attempts'])
        pending = len(self.state['pending_entries']) if reserve_pending else 0
        return recent + pending >= limit

    def event(self, t, mint, action, reason, data=None):
        self.db.execute('INSERT INTO decisions (at,mint,action,reason,data) VALUES (?,?,?,?,?)',
                        (t, mint, action, reason, canonical(data or {})))

    def _equity(self, t):
        total = self.state['cash_usd']
        impaired = 0
        for mint, p in self.state['positions'].items():
            s = self.state['snapshots'].get(mint)
            reason = usable(s, t, self.config)
            q, reason = (None, reason) if reason else sell_quote(s, p, self.config)
            p['mark_reason'] = reason
            p['mark_at'] = t
            p['mark_value_usd'] = q['proceeds_usd'] if q else 0
            p['unrealized_pnl_usd'] = p['mark_value_usd'] - p['cost_usd']
            impaired += q is None
            total += p['mark_value_usd']
        return total, impaired

    def _risk(self, t):
        s, c = self.state, self.config
        eq, impaired = self._equity(t)
        day = datetime.fromtimestamp(t, timezone.utc).strftime('%Y-%m-%d')
        if s['day'] != day:
            s['day'], s['day_start_equity'], s['day_halted'] = day, eq, False
        s['peak_equity'] = max(s['peak_equity'], eq)
        dd = 1 - eq / s['peak_equity']
        s['max_drawdown_fraction'] = max(s['max_drawdown_fraction'], dd)
        if c['daily_loss_halt_fraction'] > 0 and eq <= s['day_start_equity'] - c['virtual_balance_usd'] * c['daily_loss_halt_fraction']:
            s['day_halted'] = True
        if c['drawdown_halt_fraction'] > 0 and dd >= c['drawdown_halt_fraction']:
            s['drawdown_halted'] = True
        self._run_policy(t, eq)
        if self._entry_block_reason():
            for mint in list(s['pending_entries']):
                reason = self._entry_block_reason()
                self.event(t, mint, 'cancel_entry', 'portfolio_loss_halt' if reason in ('daily_loss','drawdown') else reason)
                del s['pending_entries'][mint]
        return eq, impaired

    def _attempt_failed(self, t, mint, side, snapshot):
        s, c = self.state, self.config
        s['attempts'] += 1
        key = f"{c['random_seed']}:{s['attempts']}:{mint}:{side}"
        uniform = int(hashlib.sha256(key.encode()).hexdigest()[:16], 16) / 2**64
        if uniform >= c['failed_attempt_probability']:
            return False
        fee = c['network_fee_sol'] * snapshot['quote_usd']
        s['cash_usd'] -= fee
        s['failed_fees_usd'] += fee
        s['failed_attempts'] += 1
        self.event(t, mint, 'failed_' + side, 'modeled_execution_failure', {'fee_usd': fee})
        return True

    def _exits(self, t, fresh_mints):
        c, state = self.config, self.state
        for mint, p in list(state['positions'].items()):
            snap = state['snapshots'].get(mint)
            reason = usable(snap, t, c)
            quote, reason = (None, reason) if reason else sell_quote(snap, p, c)
            if not p.get('exit_reason'):
                trigger = None
                if t - p['entry_at'] >= c['max_hold_seconds']:
                    trigger = 'time_limit'
                elif not quote:
                    trigger = 'unavailable_exit_quote'
                else:
                    ret = quote['proceeds_usd'] / p['cost_usd'] - 1
                    if ret >= c['take_profit_fraction']:
                        trigger = 'take_profit'
                    elif ret <= c['stop_loss_fraction']:
                        trigger = 'stop_loss'
                if trigger:
                    p['exit_reason'], p['exit_signal_at'] = trigger, t
                    p['exit_ready_at'] = t + c['exit_latency_seconds']
                    self.event(t, mint, 'signal_exit', trigger)
            if not p.get('exit_reason'):
                continue
            p['status'] = 'exit_pending'
            if not quote:
                if p.get('last_exit_issue') != reason:
                    self.event(t, mint, 'exit_unfilled', reason)
                p['last_exit_issue'] = reason
                continue
            # Always require a newly fetched observation after the scheduled latency.
            if (t < p['exit_ready_at'] or mint not in fresh_mints or
                    snap['observed_at'] < p['exit_ready_at']):
                continue
            if self._attempt_failed(t, mint, 'exit', snap):
                p['exit_ready_at'] = t + c['poll_seconds']
                continue
            state['cash_usd'] += quote['proceeds_usd']
            p.update(quote)
            p.update(status='closed', exit_at=t,
                     pnl_usd=quote['proceeds_usd'] - p['cost_usd'],
                     return_fraction=quote['proceeds_usd'] / p['cost_usd'] - 1,
                     hold_seconds=t - p['entry_at'],
                     exit_delay_seconds=t - p['exit_signal_at'])
            p['fees_usd'] = p['entry_fees_usd'] + p['exit_fees_usd']
            self.db.execute('INSERT INTO trades(mint,data) VALUES (?,?)', (mint, canonical(p)))
            self.event(t, mint, 'fill_exit', p['exit_reason'], quote)
            state['last_flat_at'][mint] = t
            del state['positions'][mint]

    def _entry_quote(self, snapshot, t):
        c = self.config
        reason = usable(snapshot, t, c)
        if reason:
            return None, reason
        if not c['market_cap_min_usd'] <= snapshot['market_cap_usd'] <= c['market_cap_max_usd']:
            return None, 'outside_entry_market_cap'
        q, reason = buy_quote(snapshot, c['position_usd'], c)
        if not q:
            return None, reason
        exit_q, reason = sell_quote(snapshot, q, c)
        if not exit_q:
            return None, reason
        if 1 - exit_q['proceeds_usd'] / q['cost_usd'] > c['max_roundtrip_cost_fraction']:
            return None, 'roundtrip_cost_too_high'
        return q, None

    def _entries(self, t, fresh_mints):
        state, c = self.state, self.config
        for mint, order in list(state['pending_entries'].items()):
            if t > order['expires_at']:
                self.event(t, mint, 'cancel_entry', 'entry_expired')
                del state['pending_entries'][mint]
                continue
            snap = state['snapshots'].get(mint)
            if (t < order['ready_at'] or mint not in fresh_mints or
                    snap['observed_at'] < order['ready_at']):
                continue
            q, reason = self._entry_quote(snap, t)
            if not q:
                self.event(t, mint, 'cancel_entry', reason)
                del state['pending_entries'][mint]
                continue
            if state['cash_usd'] < c['position_usd']:
                self.event(t, mint, 'cancel_entry', 'insufficient_virtual_cash')
                del state['pending_entries'][mint]
                continue
            if self._buy_rate_limited(t):
                continue  # Recheck at execution; delayed signals cannot produce a burst.
            # Persist attempt limits before either success or failure is recorded.
            state['recent_buy_attempts'].append(t)
            state['entry_attempts_by_mint'][mint] = state['entry_attempts_by_mint'].get(mint, 0) + 1
            if self._attempt_failed(t, mint, 'entry', snap):
                del state['pending_entries'][mint]
                state['traded_mints'].append(mint)
                state['last_flat_at'][mint] = t
                continue
            q.update(mint=mint, symbol=snap.get('symbol', mint[:8]), status='open', entry_at=t,
                     signal_at=order['signal_at'], entry_delay_seconds=t - order['signal_at'],
                     entry_market_cap_usd=snap['market_cap_usd'], signal=order['features'],
                     entry_snapshot=snap)
            state['cash_usd'] -= q['cost_usd']
            state['positions'][mint] = q
            state['traded_mints'].append(mint)
            self.event(t, mint, 'fill_entry', 'snapshot_momentum', q)
            del state['pending_entries'][mint]
            self._risk(t)
            if self._entry_block_reason():
                break

    def _features(self, mint, t):
        c, h = self.config, self.state['histories'].get(mint, [])
        if len(h) < c['min_samples'] or t - h[0]['t'] < c['observation_seconds']:
            return None, 'warming_up'
        old = [x for x in h if x['t'] <= t - c['observation_seconds']]
        short = [x for x in h if x['t'] <= t - c['short_window_seconds']]
        if not old or not short:
            return None, 'warming_up'
        a, b, end = old[-1], short[-1], h[-1]
        return {'long_momentum': end['price'] / a['price'] - 1,
                'short_momentum': end['price'] / b['price'] - 1,
                'long_window_seconds': c['observation_seconds'],
                'short_window_seconds': c['short_window_seconds'],
                'reserve_growth_usd': (end['real_y'] - b['real_y']) * end['fx'],
                'samples': len(h)}, None

    def _signals(self, t, fresh_mints):
        c, state = self.config, self.state
        for mint in sorted(fresh_mints):
            snap = state['snapshots'][mint]
            reason = usable(snap, t, c)
            if reason:
                state['histories'].pop(mint, None)
            else:
                h = state['histories'].setdefault(mint, [])
                if h and t - h[-1]['t'] > c['max_sample_gap_seconds']:
                    h.clear()
                h.append({'t': t, 'price': snap['y'] / snap['x'],
                          'real_y': snap['real_y'], 'fx': snap['quote_usd']})
                state['histories'][mint] = [x for x in h if x['t'] >=
                    t - c['observation_seconds'] - c['max_sample_gap_seconds']]
            features = None
            if not reason:
                _, reason = self._entry_quote(snap, t)
            if not reason:
                features, reason = self._features(mint, t)
            if not reason and not (c['min_momentum_fraction'] <= features['long_momentum']
                                    <= c['max_momentum_fraction']):
                reason = 'long_momentum_filter'
            if not reason and features['short_momentum'] < c['min_short_momentum_fraction']:
                reason = 'short_momentum_filter'
            if not reason and features['reserve_growth_usd'] < c['min_reserve_growth_usd']:
                reason = 'reserve_growth_filter'
            if mint in state['positions'] or mint in state['pending_entries']:
                reason = 'position_or_order_exists'
            elif self._reentry_reason(mint, t):
                reason = self._reentry_reason(mint, t)
            elif self._entry_block_reason():
                block = self._entry_block_reason()
                reason = 'portfolio_loss_halt' if block in ('daily_loss','drawdown') else block
            count = len(state['positions']) + len(state['pending_entries'])
            exposure = sum(p['cost_usd'] for p in state['positions'].values()) + \
                len(state['pending_entries']) * c['position_usd']
            free = state['cash_usd'] - len(state['pending_entries']) * c['position_usd']
            if not reason and (count >= c['max_positions'] or exposure + c['position_usd'] > c['max_exposure_usd']):
                reason = 'exposure_limit'
            if not reason and free < c['position_usd']:
                reason = 'insufficient_virtual_cash'
            if not reason and self._buy_rate_limited(t, reserve_pending=True):
                reason = 'buy_rate_limit'
            state['candidates'][mint] = {
                'mint': mint, 'symbol': snap.get('symbol', mint[:8]), 'at': t,
                'market_cap_usd': snap.get('market_cap_usd'),
                'reason': reason or 'entry_scheduled', 'features': features,
                'real_liquidity_usd': snap.get('real_y', 0) * snap.get('quote_usd', 0)}
            self.event(t, mint, 'skip' if reason else 'signal_entry', reason or 'snapshot_momentum', features)
            if not reason:
                state['pending_entries'][mint] = {'signal_at': t,
                    'ready_at': t + c['entry_latency_seconds'],
                    'expires_at': t + c['order_expiry_seconds'], 'features': features}

    def step(self, batch):
        """Input contains only observations available at batch.at; no future prices."""
        if self.view_only:
            raise ValueError('This ledger is open for viewing only')
        t = batch['at']
        payload = canonical(batch)
        ident = hashlib.sha256(payload.encode()).hexdigest()
        with self.lock:
            if self.db.execute('SELECT 1 FROM batches WHERE id=?', (ident,)).fetchone():
                return False
            if self.state['last_at'] is not None and t <= self.state['last_at']:
                raise ValueError('Input batches must arrive in strictly increasing time order')
            before = copy.deepcopy(self.state)
            try:
                with self.db:
                    self.db.execute('INSERT INTO batches VALUES (?,?,?)',
                                    (ident, t, zlib.compress(payload.encode())))
                    state = self.state
                    if state['started_at'] is None:
                        state['started_at'] = t
                    if state['last_at'] is not None and not state['run_result']:
                        cost = (t - state['last_at']) / 86400 * self.config['daily_operating_cost_usd']
                        state['cash_usd'] -= cost
                        state['operating_cost_usd'] += cost
                    state['last_at'] = t
                    state['recent_buy_attempts'] = [at for at in state['recent_buy_attempts'] if at > t - 60]
                    state['last_issues'] = batch.get('issues', [])
                    state['last_source_status'] = batch.get('status', 'ok')
                    state['discovered_count'] = batch.get('discovered_count', state['discovered_count'])
                    fresh = set()
                    for snap in batch['snapshots']:
                        mint = snap['mint']
                        if mint in fresh:
                            continue
                        fresh.add(mint)
                        snap = copy.deepcopy(snap)
                        previous = state['snapshots'].get(mint)
                        if previous and snap.get('source_at', 0) < previous.get('source_at', 0):
                            snap['validation_error'] = 'source_time_regressed'
                        state['snapshots'][mint] = snap
                        state['observations'] += 1
                    self._risk(t)
                    self._exits(t, fresh)
                    self._risk(t)
                    self._entries(t, fresh)
                    self._risk(t)
                    self._signals(t, fresh)
                    eq, impaired = self._risk(t)
                    state['batches'] += 1
                    # Bound the working state. Raw observations remain in the database.
                    protected = set(state['positions']) | set(state['pending_entries'])
                    for mint, snap in list(state['snapshots'].items()):
                        if mint not in protected and t - snap['observed_at'] > self.config['watch_retention_seconds']:
                            for key in ('snapshots', 'histories', 'candidates'):
                                state[key].pop(mint, None)
                    self.db.execute('INSERT INTO equity(at,value,cash,impaired) VALUES (?,?,?,?)',
                                    (t, eq, state['cash_usd'], impaired))
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('state', canonical(state)))
            except Exception:
                self.state = before
                raise
        return True

    def trades(self):
        with self.lock:
            return [dict(id=r['id'], **json.loads(r['data'])) for r in self.db.execute('SELECT * FROM trades ORDER BY id')]

    def report(self):
        with self.lock:
            s, c = self.state, self.config
            trades = self.trades()
            closed = sum(x['pnl_usd'] for x in trades)
            wins = [x['pnl_usd'] for x in trades if x['pnl_usd'] > 0]
            losses = [x['pnl_usd'] for x in trades if x['pnl_usd'] < 0]
            fees = sum(x['fees_usd'] for x in trades) + sum(p['entry_fees_usd'] for p in s['positions'].values())
            # Mark copy for current server time separately in UI; engine results remain deterministic.
            positions = list(copy.deepcopy(s['positions']).values())
            equity = s['cash_usd'] + sum(p.get('mark_value_usd', 0) for p in positions)
            bands = []
            for low, high in [(10000, 20000), (20000, 35000), (35000, 50000)]:
                rows = [x for x in trades if low <= x['entry_market_cap_usd'] and
                        (x['entry_market_cap_usd'] < high or high == 50000 and x['entry_market_cap_usd'] == high)]
                bands.append({'range': f'${low/1000:g}–{high/1000:g}k', 'trades': len(rows),
                              'pnl_usd': sum(x['pnl_usd'] for x in rows)})
            curve = [dict(r) for r in self.db.execute('SELECT at,value,cash,impaired FROM equity ORDER BY id DESC LIMIT 2000')][::-1]
            return {
                'mode': 'PAPER ONLY', 'source': self.source, 'config': c,
                'config_hash': hashlib.sha256(canonical(c).encode()).hexdigest()[:12],
                'implementation_hash': self.implementation_hash,
                'started_at': s['started_at'], 'last_at': s['last_at'],
                'source_status': s['last_source_status'], 'issues': s['last_issues'],
                'cash_usd': s['cash_usd'], 'equity_usd': equity,
                'total_pnl_usd': equity - c['virtual_balance_usd'],
                'closed_trade_pnl_usd': closed,
                'realized_net_pnl_usd': closed - s['failed_fees_usd'] - s['operating_cost_usd'],
                'unrealized_pnl_usd': sum(p.get('unrealized_pnl_usd', 0) for p in positions),
                'modeled_fees_usd': fees + s['failed_fees_usd'],
                'operating_cost_usd': s['operating_cost_usd'],
                'failed_attempts': s['failed_attempts'], 'failed_fees_usd': s['failed_fees_usd'],
                'closed_trades': len(trades), 'win_rate': len(wins) / len(trades) if trades else None,
                'profit_factor': sum(wins) / -sum(losses) if losses else None,
                'mean_trade_pnl_usd': closed / len(trades) if trades else None,
                'without_largest_winner_usd': closed - max(wins, default=0) - s['failed_fees_usd'] - s['operating_cost_usd'],
                'max_drawdown_fraction': s['max_drawdown_fraction'],
                'halted': bool(self._entry_block_reason()),
                'halt_reason': self._entry_block_reason(),
                'run_result': s['run_result'], 'profit_exit_pending': s['profit_exit_pending'],
                'observations': s['observations'], 'batches': s['batches'],
                'discovered_count': s['discovered_count'], 'positions': positions,
                'pending_entries': copy.deepcopy(s['pending_entries']),
                'candidates': sorted(copy.deepcopy(list(s['candidates'].values())), key=lambda x: -x['at'])[:100],
                'trades': trades[-500:][::-1], 'bands': bands, 'equity_curve': curve,
                'decisions': [dict(r) for r in self.db.execute('SELECT at,mint,action,reason FROM decisions ORDER BY id DESC LIMIT 60')]
            }

    def export_csv(self):
        rows = self.trades()
        fields = ['id', 'mint', 'symbol', 'signal_at', 'entry_at', 'exit_at',
                  'entry_market_cap_usd', 'quantity', 'entry_price_usd', 'exit_price_usd',
                  'entry_price_sol', 'exit_price_sol', 'entry_quote_usd', 'exit_quote_usd',
                  'cost_usd', 'proceeds_usd', 'pnl_usd', 'return_fraction', 'fees_usd',
                  'hold_seconds', 'exit_reason', 'entry_delay_seconds', 'exit_delay_seconds']
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=fields, extrasaction='ignore')
        writer.writeheader()
        for row in rows:
            # A token symbol is untrusted spreadsheet content.
            row = dict(row)
            for k in ('symbol', 'mint'):
                if row[k].startswith(('=', '+', '-', '@', '\t', '\r', '\n')):
                    row[k] = "'" + row[k]
            writer.writerow(row)
        return out.getvalue()

    def export_recording(self, path):
        with self.lock, open(path, 'w') as f:
            f.write(canonical({'type': 'header', 'source': self.source, 'config': self.config,
                               'implementation_hash': self.implementation_hash}) + '\n')
            for row in self.db.execute('SELECT payload FROM batches ORDER BY at'):
                f.write(zlib.decompress(row[0]).decode() + '\n')
