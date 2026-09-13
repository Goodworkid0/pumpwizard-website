"""Audited capital transfers around the frozen paper execution models.

Transfers change spendable strategy allocations, not trading P&L. Historical
quotes and fills retain their original contracts; exports carry the dated flows.
"""
import copy
import hashlib
import json
from pathlib import Path
import zlib

from .engine import canonical
from .guarded import GuardedEngine
from .ladder import LadderRunner


def implementation_hash():
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def adjusted_state(state, amount, strategy):
    state = copy.deepcopy(state)
    state['cash_usd'] += amount
    if state['cash_usd'] < -1e-8:
        raise ValueError('Allocation transfer would overdraw virtual cash')
    if strategy == 'scalp':
        for key in ('peak_equity', 'day_start_equity'):
            state[key] += amount
    return state


class Funding:
    def _funding_init(self, strategy, base_config, capital_key):
        self.strategy = strategy
        self.base_config = copy.deepcopy(base_config)
        self.capital_key = capital_key
        self.replay_transfers = []
        row = self.db.execute("SELECT value FROM meta WHERE key='account_funding'").fetchone()
        self.funding = json.loads(row[0]) if row else None
        if self.funding and self.funding['code_hash'] != implementation_hash():
            self.close()
            raise ValueError('Funding policy changed; preserve the prior implementation before resuming')
        self._funding_config()

    def _funding_config(self):
        amount = sum(e['amount_usd'] for e in self.funding['events']) if self.funding else 0
        config = dict(self.base_config)
        config[self.capital_key] += amount
        if config[self.capital_key] <= 0:
            raise ValueError('Allocation capital must remain positive')
        if self.strategy == 'scalp':
            self.config = config
        else:
            self.cfg = config

    def load_replay_funding(self, funding):
        if not funding:
            return
        if funding['code_hash'] != implementation_hash():
            raise ValueError('Recording requires its original funding policy')
        if funding['base_capital_usd'] != self.base_config[self.capital_key]:
            raise ValueError('Replay starting capital must match its recorded funding history')
        self.replay_transfers = copy.deepcopy(funding['events'])

    def _replay_funding_until(self, t):
        applied = {e['id'] for e in self.funding['events']} if self.funding else set()
        for flow in self.replay_transfers:
            if flow['at'] > t or flow['id'] in applied:
                continue
            self.state = adjusted_state(self.state, flow['amount_usd'], self.strategy)
            if self.funding is None:
                self.funding = {'version': 'account-funding-v1', 'strategy': self.strategy,
                    'base_capital_usd': self.base_config[self.capital_key],
                    'code_hash': implementation_hash(), 'events': []}
            self.funding['events'].append(copy.deepcopy(flow))
            self._funding_config()
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('account_funding', canonical(self.funding)))
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('state', canonical(self.state)))
                equity = self.state['cash_usd'] + sum(p['mark_value_usd'] for p in self.state['positions'].values())
                if self.strategy == 'scalp':
                    self.db.execute('INSERT INTO equity(at,value,cash,impaired) VALUES (?,?,?,?)',
                        (flow['at'], equity, self.state['cash_usd'], sum(bool(p.get('mark_reason')) for p in self.state['positions'].values())))
                else:
                    self.db.execute('INSERT INTO equity(at,value,cash) VALUES (?,?,?)', (flow['at'], equity, self.state['cash_usd']))
            applied.add(flow['id'])

    def step(self, batch):
        with self.lock:
            self._replay_funding_until(batch['at'])
            return super().step(batch)

    def report(self):
        result = super().report()
        if self.funding:
            result['account_funding'] = copy.deepcopy(self.funding)
            result['strategy'] = self.strategy
            if self.strategy == 'ladder':
                result['model'] = result['model'].replace('Separate $100 forward paper test.',
                    'Long-hold allocation within the unified paper account.')
        return result


class FundedScalp(Funding, GuardedEngine):
    def __init__(self, path, config, source, ownership_config, **kwargs):
        super().__init__(path, config, source, ownership_config, **kwargs)
        self._funding_init('scalp', config, 'virtual_balance_usd')

    def export_recording(self, path):
        with self.lock, open(path, 'w') as f:
            header = {'type': 'header', 'source': self.source, 'config': self.base_config,
                'implementation_hash': self.implementation_hash, 'entry_policy': self.entry_policy}
            if self.funding:
                header['funding'] = self.funding
            f.write(canonical(header)+'\n')
            for row in self.db.execute('SELECT payload FROM batches ORDER BY at'):
                f.write(zlib.decompress(row[0]).decode()+'\n')


class FundedLadder(Funding, LadderRunner):
    def __init__(self, path, config, ownership_config, source='public_index_live'):
        super().__init__(path, config, ownership_config, source)
        self._funding_init('ladder', config, 'budget_usd')

    def export_recording(self, path):
        with self.lock, open(path, 'w') as f:
            header = dict(self.contract, type='ladder_header')
            if self.funding:
                header['funding'] = self.funding
            f.write(canonical(header)+'\n')
            for row in self.db.execute('SELECT payload FROM inputs ORDER BY at'):
                f.write(zlib.decompress(row[0]).decode()+'\n')
