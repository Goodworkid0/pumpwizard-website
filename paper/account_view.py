"""One read-only account statement over funded strategy allocations.

Cash movements are reconstructed from actual recorded actions. Embedded fees
are reported once, capital is excluded from P&L, and partial exits retain cost.
"""
import copy
import csv
import io
import json
import math
from collections import defaultdict
from pathlib import Path
import sqlite3

from .dashboard import build_view, compact_curve, period_start, PERIODS

LABELS = {'scalp': 'Short term', 'ladder': 'Long hold', 'account': 'Account'}
FINANCIAL = {'fill_entry', 'fill_exit', 'fill_partial_exit', 'failed_entry', 'failed_exit'}


def csv_text(rows, fields):
    out = io.StringIO()
    writer = csv.DictWriter(out, fields, extrasaction='ignore'); writer.writeheader()
    for original in rows:
        row = dict(original)
        for key, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(('=', '+', '-', '@')):
                row[key] = "'"+value
        writer.writerow(row)
    return out.getvalue()


class Account:
    def __init__(self, scalp, ladder, path):
        self.scalp, self.ladder, self.path = scalp, ladder, Path(path)
        with sqlite3.connect(f'file:{self.path}?mode=ro', uri=True) as con:
            row = con.execute("SELECT value FROM meta WHERE key='migration'").fetchone()
            if not row:
                raise ValueError('Account migration has not completed')
            self.migration = json.loads(row[0])
            self.transfers = [dict(zip(('id', 'at', 'source', 'destination', 'amount'), r))
                              for r in con.execute('SELECT * FROM transfers ORDER BY at,id')]
        if not scalp.funding or not ladder.funding:
            raise ValueError('Both strategy allocations need their recorded funding history')

    def _member(self, name, engine):
        s = copy.deepcopy(engine.state)
        cfg = engine.base_config
        capital = cfg['virtual_balance_usd' if name == 'scalp' else 'budget_usd']
        started = s['started_at'] or self.migration['at']
        equity = [dict(r) for r in engine.db.execute('SELECT * FROM equity ORDER BY at,id')]
        closed = [dict(json.loads(r['data']), native_id=r['id']) for r in engine.db.execute('SELECT id,data FROM trades ORDER BY id')]
        symbols = {p['mint']: p['symbol'] for p in s['positions'].values()}
        symbols.update({p['mint']: p['symbol'] for p in closed})
        raw_events = []
        table = 'decisions' if name == 'scalp' else 'events'
        for row in engine.db.execute(f"SELECT * FROM {table} WHERE action!='skip' ORDER BY id"):
            event = dict(row); event['details'] = json.loads(event.pop('data'))
            event.setdefault('reason', event['details'].get('reason', event['details'].get('exit_reason', '')))
            if event['details'].get('symbol'):
                symbols[event['mint']] = event['details']['symbol']
            raw_events.append(event)
        by_exit = {(p['mint'], p['exit_at']): p for p in closed}
        sales = [dict(json.loads(r['data']), native_id=r['id']) for r in engine.db.execute('SELECT id,data FROM sales ORDER BY id')] if name == 'ladder' else closed
        sale_fees = defaultdict(float)
        for sale in sales:
            sale_fees[sale['mint']] += sale.get('exit_fees_usd', 0)
        rows = [dict(id=name+':funding', at=started, strategy=name, action='virtual_funding',
                     mint='', symbol=LABELS[name]+' initial virtual funding', cash_delta_usd=capital,
                     fees_usd=0, pnl_usd=0, cost_usd=0, quantity=None, price_usd=None, reason='Virtual capital, excluded from profit')]
        for e in raw_events:
            d, action = e['details'], e['action']
            delta, fees, pnl, cost, qty, price = 0, 0, 0, 0, None, None
            if action == 'fill_entry':
                cost = d['cost_usd']; delta = -cost; fees = d.get('entry_fees_usd', 0)
                qty, price = d.get('quantity'), d.get('entry_price_usd')
            elif action in ('fill_exit', 'fill_partial_exit'):
                trade = by_exit[(e['mint'], e['at'])] if name == 'scalp' else d
                cost = trade['cost_usd']; delta = d['proceeds_usd']; pnl = delta-cost
                fees = d.get('exit_fees_usd', 0)
                qty, price = trade.get('quantity'), d.get('exit_price_usd')
            elif action in ('failed_entry', 'failed_exit'):
                fees = d['fee_usd']; delta = -fees; pnl = -fees
            rows.append(dict(id=f'{name}:{e["id"]}', at=e['at'], strategy=name, action=action,
                mint=e['mint'], symbol=symbols.get(e['mint'], e['mint'][:8]), reason=e['reason'],
                cash_delta_usd=delta, fees_usd=fees, pnl_usd=pnl, cost_usd=cost, quantity=qty, price_usd=price, sequence=e['id']))
        # Reconcile each recorded cash checkpoint. The engine's operating costs
        # are real modeled debits even though its old event table omitted them.
        funding = engine.funding['events'] if engine.funding else []
        movements = sorted([(r['at'], r['cash_delta_usd']) for r in rows if r['action'] != 'virtual_funding']+
                           [(f['at'], f['amount_usd']) for f in funding])
        expected, index, inferred_costs = capital, 0, 0
        for point in equity:
            while index < len(movements) and movements[index][0] <= point['at']:
                expected += movements[index][1]; index += 1
            diff = point['cash']-expected
            if abs(diff) > 1e-7:
                if name != 'scalp' or diff > 0 or cfg['daily_operating_cost_usd'] <= 0:
                    raise ValueError('Unexplained cash movement in '+name+' ledger')
                rows.append(dict(id=f'{name}:cost:{point["id"]}', at=point['at'], strategy=name,
                    action='operating_cost', mint='', symbol='Modeled operating expense',
                    cash_delta_usd=diff, fees_usd=0, pnl_usd=diff, cost_usd=0,
                    quantity=None, price_usd=None, reason='Recorded cash expense'))
                inferred_costs -= diff; expected += diff
        if abs(inferred_costs-s.get('operating_cost_usd', 0)) > 1e-6:
            raise ValueError('Operating expenses do not reconcile')
        if abs(expected-s['cash_usd']) > 1e-6:
            raise ValueError('Latest cash does not match its action ledger')
        trades = []
        for p in closed:
            initial_qty = p.get('initial_quantity', p['quantity'])
            item = dict(p, id=f'{name}:{p["native_id"]}', strategy=name,
                quantity=initial_qty, entry_market_cap_usd=p.get('entry_market_cap_usd', p.get('entry_cap')),
                exit_price_usd=p['proceeds_usd']/initial_qty,
                fees_usd=p.get('fees_usd', p.get('entry_fees_usd', 0)+sale_fees[p['mint']]),
                return_fraction=p['pnl_usd']/p['cost_usd'], hold_seconds=p['exit_at']-p['entry_at'])
            trades.append({k: item.get(k) for k in ('id', 'strategy', 'mint', 'symbol', 'quantity', 'entry_at',
                'exit_at', 'entry_price_usd', 'exit_price_usd', 'entry_market_cap_usd', 'cost_usd',
                'proceeds_usd', 'pnl_usd', 'return_fraction', 'fees_usd', 'hold_seconds', 'exit_reason')})
        pending = s.get('pending_entries', s.get('pending', {}))
        size = cfg['position_usd' if name == 'scalp' else 'order_usd']
        positions = []
        for p in s['positions'].values():
            cost = p.get('remaining_cost_usd', p['cost_usd'])
            unrealized = p['mark_value_usd']-cost
            positions.append(dict(mint=p['mint'], symbol=p['symbol'], strategy=name,
                position_id=name+':'+p['mint'], quantity=p['quantity'], entry_at=p['entry_at'],
                entry_price_usd=p['entry_price_usd'], entry_market_cap_usd=p.get('entry_market_cap_usd', p.get('entry_cap')),
                cost_usd=cost, initial_cost_usd=p['cost_usd'], mark_value_usd=p['mark_value_usd'],
                mark_reason=p.get('mark_reason'), unrealized_pnl_usd=unrealized,
                return_fraction=unrealized/cost, status=p['status'],
                exit_price_usd=p['mark_value_usd']/p['quantity'] if not p.get('mark_reason') else None,
                remaining_fraction=p['quantity']/p.get('initial_quantity', p['quantity'])))
        realized = sum(r['pnl_usd'] for r in rows)
        value = s['cash_usd']+sum(p['mark_value_usd'] for p in positions)
        contributed = capital+sum(f['amount_usd'] for f in funding)
        if abs(value-contributed-realized-sum(p['unrealized_pnl_usd'] for p in positions)) > 1e-6:
            raise ValueError('Allocation profit does not reconcile')
        return dict(name=name, label=LABELS[name], state=s, capital=capital, contributed=contributed,
            started_at=started, equity_points=equity, rows=rows, trades=trades, positions=positions,
            cash_usd=s['cash_usd'], reserved_usd=len(pending)*size, equity_usd=value,
            realized_pnl_usd=realized, unrealized_pnl_usd=sum(p['unrealized_pnl_usd'] for p in positions),
            pnl_usd=value-contributed, pending_count=len(pending), fees_usd=sum(r['fees_usd'] for r in rows))

    def snapshot(self):
        # Fixed acquisition order; neither market worker holds these two locks together.
        with self.scalp.lock, self.ladder.lock:
            members = [self._member('scalp', self.scalp), self._member('ladder', self.ladder)]
        rows = [r for m in members for r in m['rows']]
        for transfer in self.transfers:
            rows.append(dict(id='transfer:'+transfer['id'], at=transfer['at'], strategy='account',
                action='allocation_transfer', mint='', symbol=LABELS[transfer['source']]+' → '+LABELS[transfer['destination']],
                cash_delta_usd=0, fees_usd=0, pnl_usd=0, cost_usd=0, quantity=None, price_usd=None,
                transfer_usd=transfer['amount'], reason='Internal allocation; account cash and profit unchanged'))
        rows.sort(key=lambda r: (r['at'], 0 if r['action']=='virtual_funding' else 1,
            {'scalp':0,'ladder':1,'account':2}[r['strategy']],r.get('sequence',0),r['id']))
        cash = 0
        for row in rows:
            cash += row['cash_delta_usd']; row['cash_after_usd'] = cash
        if abs(cash-sum(m['cash_usd'] for m in members)) > 1e-6:
            raise ValueError('Combined account cash does not reconcile')
        updates = defaultdict(list)
        for m in members:
            updates[m['started_at']].append((m['name'], m['capital'], m['capital'], 0))
            for p in m['equity_points']:
                updates[p['at']].append((m['name'], p['value'], p['cash'], p.get('impaired', 0)))
        values, curve = {}, []
        for at, changes in sorted(updates.items()):
            for name, value, available, impaired in changes:
                values[name] = (value, available, impaired)
            capital = sum(m['capital'] for m in members if m['started_at'] <= at)
            total = sum(v[0] for v in values.values())
            curve.append({'at': at, 'value': total, 'cash': sum(v[1] for v in values.values()),
                          'impaired': sum(v[2] for v in values.values()), 'pnl': total-capital,
                          'capital_usd': capital})
        return members, rows, curve

    def build(self, now, period='live', outcome='all', sort='newest', page=1, search='',
              ledger_page=1, ledger_strategy='all', ledger_action='all', ledger_search=''):
        if ledger_strategy not in ('all', 'scalp', 'ladder', 'account'):
            raise ValueError('Unknown account strategy filter')
        if ledger_action not in ('all', 'buys', 'sales', 'fees', 'funding', 'orders'):
            raise ValueError('Unknown account action filter')
        data = build_view(self.scalp, now, period, outcome, sort, page, search)
        members, rows, curve = self.snapshot()
        started = min(m['started_at'] for m in members)
        start = period_start(period, now, started)
        end = min(max(self.migration['at'],max(m['state']['last_at'] or m['started_at'] for m in members)), now)
        before = [p for p in curve if p['at'] < start]
        previous = before[-1] if before else None
        points = [dict(p) for p in curve if start <= p['at'] <= end]
        baseline_pnl = previous['pnl'] if previous else 0
        baseline = previous['value'] if previous else min(members, key=lambda m:m['started_at'])['capital']
        for p in points:
            p['period_pnl'] = p['pnl']-baseline_pnl
        period_rows = [r for r in rows if start <= r['at'] <= end]
        capital_added = sum(r['cash_delta_usd'] for r in period_rows if r['action']=='virtual_funding')
        denominator = (previous['value'] if previous else 0)+capital_added
        cash = sum(m['cash_usd'] for m in members)
        reserved = sum(m['reserved_usd'] for m in members)
        capital = sum(m['capital'] for m in members)
        equity = sum(m['equity_usd'] for m in members)
        positions = sorted([p for m in members for p in m['positions']], key=lambda p:(p['entry_at'],p['position_id']), reverse=True)
        realized = sum(m['realized_pnl_usd'] for m in members)
        unrealized = sum(m['unrealized_pnl_usd'] for m in members)
        if abs(equity-capital-realized-unrealized)>1e-6:
            raise ValueError('Account balance sheet does not reconcile')
        p = data['portfolio']
        p.update(equity_usd=equity, cash_usd=cash, available_cash_usd=cash-reserved,
            reserved_cash_usd=reserved, initial_usd=capital, total_pnl_usd=equity-capital,
            unrealized_pnl_usd=unrealized, realized_net_pnl_usd=realized,
            open_count=len(positions), pending_count=sum(m['pending_count'] for m in members),
            exposure_usd=sum(x['cost_usd'] for x in positions), impaired_count=sum(bool(x['mark_reason']) for x in positions),
            started_at=started, last_at=min(m['state']['last_at'] or 0 for m in members),
            halted=False, halt_reason=None, run_result=None, profit_exit_pending=False)
        p['feed_alive'] = p['last_at']>0 and now-p['last_at']<=max(20,data['config']['poll_seconds']*3)
        p['status'] = 'active' if p['feed_alive'] else 'offline'
        p['buys_last_minute'] = sum(r['action']=='fill_entry' and now-60<r['at']<=now for r in rows)
        p['attempts_last_minute'] = sum(r['action'] in ('fill_entry','failed_entry') and now-60<r['at']<=now for r in rows)
        closed = [t for m in members for t in m['trades'] if start<=t['exit_at']<=end]
        wins = [t for t in closed if t['pnl_usd']>0]; losses = [t for t in closed if t['pnl_usd']<0]
        selected = [t for t in closed if (outcome=='all' or outcome=='winners' and t['pnl_usd']>0 or
                    outcome=='losers' and t['pnl_usd']<0) and search.casefold() in (t['symbol']+' '+t['mint']).casefold()]
        selected.sort(key=lambda t:(t['exit_at'],t['id']) if sort in ('newest','oldest') else
                      (t['pnl_usd'],t['exit_at'],t['id']),reverse=sort in ('newest','best'))
        pages=max(1,math.ceil(len(selected)/12));page=max(1,min(int(page),pages))
        groups={}
        for t in closed:
            group=groups.setdefault(t['mint'],dict(mint=t['mint'],symbol=t['symbol'],pnl_usd=0,trades=0,cost_usd=0))
            group['pnl_usd']+=t['pnl_usd'];group['cost_usd']+=t['cost_usd'];group['trades']+=1
        for g in groups.values():g['return_fraction']=g['pnl_usd']/g['cost_usd']
        leaders=sorted(groups.values(),key=lambda g:g['pnl_usd'],reverse=True)
        peak,drawdown=baseline,0
        for point in points:
            adjusted=baseline+point['period_pnl'];peak=max(peak,adjusted)
            if peak>0:drawdown=max(drawdown,1-adjusted/peak)
        change=points[-1]['period_pnl'] if points else None
        data['range'].update(requested_start=start,end=end,has_data=bool(points),
            first_observation=points[0]['at'] if points else None,partial_history=started>start+1,
            baseline_usd=baseline,baseline_at=previous['at'] if previous else started,
            baseline_pnl_usd=baseline_pnl,capital_added_usd=capital_added,flow_adjusted=True)
        data['performance'].update(net_pnl_usd=change,return_fraction=change/denominator if change is not None and denominator else None,
            closed_trade_pnl_usd=sum(t['pnl_usd'] for t in closed),closed_count=len(closed),wins=len(wins),losses=len(losses),
            breakeven=len(closed)-len(wins)-len(losses),win_rate=len(wins)/len(closed) if closed else None,
            buys=sum(r['action']=='fill_entry' for r in period_rows),
            sells=sum(r['action'] in ('fill_exit','fill_partial_exit') for r in period_rows),
            failed_attempts=sum(r['action'] in ('failed_entry','failed_exit') for r in period_rows),
            fees_usd=sum(r['fees_usd'] for r in period_rows),max_drawdown_fraction=drawdown,
            profit_factor=sum(t['pnl_usd'] for t in wins)/-sum(t['pnl_usd'] for t in losses) if losses else None,
            realized_net_pnl_usd=sum(r['pnl_usd'] for r in period_rows),capital_added_usd=capital_added)
        data.update(positions=positions,equity_curve=compact_curve(points),
            journal=dict(rows=selected[(page-1)*12:page*12],total=len(selected),pages=pages,page=page,outcome=outcome,sort=sort,search=search),
            winners=[g for g in leaders if g['pnl_usd']>0][:4],losers=[g for g in reversed(leaders) if g['pnl_usd']<0][:4])
        activity=[r for r in reversed(period_rows) if r['action']!='virtual_funding'][:24]
        data['activity']=[dict(r,amount_usd=r.get('transfer_usd') or (abs(r['cash_delta_usd']) if r['cash_delta_usd'] else None)) for r in activity]
        filters={'buys':{'fill_entry'},'sales':{'fill_exit','fill_partial_exit'},
                 'funding':{'virtual_funding','allocation_transfer'}}
        ledger_search=str(ledger_search).strip()[:100]
        statement=[r for r in reversed(period_rows) if (ledger_strategy=='all' or r['strategy']==ledger_strategy) and
            (ledger_action=='all' or ledger_action=='fees' and r['fees_usd']>0 or
             ledger_action=='orders' and r['action'] not in FINANCIAL|{'virtual_funding','allocation_transfer','operating_cost'} or
             r['action'] in filters.get(ledger_action,set())) and
            ledger_search.casefold() in (r['symbol']+' '+r['mint']+' '+r['reason']).casefold()]
        ledger_pages=max(1,math.ceil(len(statement)/20));ledger_page=max(1,min(int(ledger_page),ledger_pages))
        data['account']={'version':'unified-paper-account-v1','migration_at':self.migration['at'],
            'balance_sheet':{'cash_usd':cash,'reserved_cash_usd':reserved,'available_cash_usd':cash-reserved,
                'positions_cost_usd':sum(x['cost_usd'] for x in positions),'positions_value_usd':equity-cash,
                'original_capital_usd':capital,'realized_net_pnl_usd':realized,'unrealized_pnl_usd':unrealized,
                'equity_usd':equity,'pnl_usd':equity-capital,'fees_included_usd':sum(m['fees_usd'] for m in members),
                'reconciliation_difference_usd':equity-capital-realized-unrealized},
            'allocations':[{k:m[k] for k in ('name','label','cash_usd','reserved_usd','equity_usd','realized_pnl_usd','unrealized_pnl_usd','pnl_usd','pending_count')} |
                {'open_count':len(m['positions']),'weight':m['equity_usd']/equity if equity else 0,
                 'funded_capital_usd':m['contributed'],'run_result':m['state'].get('run_result')} for m in members],
            'statement':{'rows':statement[(ledger_page-1)*20:ledger_page*20],'page':ledger_page,'pages':ledger_pages,
                'total':len(statement),'strategy':ledger_strategy,'action':ledger_action,'search':ledger_search},
            'scope':'Active short-term and long-hold allocations. Earlier stopped experiments are archived research portfolios and do not add capital to this account.',
            'archived_runs':self.archives()}
        return data

    def archives(self):
        result=[]
        for name,label in [('live.sqlite','Original experiment'),('frequent.sqlite','Frequent experiment')]:
            path=self.path.parent/name
            if not path.exists():continue
            with sqlite3.connect(f'file:{path}?mode=ro',uri=True) as con:
                meta=dict(con.execute('SELECT key,value FROM meta'))
                state=json.loads(meta['state']);cfg=json.loads(meta['contract'])['config']
                eq=state['cash_usd']+sum(p['mark_value_usd'] for p in state['positions'].values())
                result.append({'name':name,'label':label,'equity_usd':eq,'pnl_usd':eq-cfg['virtual_balance_usd'],
                    'open_count':len(state['positions']),'closed_count':con.execute('SELECT count(*) FROM trades').fetchone()[0],
                    'last_at':state['last_at']})
        return result

    def export_statement(self):
        _,rows,_=self.snapshot()
        return csv_text(rows,('id','at','strategy','action','mint','symbol','quantity','price_usd','cost_usd',
                             'cash_delta_usd','fees_usd','pnl_usd','cash_after_usd','transfer_usd','reason'))

    def export_trades(self):
        members,_,_=self.snapshot()
        rows=sorted([t for m in members for t in m['trades']],key=lambda t:(t['exit_at'],t['id']))
        return csv_text(rows,('id','strategy','mint','symbol','entry_at','exit_at','entry_market_cap_usd',
            'quantity','entry_price_usd','exit_price_usd','cost_usd','proceeds_usd','pnl_usd','fees_usd','exit_reason'))
