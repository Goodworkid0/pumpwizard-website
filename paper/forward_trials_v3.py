"""Second matched forward cohort: confirmation and early profit protection.

This module is deliberately separate from ``versioned_trials.py``.  Existing
V1/V2 contracts include that module's source hash and must never be changed
when a new hypothesis is introduced.
"""
import copy
import hashlib
import math
import re

from .engine import canonical
from .ladder import LadderRunner
from .research_runner import ResearchRunner


ARMS = (
    ('control', 'V3 control'),
    ('confirmation', 'V3 confirmation'),
    ('protection', 'V3 protection'),
    ('combined', 'V3 combined'),
)


def trial_entries(document, parents):
    """Create immutable four-arm V3 cohorts without selecting on outcomes."""
    if document.get('version') != 'matched-forward-trial-v3':
        raise ValueError('Unknown V3 forward trial manifest')
    trial_id = document.get('trial_id', '')
    if not re.fullmatch(r'[a-z0-9-]{1,8}', trial_id):
        raise ValueError('Invalid V3 trial ID')
    confirmation_delay = document.get('confirmation_delay_seconds')
    activation = document.get('break_even_activation_return')
    if (isinstance(confirmation_delay, bool) or not isinstance(confirmation_delay, int)
            or not 30 <= confirmation_delay <= 600):
        raise ValueError('Invalid V3 confirmation delay')
    if (isinstance(activation, bool) or not isinstance(activation, (int, float))
            or not math.isfinite(activation) or not 0 < activation < 1):
        raise ValueError('Invalid V3 protection activation')
    if not document.get('parents'):
        raise ValueError('V3 trial parents required')
    lookup = {parent['id']: parent for parent in parents}
    entries, seen, parent_ids = [], set(), set()
    for selection in document['parents']:
        group, parent_id = selection.get('group'), selection.get('id')
        if (not isinstance(group, str) or not re.fullmatch(r'[a-z0-9-]{1,20}', group)
                or parent_id in parent_ids):
            raise ValueError('Invalid or repeated V3 trial parent')
        parent_ids.add(parent_id)
        if parent_id not in lookup or lookup[parent_id].get('trial'):
            raise ValueError('V3 trial parent must be an existing original cohort: ' + str(parent_id))
        parent = lookup[parent_id]
        original = parent['runner'].cfg
        if original['budget_usd'] != 500:
            raise ValueError('Matched V3 trials require a $500 parent definition')
        for arm, label in ARMS:
            ident = f'{trial_id}-{group}-{arm}'
            if ident in seen or ident in lookup or len(ident) > 40:
                raise ValueError('Duplicate or oversized V3 trial instance ID')
            seen.add(ident)
            confirmation_enabled = arm in ('confirmation', 'combined')
            protection_enabled = arm in ('protection', 'combined')
            definition = {
                'trial_id': trial_id,
                'generation': 'V3',
                'comparison_title': 'V3 entry & exit comparison',
                'group': group,
                'arm': arm,
                'label': label,
                'parent_id': parent_id,
                'control_id': f'{trial_id}-{group}-control',
                'confirmation_enabled': confirmation_enabled,
                'protection_enabled': protection_enabled,
                'confirmation_delay_seconds': confirmation_delay,
                'break_even_activation_return': activation,
                'parent_config_hash': hashlib.sha256(canonical(original).encode()).hexdigest(),
            }
            entries.append({
                'id': ident,
                'label': parent['label'] + ' · ' + label,
                'description': parent.get('description'),
                'family': original.get('entry_style', 'momentum'),
                'config': copy.deepcopy(original),
                'trial': definition,
                'research': isinstance(parent['runner'], ResearchRunner),
            })
    return entries


class V3Policy:
    """A small, auditable policy layer over an unchanged parent configuration."""
    immutable_contract = True

    def __init__(self, *args, definition, **kwargs):
        self.experiment_definition = copy.deepcopy(definition)
        super().__init__(*args, **kwargs)

    def entry_reason(self, mint, market, evidence, at):
        reason = super().entry_reason(mint, market, evidence, at)
        if not self.experiment_definition['confirmation_enabled']:
            return reason
        # A pending order was already confirmed when it was scheduled. Recheck
        # the original safety/signal logic at its modeled fill; do not demand a
        # second confirmation all over again.
        if mint in self.state.get('pending', {}):
            return reason
        confirmations = self.state.setdefault('v3_confirmations', {})
        if reason:
            confirmations.pop(mint, None)
            return reason
        previous = confirmations.get(mint)
        delay = self.experiment_definition['confirmation_delay_seconds']
        if previous is None:
            confirmations[mint] = {'at': at}
            self.state.setdefault('trial_metrics', {})['confirmation_waits'] = (
                self.state.setdefault('trial_metrics', {}).get('confirmation_waits', 0) + 1)
            self.event(at, mint, 'confirmation_pending', {
                'reason': 'First valid signal recorded; waiting for a second fresh valid observation',
                'confirmation_delay_seconds': delay,
            })
            return 'Awaiting V3 confirmation'
        if at - previous['at'] < delay:
            return 'Awaiting V3 confirmation'
        del confirmations[mint]
        metrics = self.state.setdefault('trial_metrics', {})
        metrics['confirmations_passed'] = metrics.get('confirmations_passed', 0) + 1
        self.event(at, mint, 'confirmation_passed', {
            'reason': 'Second fresh valid observation confirmed the entry signal',
            'confirmation_delay_seconds': delay,
        })
        return None

    def additional_exit_reason(self, mint, position, sell, at):
        if not self.experiment_definition['protection_enabled'] or not sell:
            return None
        # Use modeled net liquidation plus realized partial proceeds, exactly as
        # the rest of the ledger reports total position value. The scheduled fill
        # can still land below cost after delay; this is protection, not a promise.
        value = position['proceeds_usd'] + sell['proceeds_usd']
        if not position.get('break_even_protection_armed'):
            activation = self.experiment_definition['break_even_activation_return']
            if value / position['cost_usd'] - 1 >= activation:
                position['break_even_protection_armed'] = True
                metrics = self.state.setdefault('trial_metrics', {})
                metrics['protection_armed'] = metrics.get('protection_armed', 0) + 1
                self.event(at, mint, 'break_even_protection_armed', {
                    'net_return': value / position['cost_usd'] - 1,
                    'activation_return': activation,
                    'reason': 'Net liquidation reached V3 protection activation',
                })
        if position.get('break_even_protection_armed') and value <= position['cost_usd']:
            return 'break_even_protection'
        return None

    def _step(self, batch):
        super()._step(batch)
        equity = self.state['cash_usd'] + sum(position['mark_value_usd']
                                               for position in self.state['positions'].values())
        peak = max(self.state.get('equity_peak', self.cfg['budget_usd']), equity)
        self.state['equity_peak'] = peak
        self.state['max_drawdown'] = max(self.state.get('max_drawdown', 0), 1 - equity / peak)

    def event(self, at, mint, action, data):
        payload = dict(data, trial=self.experiment_definition, code_hash=self.contract['code_hash'])
        if hasattr(self, 'shared'):
            payload.update(input_id=self.shared.last_input_id,
                           signal_features=self.shared.features.get((mint, self.cfg['observation_seconds'])))
        super().event(at, mint, action, payload)

    def report(self):
        with self.lock:
            report = super().report()
            gains, losses = self.db.execute('''SELECT
                coalesce(sum(max(0,json_extract(data,'$.pnl_usd'))),0),
                coalesce(sum(max(0,-json_extract(data,'$.pnl_usd'))),0) FROM trades''').fetchone()
            report.update(
                trial=copy.deepcopy(self.experiment_definition),
                trial_metrics=copy.deepcopy(self.state.get('trial_metrics', {})),
                max_drawdown=self.state.get('max_drawdown', 0),
                closed_profit_factor=gains / losses if losses else None,
                closed_gross_profit_usd=gains,
                closed_gross_loss_usd=losses,
                code_hash=self.contract['code_hash'],
                model='Matched V3 forward simulation. Independent $500 ledger; unchanged modeled fees and delayed fills. Not real blockchain orders.',
            )
            return report


class V3LadderRunner(V3Policy, LadderRunner):
    extra_code_files = ('forward_trials_v3.py',)


class V3ResearchRunner(V3Policy, ResearchRunner):
    extra_code_files = (*ResearchRunner.extra_code_files, 'forward_trials_v3.py')


def open_trials(root, document, parents, risk, shared):
    entries = trial_entries(document, parents)
    result = []
    try:
        for entry in entries:
            args = [root / 'data' / ('long-hold-' + entry['id'] + '.sqlite'), entry['config'], risk]
            runner_type = V3ResearchRunner if entry['research'] else V3LadderRunner
            if entry['research']:
                if shared is None:
                    raise ValueError('V3 research trial requires shared observations')
                args.append(shared)
            runner = runner_type(*args, definition=entry['trial'])
            result.append(dict(entry, runner=runner))
    except Exception:
        for entry in result:
            entry['runner'].close()
        raise
    return result
