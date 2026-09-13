"""Isolated forward simulations with shared inputs and ten explicitly defined signals."""
import copy
import math

from .engine import canonical
from .ladder import LadderRunner


class ResearchRunner(LadderRunner):
    extra_code_files=('research_runner.py','shared_research.py','experiment_catalog.py')

    def __init__(self,path,config,risk,shared,source='public_index_live'):
        self.shared=shared
        super().__init__(path,config,risk,source)
        self.db.executescript('''CREATE TABLE IF NOT EXISTS input_refs(id TEXT PRIMARY KEY,at REAL);
            CREATE TABLE IF NOT EXISTS contract_history(hash TEXT PRIMARY KEY,data TEXT);''')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO contract_history VALUES (?,?)',
                (self.contract['code_hash'],canonical(self.contract)))

    def buy_quote(self,m):
        if not m or m['route']=='curve':return super().buy_quote(m)
        c=self.cfg
        pool=(c['order_usd']-c['fixed_fee_usd']-c['entry_rent_usd'])/(1+c['fee_fraction'])
        if pool<=0 or pool>m['real']*c['max_entry_reserve_fraction']:return None
        qty=m['base']*pool/(m['reserve']+pool)*(1-c['adverse_fraction'])
        if not math.isfinite(qty) or qty<=0:return None
        p={'quantity':qty,'initial_quantity':qty,'entry_pool':m['pool'],'amm_pool':m['pool'],
            'migration_seen':True,'entry_supply':m['cap']/m['price'],'cost_usd':c['order_usd'],
            'remaining_cost_usd':c['order_usd'],'entry_price_usd':c['order_usd']/qty,
            'entry_fees_usd':pool*c['fee_fraction']+c['fixed_fee_usd']+c['entry_rent_usd']}
        sell=self.sell_quote(m,p)
        if not sell or 1-sell['proceeds_usd']/c['order_usd']>c['max_roundtrip_loss_fraction']:return None
        return p

    def update_histories(self,quotes,markets,t):
        # Features are prepared once for all cohorts by SharedResearch.
        if self.cfg['entry_style']=='second-wave':
            eligible={mint for mint,at in self.state.get('last_exits',{}).items()
                      if t-at>=max(3600,self.cfg['observation_seconds']) and mint not in self.state['positions']}
            self.state['attempted']=[mint for mint in self.state['attempted'] if mint not in eligible]

    def exit_position(self,mint,p,m,evidence,t):
        # Unverified simulation studies keep ownership evidence in their inputs
        # and entry markers, but RPC results do not override their exit policy.
        # This class cannot construct a real order; strict cohorts retain gates.
        if self.cfg['entry_policy']=='simulation_unverified':
            evidence=dict(evidence,ownership={})
        return super().exit_position(mint,p,m,evidence,t)

    def schedule_exit(self,p,t,reason,stages=None):
        if reason=='ten_day_limit':reason='holding_limit'
        return super().schedule_exit(p,t,reason,stages)

    def entry_reason(self,mint,m,evidence,t):
        c=self.cfg
        if not m:return 'Awaiting fresh supported market quote'
        if not c['entry_cap_min']<=m['cap']<c['entry_cap_max']:return 'Outside entry market-cap band'
        f=self.shared.features.get((mint,c['observation_seconds']))
        if not f or f['pool']!=m['pool']:return 'Warming up: insufficient continuous observations'
        if t-f['end_at']>120 or f['end_at']>t:return 'Awaiting fresh signal observations'
        if c['entry_policy']=='strict' and not self.ownership_clear(mint,evidence,t):return 'Ownership flagged or unverified'
        style=c['entry_style'];growth=f['growth']
        if style not in ('recovery','second-wave') and growth<c['minimum_growth_fraction']:
            return 'Growth below signal threshold'
        if f['drawdown']>c['maximum_peak_drawdown']:return 'Too far below window high'
        liquidity=f['liquidity_growth']
        passed={
            'persistent':f['first_half']>.01 and f['second_half']>.01 and f['drawdown']<.12,
            'higher-low':f['higher_lows'],
            'multi-window':f['first_half']>0 and f['second_half']>.01 and f['recent_quarter']>.005,
            'squeeze':f['squeeze'] and liquidity is not None and liquidity>0,
            'retest':f['retest'],
            'post-grad':m['route']=='amm' and evidence.get('graduated_observed') is True and f['drawdown']<.15 and f['second_half']>0,
            'liquidity':liquidity is not None and liquidity>=.05 and growth>=.03,
            'relative':f['peer_count']>=4 and f['percentile'] is not None and f['percentile']>=.75,
            'second-wave':f['recovery'],
            'recovery':f['recovery'],
        }[style]
        if not passed:return 'Awaiting '+style.replace('-',' ')+' signal'
        if style=='second-wave':
            last=self.state.get('last_exits',{}).get(mint)
            if last is not None and f['start_at']<=last:return 'Awaiting a fresh post-exit observation window'
        if not self.buy_quote(m):return 'Liquidity or modeled entry costs fail screen'
        return None

    def event(self,t,mint,action,data):
        if action in ('signal_entry','fill_entry'):
            data=dict(data,research_family=self.cfg['entry_style'],code_hash=self.contract['code_hash'],
                input_id=self.shared.last_input_id,signal_features=self.shared.features.get((mint,self.cfg['observation_seconds'])))
        if action=='fill_exit':self.state.setdefault('last_exits',{})[mint]=t
        if action in ('fill_entry','failed_entry'):self.state.setdefault('last_exits',{}).pop(mint,None)
        super().event(t,mint,action,data)

    def step(self,batch):
        if self.shared.last_at!=batch['at'] or self.shared.last_input_id is None:
            raise ValueError('Shared input must commit before any research execution')
        with self.lock:
            t=batch['at']
            if self.state['last_at'] is not None and t<=self.state['last_at']:return
            before=copy.deepcopy(self.state)
            try:
                with self.db:
                    self._step(batch)
                    s=self.state;equity=s['cash_usd']+sum(p['mark_value_usd'] for p in s['positions'].values())
                    unrealized=sum(p['mark_value_usd']-p['remaining_cost_usd'] for p in s['positions'].values())
                    assert abs(equity-self.cfg['budget_usd']-(s['realized_pnl_usd']-s['failed_fees_usd']+unrealized))<1e-6
                    peak=max(s.get('equity_peak',self.cfg['budget_usd']),equity)
                    s['equity_peak']=peak;s['max_drawdown']=max(s.get('max_drawdown',0),1-equity/peak)
                    self.db.execute('INSERT INTO input_refs VALUES (?,?)',(self.shared.last_input_id,t))
                    self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('state',canonical(s)))
                    self.db.execute('INSERT INTO equity(at,value,cash) VALUES (?,?,?)',(t,equity,s['cash_usd']))
            except Exception:
                self.state=before
                raise

    def report(self):
        report=super().report()
        with self.lock:
            report.update(max_drawdown=self.state.get('max_drawdown',0),
                experiment_status='active' if report['total_entries'] else 'warming_up' if
                    self.state['started_at'] is None or self.state['last_at']-self.state['started_at']<self.cfg['observation_seconds'] else 'observing',
                family=self.cfg['entry_style'],
                model='Simulation with shared market inputs, independent $500 capital and modeled execution costs.')
        return report
