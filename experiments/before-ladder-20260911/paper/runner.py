"""Separate $100 prospective longer-hold paper ledger, backed by observed quotes."""
import copy
import hashlib
import json
from pathlib import Path

from .engine import canonical


class Runner:
    def __init__(self,memory,config):
        self.memory=memory;self.cfg=config
        db=memory.db
        db.executescript('''CREATE TABLE IF NOT EXISTS runner_events(id INTEGER PRIMARY KEY,at REAL,mint TEXT,action TEXT,data TEXT);
            CREATE TABLE IF NOT EXISTS runner_trades(id INTEGER PRIMARY KEY,data TEXT);''')
        contract=canonical({'config':config,'code_hash':hashlib.sha256(Path(__file__).read_bytes()).hexdigest()})
        old=db.execute("SELECT value FROM meta WHERE key='runner_contract'").fetchone()
        if old and old[0]!=contract:raise ValueError('Longer-hold experiment changed; use a new memory ledger for the new rules')
        row=db.execute("SELECT value FROM meta WHERE key='runner_state'").fetchone()
        self.state=json.loads(row[0]) if row else {'cash_usd':config['runner_budget_usd'],'positions':{},'pending':{},'attempted':[],
            'started_at':None,'last_at':None,'attempts':0,'failed_fees_usd':0}
        with db:db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)',('runner_contract',contract))

    def held_mints(self):return set(self.state['positions'])|set(self.state['pending'])

    def event(self,t,mint,action,data):
        self.memory.db.execute('INSERT INTO runner_events(at,mint,action,data) VALUES (?,?,?,?)',(t,mint,action,canonical(data)))

    def quote(self,q,t,position=None):
        c=self.cfg
        if (q.get('dex_id')!='pumpswap' or not 0<=t-q.get('observed_at',0)<=180 or
                q.get('liquidity_usd',0)<c['runner_min_liquidity_usd'] or q.get('base_reserve',0)<=0):
            return None
        price,base=q['price_usd'],q['base_reserve'];reserve=base*price
        if position:
            qty=position['quantity']
            if qty>=base or position['pair_address']!=q['pair_address']:return None
            gross=reserve*qty/(base-qty)*(1-c['runner_adverse_fraction'])
            net=gross*(1-c['runner_fee_fraction'])-c['runner_fixed_fee_usd']
            return {'proceeds_usd':max(0,net),'exit_price_usd':max(0,net)/qty,
                    'exit_fees_usd':gross*c['runner_fee_fraction']+c['runner_fixed_fee_usd']}
        cash=c['runner_order_usd'];pool=(cash-c['runner_fixed_fee_usd'])/(1+c['runner_fee_fraction'])
        if pool<=0 or pool>reserve*.01:return None
        qty=base*pool/(reserve+pool)*(1-c['runner_adverse_fraction'])
        buy={'quantity':qty,'cost_usd':cash,'entry_price_usd':cash/qty,'pair_address':q['pair_address'],
             'entry_fees_usd':pool*c['runner_fee_fraction']+c['runner_fixed_fee_usd']}
        sell=self.quote(q,t,buy)
        if not sell or 1-sell['proceeds_usd']/cash>.12:return None
        return buy

    def failed(self,t,mint,side):
        s=self.state;s['attempts']+=1
        u=int(hashlib.sha256(f"runner:{s['attempts']}:{mint}:{side}".encode()).hexdigest()[:16],16)/2**64
        if u>=.05:return False
        fee=self.cfg['runner_fixed_fee_usd'];s['cash_usd']-=fee;s['failed_fees_usd']+=fee
        self.event(t,mint,'failed_'+side,{'fee_usd':fee})
        return True

    def step(self,quotes,t):
        s,c=self.state,self.cfg
        if s['last_at'] is not None and t<=s['last_at']:return
        before=copy.deepcopy(s)
        try:
            fresh={q['mint']:q for q in quotes}
            if s['started_at'] is None:s['started_at']=t
            s['last_at']=t
            for mint,p in list(s['positions'].items()):
                q=fresh.get(mint);sell=self.quote(q,t,p) if q else None
                if q and sell:p['last_usable_quote']=q
                if not sell and not q:
                    last=p.get('last_usable_quote');sell=self.quote(last,t,p) if last else None
                p['mark_value_usd']=sell['proceeds_usd'] if sell else 0
                p['mark_reason']=None if sell else 'No usable quote from the entry pool'
                p['peak_value_usd']=max(p['peak_value_usd'],p['mark_value_usd'])
                age=t-p['entry_at'];ret=p['mark_value_usd']/p['cost_usd']-1
                risk=self.memory.ownership(mint,t)
                reason=None
                if not sell:reason='quote_unavailable'
                elif risk['status']=='blocked':reason='ownership_flag'
                elif ret<=c['runner_stop_fraction']:reason='stop_loss'
                elif age>=c['runner_max_hold_seconds']:reason='ten_day_limit'
                elif age>=c['runner_min_hold_seconds'] and p['mark_value_usd']<=p['peak_value_usd']*(1-c['runner_trailing_fraction']):reason='trailing_exit'
                if reason and not p.get('exit_reason'):
                    p.update(exit_reason=reason,exit_signal_at=t,status='exit_pending')
                    self.event(t,mint,'signal_exit',{'reason':reason})
                # Exits use a later fetched observation, even after a quote gap or ownership flag.
                if (p.get('exit_reason') and q and sell and q['observed_at']>p['exit_signal_at'] and
                        t-p['exit_signal_at']>=2):
                    if self.failed(t,mint,'exit'):continue
                    s['cash_usd']+=sell['proceeds_usd']
                    trade=dict(p,**sell,exit_at=t,pnl_usd=sell['proceeds_usd']-p['cost_usd'])
                    self.memory.db.execute('INSERT INTO runner_trades(data) VALUES (?)',(canonical(trade),))
                    self.event(t,mint,'fill_exit',trade);del s['positions'][mint]
            for mint,order in list(s['pending'].items()):
                if t-order['at']>180:
                    del s['pending'][mint];self.event(t,mint,'cancel_entry',{'reason':'quote_expired'});continue
                q=fresh.get(mint)
                if not q or q['observed_at']<=order['at'] or t-order['at']<2:continue
                coin=self.memory.get(mint);assessment=self.memory.evaluate(coin,t) if coin else None
                buy=self.quote(q,t)
                risk=self.memory.ownership(mint,t)
                if (not assessment or assessment['stage']!='runner_candidate' or not buy or
                        risk['status']!='clear' or not 0<=t-risk.get('checked_at',0)<=120 or s['cash_usd']<c['runner_order_usd']):
                    del s['pending'][mint];self.event(t,mint,'cancel_entry',{'reason':'thesis_or_ownership_changed'});continue
                del s['pending'][mint];s['attempted'].append(mint)
                if self.failed(t,mint,'entry'):continue
                s['cash_usd']-=buy['cost_usd']
                mark=self.quote(q,t,buy)
                p=dict(buy,mint=mint,symbol=q['symbol'],entry_at=t,entry_cap=q['market_cap_usd'],
                    mark_value_usd=mark['proceeds_usd'],peak_value_usd=mark['proceeds_usd'],mark_reason=None,status='open',
                    last_usable_quote=q,thesis={'first_cap':coin['first_cap'],'active_windows':assessment['active_windows'],
                    'growth_fraction':assessment['growth_fraction'],'ownership_checked_at':risk['checked_at']})
                s['positions'][mint]=p;self.event(t,mint,'fill_entry',p)
            if c['runner_enabled']:
                for mint,q in fresh.items():
                    if mint in self.held_mints() or mint in s['attempted']:continue
                    if len(self.held_mints())>=c['runner_max_positions'] or s['cash_usd']-len(s['pending'])*c['runner_order_usd']<c['runner_order_usd']:break
                    coin=self.memory.get(mint)
                    if not coin or self.memory.evaluate(coin,t)['stage']!='runner_candidate' or not self.quote(q,t):continue
                    s['pending'][mint]={'at':t}
                    self.event(t,mint,'signal_entry',{'reason':'persistent_growth_and_ownership_screen'})
            self.memory.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('runner_state',canonical(s)))
        except Exception:
            self.state=before
            raise

    def report(self):
        s,c=self.state,self.cfg
        positions=copy.deepcopy(list(s['positions'].values()))
        equity=s['cash_usd']+sum(p['mark_value_usd'] for p in positions)
        trades=[json.loads(r[0]) for r in self.memory.db.execute('SELECT data FROM runner_trades ORDER BY id DESC')]
        return {'initial_usd':c['runner_budget_usd'],'cash_usd':s['cash_usd'],'equity_usd':equity,
                'pnl_usd':equity-c['runner_budget_usd'],'positions':positions,'trades':trades[:30],
                'closed_count':len(trades),'pending_count':len(s['pending']),'started_at':s['started_at'],
                'model':'Separate paper experiment; indexed pool-reserve estimates, 2.5% modeled fees + 1% adverse fill per side, $0.10 fixed cost, 5% modeled failed attempts.'}
