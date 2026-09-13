"""Fast, unverified simulation cohort for collecting market-behavior samples.

This runner never constructs or submits transactions. It deliberately does not
use ownership clearance as an entry condition; that distinction is exposed in
its public label and should never be used to approve real execution.
"""
import copy

from .engine import Engine
from .model import load_config


class SimulationSampler:
    is_simulation_sampler = True

    def __init__(self, root):
        self.cfg = load_config(root / 'simulation_sampler.config.json')
        self.engine = Engine(root / 'data' / 'simulation-sampler.sqlite', self.cfg, 'public_index_live')
        self.lock = self.engine.lock
        self.db = self.engine.db

    def close(self):
        self.engine.close()

    def held_mints(self):
        return set(self.engine.watch_priority())

    def step(self, batch):
        self.engine.step(batch)

    def report(self):
        raw = self.engine.report()
        with self.lock:
            snapshots = copy.deepcopy(self.engine.state['snapshots'])
        positions = []
        for position in raw['positions']:
            point = copy.deepcopy(position)
            point.update(entry_cap=point['entry_market_cap_usd'],
                         last_cap=snapshots.get(point['mint'], {}).get('market_cap_usd'),
                         total_pnl_usd=point.get('unrealized_pnl_usd', 0),
                         remaining_fraction=1, status=point.get('status', 'open'))
            positions.append(point)
        trades = []
        for trade in raw['trades']:
            row = copy.deepcopy(trade)
            row.update(entry_cap=row['entry_market_cap_usd'],
                       exit_cap=row.get('exit_market_cap_usd'),
                       initial_fraction=1)
            trades.append(row)
        candidates = []
        for candidate in raw['candidates']:
            row = copy.deepcopy(candidate)
            row['reason'] = 'Entry scheduled' if row['reason'] == 'entry_scheduled' else row['reason'].replace('_', ' ')
            candidates.append(row)
        target_cap = self.cfg['market_cap_max_usd'] * (1 + self.cfg['take_profit_fraction'])
        config = {'entry_cap_min': self.cfg['market_cap_min_usd'],
                  'entry_cap_max': self.cfg['market_cap_max_usd'],
                  'order_usd': self.cfg['position_usd'], 'max_positions': self.cfg['max_positions'],
                  'observation_seconds': self.cfg['observation_seconds'],
                  'stop_loss_fraction': self.cfg['stop_loss_fraction'],
                  'trailing_drawdown_fraction': 0,
                  'targets': [{'market_cap_usd': target_cap, 'initial_fraction': 1}]}
        return {'version':'simulation-sampler-v1','initial_usd':self.cfg['virtual_balance_usd'],
                'cash_usd':raw['cash_usd'],'equity_usd':raw['equity_usd'],'pnl_usd':raw['total_pnl_usd'],
                'realized_net_pnl_usd':raw['realized_net_pnl_usd'],
                'unrealized_pnl_usd':raw['unrealized_pnl_usd'],'positions':positions,'trades':trades,
                'sale_count':len(trades),'closed_count':raw['closed_trades'],
                'pending_count':len(raw['pending_entries']),'started_at':raw['started_at'],'last_at':raw['last_at'],
                'failed_fees_usd':raw['failed_fees_usd'],'fees_usd':raw['modeled_fees_usd'],
                'candidates':candidates,'events':raw['decisions'],'config':config,
                'model':'Simulation-only data cohort. Entries do not wait for ownership RPC clearance and are not eligible for real execution.'}
