"""Prospective paper entries near $10k with persistent, delayed partial exits.

Own ledger and recorded inputs. No signing, wallet or transaction submission.
Curve quotes use the remaining-position reserve offset; after graduation, AMM
quotes use published indexed reserves without assuming an executable migration.
"""
import copy
import csv
import fcntl
import hashlib
import io
import json
import math
from pathlib import Path
import sqlite3
import threading
import zlib

from .engine import canonical
from .model import usable


def validate_config(config):
    c = copy.deepcopy(config)
    if c.get('mode') != 'paper_only' or c.get('version') != 'early-ladder-v1':
        raise ValueError('Only the paper ladder is supported')
    for key, value in c.items():
        if key in ('mode', 'version', 'targets'):
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError('Invalid ladder setting: ' + key)
        if key != 'stop_loss_fraction' and value <= 0:
            if key not in ('failed_attempt_probability', 'entry_rent_usd') or value < 0:
                raise ValueError('Ladder setting must be positive: ' + key)
    if not (c['order_usd'] <= c['budget_usd'] and 0 < c['entry_cap_min'] < c['entry_cap_max']):
        raise ValueError('Invalid ladder budget or entry range')
    for key in ('max_positions', 'minimum_samples', 'minimum_price_changes'):
        if not isinstance(c[key], int) or c[key] < 1:
            raise ValueError('Invalid ladder count: ' + key)
    for key in ('fee_fraction', 'adverse_fraction', 'maximum_peak_drawdown',
                'max_entry_reserve_fraction', 'max_roundtrip_loss_fraction',
                'trailing_drawdown_fraction', 'failed_attempt_probability'):
        if not 0 <= c[key] < 1:
            raise ValueError('Invalid ladder fraction: ' + key)
    if not -1 < c['stop_loss_fraction'] < 0:
        raise ValueError('Invalid ladder stop')
    if not c['fill_delay_seconds'] < c['entry_expiry_seconds']:
        raise ValueError('Entry expiry must follow fill delay')
    last, total = c['entry_cap_max'], 0
    if not c['targets']:
        raise ValueError('Ladder targets required')
    for step in c['targets']:
        cap, fraction = step['market_cap_usd'], step['initial_fraction']
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (cap, fraction)):
            raise ValueError('Invalid ladder target')
        if cap <= last or not 0 < fraction <= 1:
            raise ValueError('Ladder targets must increase, with positive fractions')
        last, total = cap, total + fraction
    if total > 1 + 1e-9:
        raise ValueError('Cannot sell more than the original position')
    return c


class LadderRunner:
    def __init__(self, path, config, ownership_config, source='public_index_live'):
        self.cfg = validate_config(config)
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.file_lock = open(str(self.path) + '.lock', 'a')
        try:
            fcntl.flock(self.file_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.file_lock.close()
            raise ValueError('Ladder ledger already has a writer') from None
        self.policy_hash = hashlib.sha256(canonical(ownership_config).encode()).hexdigest()
        code = b''.join((Path(__file__).parent / n).read_bytes() for n in ('ladder.py', 'model.py'))
        self.contract = {'config': self.cfg, 'source': source, 'ownership_config': ownership_config,
                         'code_hash': hashlib.sha256(code).hexdigest()}
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS inputs(id TEXT PRIMARY KEY,at REAL,payload BLOB);
            CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at REAL,mint TEXT,action TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS sales(id INTEGER PRIMARY KEY,mint TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS trades(id INTEGER PRIMARY KEY,mint TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS equity(id INTEGER PRIMARY KEY,at REAL,value REAL,cash REAL);
        ''')
        old = self.db.execute("SELECT value FROM meta WHERE key='contract'").fetchone()
        if old and old[0] != canonical(self.contract):
            self.close()
            raise ValueError('Ladder rules changed; preserve this experiment and use a new ledger')
        row = self.db.execute("SELECT value FROM meta WHERE key='state'").fetchone()
        self.state = json.loads(row[0]) if row else {
            'cash_usd': self.cfg['budget_usd'], 'positions': {}, 'pending': {}, 'attempted': [],
            'histories': {}, 'candidates': {}, 'started_at': None, 'last_at': None,
            'attempts': 0, 'failed_fees_usd': 0, 'observations': 0,
            'realized_pnl_usd': 0, 'fees_usd': 0}
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('contract', canonical(self.contract)))

    def close(self):
        self.db.close()
        self.file_lock.close()

    def held_mints(self):
        with self.lock:
            return set(self.state['positions']) | set(self.state['pending'])

    def event(self, t, mint, action, data):
        self.db.execute('INSERT INTO events(at,mint,action,data) VALUES (?,?,?,?)',
                        (t, mint, action, canonical(data)))

    def market(self, q, t):
        """Validate current quote; AMM values remain explicitly indexed estimates."""
        if not q:
            return None
        c = self.cfg
        if q.get('route') == 'pump_bonding_curve_sol':
            limits = {'max_observation_age_seconds': c['quote_max_age_seconds'],
                      'max_source_age_seconds': c['quote_max_age_seconds']}
            if usable(q, t, limits) or not 0 <= t-q['observed_at'] <= c['quote_max_age_seconds']:
                return None
            return {'route': 'curve', 'pool': q.get('curve_address') or q['mint'],
                'base': q['x'], 'reserve': q['y'] * q['quote_usd'],
                'real': q['real_y'] * q['quote_usd'], 'real_tokens': q['real_x'],
                'price': q['y'] / q['x'] * q['quote_usd'], 'cap': q['market_cap_usd'],
                'supply': q['supply'], 'decimals': q['decimals'], 'observed_at': q['observed_at']}
        fields = ('observed_at', 'price_usd', 'market_cap_usd', 'base_reserve', 'liquidity_usd')
        if any(isinstance(q.get(k), bool) or not isinstance(q.get(k), (int, float)) or
               not math.isfinite(q[k]) or q[k] <= 0 for k in fields):
            return None
        if (q.get('dex_id') != 'pumpswap' or not q.get('pair_address') or
                q.get('valuation_basis') != 'fdv' or
                not 0 <= t-q['observed_at'] <= c['quote_max_age_seconds'] or
                q['liquidity_usd'] < c['min_amm_liquidity_usd']):
            return None
        return {'route': 'amm', 'pool': q['pair_address'], 'base': q['base_reserve'],
            'reserve': q['base_reserve'] * q['price_usd'],
            'real': q['base_reserve'] * q['price_usd'], 'price': q['price_usd'],
            'cap': q['market_cap_usd'], 'observed_at': q['observed_at']}

    def sell_quote(self, market, p, quantity=None):
        if not market:
            return None
        m, c = market, self.cfg
        qty = p['quantity'] if quantity is None else quantity
        if not 0 < qty <= p['quantity'] * (1 + 1e-9):
            return None
        # Once an AMM is accepted, an obsolete curve or another pool cannot price an exit.
        if p.get('amm_pool') and (m['route'] != 'amm' or m['pool'] != p['amm_pool']):
            return None
        if m['route'] == 'curve':
            if m['pool'] != p['entry_pool'] or p['quantity'] >= m['base'] or m['real'] <= 0:
                return None
            base = m['base'] - p['quantity']
            reserve = m['base'] * m['reserve'] / base
            real = m['real'] + reserve - m['reserve']
        else:
            if abs(m['cap']/m['price']/p['entry_supply']-1) > .10:
                return None
            base, reserve, real = m['base'], m['reserve'], m['real']
        gross = reserve * qty / (base + qty)
        if gross > real or qty / (base + qty) > .10:
            return None
        gross *= 1-c['adverse_fraction']
        fees = gross*c['fee_fraction'] + c['fixed_fee_usd']
        net = gross-fees
        if net <= 0:
            return None
        return {'quantity': qty, 'proceeds_usd': net, 'exit_price_usd': net/qty,
                'exit_fees_usd': fees, 'route': m['route'], 'pool': m['pool']}

    def buy_quote(self, m):
        c = self.cfg
        if not m or m['route'] != 'curve' or m['real'] < c['min_curve_liquidity_usd']:
            return None
        pool = (c['order_usd']-c['fixed_fee_usd']-c['entry_rent_usd'])/(1+c['fee_fraction'])
        if pool <= 0 or pool > m['real']*c['max_entry_reserve_fraction']:
            return None
        raw = m['base']*pool/(m['reserve']+pool)
        qty = math.floor(raw*(1-c['adverse_fraction'])*10**m['decimals'])/10**m['decimals']
        if qty <= 0 or raw >= m['real_tokens']:
            return None
        p = {'quantity': qty, 'initial_quantity': qty, 'entry_pool': m['pool'],
             'entry_supply': m['supply'],
             'cost_usd': c['order_usd'], 'remaining_cost_usd': c['order_usd'],
             'entry_price_usd': c['order_usd']/qty,
             'entry_fees_usd': pool*c['fee_fraction']+c['fixed_fee_usd']+c['entry_rent_usd']}
        sell = self.sell_quote(m, p)
        if not sell or 1-sell['proceeds_usd']/c['order_usd'] > c['max_roundtrip_loss_fraction']:
            return None
        return p

    def ownership_clear(self, mint, evidence, t):
        gate = evidence.get('ownership') or {}
        checked = gate.get('checked_at')
        return (gate.get('mint') == mint and gate.get('status') == 'clear' and
                gate.get('policy_hash') == self.policy_hash and
                isinstance(checked, (int, float)) and math.isfinite(checked) and
                0 <= t-checked <= self.cfg['ownership_ttl_seconds'])

    def entry_reason(self, mint, m, evidence, t):
        c, s = self.cfg, self.state
        if not m or m['route'] != 'curve':
            return 'No fresh supported bonding-curve quote'
        if not c['entry_cap_min'] <= m['cap'] <= c['entry_cap_max']:
            return 'Outside the $10k–$12.5k entry band'
        if not self.ownership_clear(mint, evidence, t):
            return 'Ownership flagged or unverified'
        history = s['histories'].get(mint, [])
        if len(history) < c['minimum_samples'] or history[-1]['at']-history[0]['at'] < c['observation_seconds']:
            return 'Building a 60-second observation window'
        changes = sum(abs(b['price']/a['price']-1) > 1e-8 for a, b in zip(history, history[1:]))
        if changes < c['minimum_price_changes']:
            return 'Not enough distinct reserve-implied price changes'
        if history[-1]['price']/history[0]['price']-1 < c['minimum_growth_fraction']:
            return 'No positive reserve-implied price trend yet'
        # Reserve changes use a fixed entry-window FX basis, so SOL/USD changes alone cannot qualify.
        if history[-1]['real_sol']-history[0]['real_sol'] < c['minimum_reserve_growth_usd']/history[-1]['fx']:
            return 'Real SOL reserve growth is insufficient'
        peak = max(evidence.get('peak_cap') or m['cap'], m['cap'])
        if 1-m['cap']/peak > c['maximum_peak_drawdown']:
            return 'Too far below its recorded peak'
        if not self.buy_quote(m):
            return 'Liquidity or modeled entry costs fail the screen'
        return None

    def failed(self, t, mint, side):
        s, c = self.state, self.cfg
        s['attempts'] += 1
        u = int(hashlib.sha256(f"ladder:{s['attempts']}:{mint}:{side}".encode()).hexdigest()[:16], 16)/2**64
        if u >= c['failed_attempt_probability']:
            return False
        # No imaginary borrowing to pay a failed attempt.
        fee = min(max(0, s['cash_usd']), c['fixed_fee_usd'])
        s['cash_usd'] -= fee
        s['failed_fees_usd'] += fee
        s['fees_usd'] += fee
        self.event(t, mint, 'failed_'+side, {'fee_usd': fee})
        return True

    def schedule_exit(self, p, t, reason, stages=None):
        p['exit_order'] = {'at': t, 'reason': reason, 'stages': stages or []}
        p['status'] = 'partial_exit_pending' if stages else 'exit_pending'
        self.event(t, p['mint'], 'signal_exit', p['exit_order'])

    def exit_position(self, mint, p, m, evidence, t):
        s, c = self.state, self.cfg
        if evidence.get('curve_complete') is True and not p.get('migration_seen'):
            p['migration_seen'] = True
            self.event(t, mint, 'curve_completed', {'observed_at': t})
        if m and m['route'] == 'amm' and not p.get('migration_seen'):
            m = None
        sell = self.sell_quote(m, p)
        if sell:
            if m['route'] == 'amm' and not p.get('amm_pool'):
                p['amm_pool'] = m['pool']
                self.event(t, mint, 'route_transition', {'pool': m['pool'], 'source': 'indexed_pumpswap'})
            p['last_usable_at'] = m['observed_at']
            p['last_cap'] = m['cap']
            p['peak_price_usd'] = max(p['peak_price_usd'], m['price'])
        p['mark_value_usd'] = sell['proceeds_usd'] if sell else 0
        p['mark_reason'] = None if sell else 'Awaiting a fresh supported quote; unsold tokens remain open'
        reason = None
        risk = evidence.get('ownership') or {}
        if risk.get('mint') == mint and risk.get('status') == 'blocked':
            reason = 'ownership_flag'
        elif t-p['entry_at'] >= c['max_hold_seconds']:
            reason = 'ten_day_limit'
        elif not sell and t-p['last_usable_at'] >= c['quote_gap_exit_seconds']:
            reason = 'quote_gap'
        elif sell and sell['proceeds_usd']/p['remaining_cost_usd']-1 <= c['stop_loss_fraction']:
            reason = 'stop_loss'
        elif sell and p['sold_stages'] and m['price'] <= p['peak_price_usd']*(1-c['trailing_drawdown_fraction']):
            reason = 'trailing_exit'
        order = p.get('exit_order')
        if reason and (not order or order['stages']):
            self.schedule_exit(p, t, reason)
            order = p['exit_order']
        if order and sell and m['observed_at'] > order['at'] and t-order['at'] >= c['fill_delay_seconds']:
            stages = [i for i in order['stages'] if m['cap'] >= c['targets'][i]['market_cap_usd']]
            if order['stages'] and not stages:
                self.event(t, mint, 'cancel_exit', {'reason': 'Target no longer reached on the fill quote'})
                p.pop('exit_order'); p['status'] = 'open'
            else:
                fraction = sum(c['targets'][i]['initial_fraction'] for i in stages)
                qty = min(p['quantity'], p['initial_quantity']*fraction) if stages else p['quantity']
                sale = self.sell_quote(m, p, qty)
                if sale and not self.failed(t, mint, 'exit'):
                    cost = p['remaining_cost_usd']*qty/p['quantity']
                    sale.update(mint=mint, symbol=p['symbol'], entry_at=p['entry_at'], exit_at=t,
                        entry_price_usd=p['entry_price_usd'], entry_cap=p['entry_cap'],
                        exit_cap=m['cap'], cost_usd=cost, pnl_usd=sale['proceeds_usd']-cost,
                        initial_fraction=qty/p['initial_quantity'], stages=stages, exit_reason=order['reason'])
                    self.db.execute('INSERT INTO sales(mint,data) VALUES (?,?)', (mint, canonical(sale)))
                    s['cash_usd'] += sale['proceeds_usd']
                    s['realized_pnl_usd'] += sale['pnl_usd']
                    s['fees_usd'] += sale['exit_fees_usd']
                    p['quantity'] = max(0, p['quantity']-qty)
                    p['remaining_cost_usd'] = max(0, p['remaining_cost_usd']-cost)
                    p['proceeds_usd'] += sale['proceeds_usd']
                    p['realized_pnl_usd'] += sale['pnl_usd']
                    p['sold_stages'] = sorted(set(p['sold_stages']) | set(stages))
                    p.pop('exit_order'); p['status'] = 'open'
                    self.event(t, mint, 'fill_partial_exit' if p['quantity'] > p['initial_quantity']*1e-9 else 'fill_exit', sale)
                    if p['quantity'] <= p['initial_quantity']*1e-9:
                        trade = dict(p, exit_at=t, exit_reason=order['reason'], pnl_usd=p['proceeds_usd']-p['cost_usd'])
                        self.db.execute('INSERT INTO trades(mint,data) VALUES (?,?)', (mint, canonical(trade)))
                        del s['positions'][mint]
                        return
                    residual = self.sell_quote(m, p)
                    p['mark_value_usd'] = residual['proceeds_usd'] if residual else 0
                    p['mark_reason'] = None if residual else 'Residual position has no usable exit quote'
        if sell and not p.get('exit_order'):
            stages = [i for i, step in enumerate(c['targets'])
                      if i not in p['sold_stages'] and m['cap'] >= step['market_cap_usd']]
            if stages:
                self.schedule_exit(p, t, 'market_cap_target', stages)

    def step(self, batch):
        """Atomic recorded-input transition; imports never call this method."""
        with self.lock:
            t = batch['at']
            if self.state['last_at'] is not None and t <= self.state['last_at']:
                return
            payload = canonical(batch)
            before = copy.deepcopy(self.state)
            try:
                with self.db:
                    self._step(batch)
                    s = self.state
                    equity = s['cash_usd']+sum(p['mark_value_usd'] for p in s['positions'].values())
                    unrealized = sum(p['mark_value_usd']-p['remaining_cost_usd'] for p in s['positions'].values())
                    assert abs(equity-self.cfg['budget_usd']-(s['realized_pnl_usd']-s['failed_fees_usd']+unrealized)) < 1e-6
                    self.db.execute('INSERT INTO inputs VALUES (?,?,?)',
                        (hashlib.sha256(payload.encode()).hexdigest(), t, zlib.compress(payload.encode())))
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('state', canonical(s)))
                    self.db.execute('INSERT INTO equity(at,value,cash) VALUES (?,?,?)', (t, equity, s['cash_usd']))
            except Exception:
                self.state = before
                raise

    def _step(self, batch):
        s, c, t = self.state, self.cfg, batch['at']
        if s['started_at'] is None:
            s['started_at'] = t
        s['last_at'] = t
        quotes = {q['mint']: q for q in batch['quotes']}
        markets = {mint: self.market(q, t) for mint, q in quotes.items()}
        evidence = batch['evidence']
        for mint, q in quotes.items():
            m = markets[mint]
            if not m or m['route'] != 'curve':
                continue
            history = s['histories'].setdefault(mint, [])
            if history and m['observed_at'] <= history[-1]['at']:
                continue
            if history and m['observed_at']-history[-1]['at'] > c['max_sample_gap_seconds']:
                history.clear()
            history.append({'at': m['observed_at'], 'price': q['y']/q['x'],
                            'real_sol': q['real_y'], 'fx': q['quote_usd']})
            while len(history) > 1 and history[1]['at'] <= m['observed_at']-c['observation_seconds']:
                history.pop(0)
            s['observations'] += 1
        s['histories'] = {mint: h for mint, h in s['histories'].items() if h and t-h[-1]['at'] <= 120}
        for mint, p in list(s['positions'].items()):
            self.exit_position(mint, p, markets.get(mint), evidence.get(mint, {}), t)
        for mint, order in list(s['pending'].items()):
            m = markets.get(mint)
            if t-order['at'] > c['entry_expiry_seconds']:
                del s['pending'][mint]
                self.event(t, mint, 'cancel_entry', {'reason': 'Entry quote expired'})
                continue
            if not m or m['observed_at'] <= order['at'] or t-order['at'] < c['fill_delay_seconds']:
                continue
            reason = self.entry_reason(mint, m, evidence.get(mint, {}), t)
            del s['pending'][mint]
            if reason or s['cash_usd'] < c['order_usd']:
                self.event(t, mint, 'cancel_entry', {'reason': reason or 'Insufficient virtual cash'})
                continue
            s['attempted'].append(mint)
            if self.failed(t, mint, 'entry'):
                continue
            p = self.buy_quote(m)
            s['cash_usd'] -= p['cost_usd']
            s['fees_usd'] += p['entry_fees_usd']
            p.update(mint=mint, symbol=quotes[mint]['symbol'], entry_at=t, entry_cap=m['cap'], last_cap=m['cap'],
                peak_price_usd=m['price'], last_usable_at=m['observed_at'], proceeds_usd=0,
                realized_pnl_usd=0, sold_stages=[], status='open', mark_reason=None)
            p['mark_value_usd'] = self.sell_quote(m, p)['proceeds_usd']
            s['positions'][mint] = p
            self.event(t, mint, 'fill_entry', p)
        s['candidates'] = {}
        for mint in sorted(quotes):
            m = markets.get(mint)
            if mint in self.held_mints() or mint in s['attempted']:
                continue
            reason = self.entry_reason(mint, m, evidence.get(mint, {}), t)
            if not reason and (len(self.held_mints()) >= c['max_positions'] or
                              s['cash_usd']-len(s['pending'])*c['order_usd'] < c['order_usd']):
                reason = 'Portfolio capacity or virtual cash reserved'
            if m and c['entry_cap_min'] <= m['cap'] <= c['entry_cap_max']:
                s['candidates'][mint] = {'mint': mint, 'symbol': quotes[mint]['symbol'], 'market_cap_usd': m['cap'],
                                         'reason': reason or 'Entry scheduled'}
            if not reason:
                s['pending'][mint] = {'at': t}
                self.event(t, mint, 'signal_entry', {'cap': m['cap'], 'reason': 'early_activity_and_ownership'})

    def report(self):
        with self.lock:
            s, c = self.state, self.cfg
            positions = copy.deepcopy(sorted(s['positions'].values(), key=lambda p: p['entry_at'], reverse=True))
            for p in positions:
                p['remaining_fraction'] = p['quantity']/p['initial_quantity']
                p['total_pnl_usd'] = p['proceeds_usd']+p['mark_value_usd']-p['cost_usd']
                p['next_target'] = next((step['market_cap_usd'] for i, step in enumerate(c['targets']) if i not in p['sold_stages']), None)
            equity = s['cash_usd']+sum(p['mark_value_usd'] for p in positions)
            sales = [dict(json.loads(r['data']), id=r['id']) for r in self.db.execute('SELECT id,data FROM sales ORDER BY id DESC LIMIT 50')]
            events = [dict(r, data=json.loads(r['data'])) for r in self.db.execute('SELECT * FROM events ORDER BY id DESC LIMIT 15')]
            return {'version': c['version'], 'initial_usd': c['budget_usd'], 'cash_usd': s['cash_usd'],
                'equity_usd': equity, 'pnl_usd': equity-c['budget_usd'],
                'realized_net_pnl_usd': s['realized_pnl_usd']-s['failed_fees_usd'],
                'unrealized_pnl_usd': sum(p['mark_value_usd']-p['remaining_cost_usd'] for p in positions),
                'positions': positions, 'trades': sales, 'sale_count': self.db.execute('SELECT count(*) FROM sales').fetchone()[0],
                'closed_count': self.db.execute('SELECT count(*) FROM trades').fetchone()[0],
                'pending_count': len(s['pending']), 'started_at': s['started_at'], 'last_at': s['last_at'],
                'failed_fees_usd': s['failed_fees_usd'], 'fees_usd': s['fees_usd'],
                'candidates': list(s['candidates'].values()), 'events': events, 'config': c,
                'model': 'Separate $100 forward paper test. Fractions refer to original token quantity. Signals require a later quote; target prices are never invented fills. Indexed curve / PumpSwap estimates, 2.5% fees + 1% adverse fill per side, $0.10 per attempt and $0.35 entry rent; 5% failed-attempt stress. Missing quotes mark unsold tokens at $0.'}

    def export_csv(self):
        with self.lock:
            fields = ('id', 'mint', 'symbol', 'entry_at', 'exit_at', 'entry_cap', 'exit_cap',
                      'entry_price_usd', 'exit_price_usd', 'quantity', 'initial_fraction',
                      'cost_usd', 'proceeds_usd', 'pnl_usd', 'exit_fees_usd', 'exit_reason', 'route', 'pool')
            out = io.StringIO()
            writer = csv.DictWriter(out, fields, extrasaction='ignore')
            writer.writeheader()
            for r in self.db.execute('SELECT id,data FROM sales ORDER BY id'):
                row=dict(json.loads(r['data']), id=r['id'])
                # Token symbols are public untrusted text, not spreadsheet formulas.
                for key,value in row.items():
                    if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):
                        row[key]="'"+value
                writer.writerow(row)
            return out.getvalue()

    def export_recording(self, path):
        with self.lock, open(path, 'w') as f:
            f.write(canonical(dict(self.contract, type='ladder_header'))+'\n')
            for r in self.db.execute('SELECT payload FROM inputs ORDER BY at'):
                f.write(zlib.decompress(r[0]).decode()+'\n')
