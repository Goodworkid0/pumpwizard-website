"""Matched forward-only experiments. No signing, real orders or pooled balances.

Ownership is recorded as non-blocking telemetry; only the exit arms add a
net-value profit trail. Fees, signals, sizing and original stops/targets stay identical.
"""
import copy
import hashlib
import re

from .engine import canonical
from .ladder import LadderRunner
from .research_runner import ResearchRunner


ARMS = (('control', 'V2 control'), ('risk', 'V2 ownership telemetry'),
        ('exits', 'V2 exits'), ('combined', 'V2 combined'))


def trial_entries(document, parents):
    """Resolve every parent before funding anything; never select using results."""
    if document.get('version') != 'matched-forward-trial-v1':
        raise ValueError('Unknown matched trial manifest')
    trial_id = document.get('trial_id', '')
    if not re.fullmatch(r'[a-z0-9-]{1,8}', trial_id):
        raise ValueError('Invalid trial ID')
    for field in ('profit_activation_return', 'profit_trailing_drawdown'):
        value = document.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < value < 1:
            raise ValueError('Invalid trial profit policy')
    if not document.get('parents'):
        raise ValueError('Trial parents required')
    lookup = {p['id']: p for p in parents}
    result = []; seen = set(); parent_ids = set()
    for selection in document['parents']:
        group, parent_id = selection['group'], selection['id']
        if not re.fullmatch(r'[a-z0-9-]{1,20}', group) or parent_id in parent_ids:
            raise ValueError('Invalid or repeated trial parent')
        parent_ids.add(parent_id)
        if parent_id not in lookup or lookup[parent_id].get('trial'):
            raise ValueError('Trial parent must be an existing original cohort: ' + parent_id)
        parent = lookup[parent_id]; original = parent['runner'].cfg
        if original['budget_usd'] != 500:
            raise ValueError('Matched trials require a $500 parent definition')
        for arm, label in ARMS:
            ident = f'{trial_id}-{group}-{arm}'
            if ident in seen or ident in lookup or len(ident) > 40:
                raise ValueError('Duplicate or oversized trial instance ID')
            seen.add(ident)
            config = copy.deepcopy(original)
            ownership_telemetry = arm in ('risk', 'combined')
            exits_enabled = arm in ('exits', 'combined')
            metadata = {'trial_id': trial_id, 'group': group, 'arm': arm, 'label': label,
                'parent_id': parent_id, 'control_id': f'{trial_id}-{group}-control',
                'risk_enabled': False, 'ownership_required': False,
                'ownership_telemetry': ownership_telemetry, 'exits_enabled': exits_enabled,
                'profit_activation_return': document['profit_activation_return'],
                'profit_trailing_drawdown': document['profit_trailing_drawdown'],
                'parent_config_hash': hashlib.sha256(canonical(original).encode()).hexdigest()}
            result.append({'id': ident, 'label': parent['label'] + ' · ' + label,
                'description': parent.get('description'), 'family': original['entry_style'],
                'config': config, 'trial': metadata,
                'research': isinstance(parent['runner'], ResearchRunner)})
    return result


class TrialPolicy:
    immutable_contract = True

    def __init__(self, *args, definition, **kwargs):
        self.experiment_definition = copy.deepcopy(definition)
        super().__init__(*args, **kwargs)

    def entry_reason(self, mint, m, evidence, t):
        reason = super().entry_reason(mint, m, evidence, t)
        if self.experiment_definition['ownership_telemetry']:
            # This is observability only. Missing, stale, or flagged evidence
            # is recorded without changing a simulated entry or exit decision.
            metrics = self.state.setdefault('trial_metrics', {})
            metrics['ownership_observations'] = metrics.get('ownership_observations', 0) + 1
        return reason

    def exit_position(self, mint, p, m, evidence, t):
        return super().exit_position(mint, p, m, evidence, t)

    def additional_exit_reason(self, mint, p, sell, t):
        definition = self.experiment_definition
        if not definition['exits_enabled'] or not sell:
            return None
        # Includes all previous partial proceeds plus today's NET liquidation.
        # This stays comparable across partial sales and includes modeled fees.
        value = p['proceeds_usd'] + sell['proceeds_usd']
        peak = max(p.get('profit_peak_value_usd', value), value)
        p['profit_peak_value_usd'] = peak
        if not p.get('profit_protection_armed') and peak / p['cost_usd'] - 1 >= definition['profit_activation_return']:
            p['profit_protection_armed'] = True
            self.event(t, mint, 'profit_protection_armed', {'symbol': p['symbol'],
                'net_return': peak / p['cost_usd'] - 1, 'peak_liquidation_usd': peak,
                'reason': 'Net return reached profit-trail activation; no fill yet'})
        if p.get('profit_protection_armed') and value <= peak * (1-definition['profit_trailing_drawdown']):
            return 'net_profit_trail'
        return None

    def _step(self, batch):
        super()._step(batch)
        equity = self.state['cash_usd'] + sum(p['mark_value_usd'] for p in self.state['positions'].values())
        peak = max(self.state.get('equity_peak', self.cfg['budget_usd']), equity)
        self.state['equity_peak'] = peak
        self.state['max_drawdown'] = max(self.state.get('max_drawdown', 0), 1-equity/peak)

    def event(self, t, mint, action, data):
        super().event(t, mint, action, dict(data, trial=self.experiment_definition,
                                         code_hash=self.contract['code_hash']))

    def report(self):
        # Hold the lock for the entire snapshot, including comparison statistics.
        with self.lock:
            report = super().report()
            gains, losses = self.db.execute('''SELECT
                coalesce(sum(max(0,json_extract(data,'$.pnl_usd'))),0),
                coalesce(sum(max(0,-json_extract(data,'$.pnl_usd'))),0) FROM trades''').fetchone()
            report.update(trial=copy.deepcopy(self.experiment_definition),
                trial_metrics=copy.deepcopy(self.state.get('trial_metrics', {})),
                max_drawdown=self.state.get('max_drawdown', 0),
                closed_profit_factor=gains/losses if losses else None,
                closed_gross_profit_usd=gains, closed_gross_loss_usd=losses,
                code_hash=self.contract['code_hash'],
                model='Matched forward simulation. Independent $500 ledger; unchanged modeled fees and delayed fills. Not real blockchain orders.')
            return report


class TrialLadderRunner(TrialPolicy, LadderRunner):
    extra_code_files = ('versioned_trials.py',)


class TrialResearchRunner(TrialPolicy, ResearchRunner):
    extra_code_files = (*ResearchRunner.extra_code_files, 'versioned_trials.py')


def open_trials(root, document, parents, risk, shared):
    entries = trial_entries(document, parents)
    result = []
    try:
        for entry in entries:
            args = [root/'data'/('long-hold-' + entry['id'] + '.sqlite'), entry['config'], risk]
            runner_type = TrialResearchRunner if entry['research'] else TrialLadderRunner
            if entry['research']:
                if shared is None:
                    raise ValueError('Research trial requires shared observations')
                args.append(shared)
            runner = runner_type(*args, definition=entry['trial'])
            result.append(dict(entry, runner=runner))
    except Exception:
        for entry in result:
            entry['runner'].close()
        raise
    return result
