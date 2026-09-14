"""A narrow, versioned observed-only Raydium CPMM paper-strategy pilot.

The pilot deliberately consumes immutable standard-RPC snapshots from one pool.
It has no wallet, order route, or public HTTP integration.  A caller must invoke
``step`` explicitly; the returned decision contains the snapshot/quote evidence
used for any paper-ledger action.
"""
import math
import time

from .observed_market_log import ObservedMarketLog
from .raydium_cpmm_observed_ledger import ObservedRaydiumLedger
from .raydium_cpmm_observed_quote import quote


SCHEMA = 'pumpwizard.raydium-cpmm-pilot/v1'


class RaydiumPilotError(ValueError):
    pass


def _integer(value, name, minimum=1, maximum=None):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RaydiumPilotError('Invalid ' + name)
    if maximum is not None and value > maximum:
        raise RaydiumPilotError('Invalid ' + name)
    return value


def _config(value):
    if not isinstance(value, dict):
        raise RaydiumPilotError('Pilot config must be an object')
    if value.get('schema', SCHEMA) != SCHEMA:
        raise RaydiumPilotError('Unsupported pilot schema')
    pool_id = value.get('pool_id')
    base_mint = value.get('base_mint')
    quote_mint = value.get('quote_mint')
    if not all(isinstance(item, str) and 1 <= len(item) <= 200 for item in (pool_id, base_mint, quote_mint)):
        raise RaydiumPilotError('Invalid pilot pool identity')
    result = {
        'schema': SCHEMA,
        'id': value.get('id', 'near-usdc-observed-trend-v1'),
        'pool_id': pool_id,
        'base_mint': base_mint,
        'quote_mint': quote_mint,
        'initial_quote_amount': _integer(value.get('initial_quote_amount'), 'initial_quote_amount'),
        'order_quote_amount': _integer(value.get('order_quote_amount'), 'order_quote_amount'),
        'base_probe_amount': _integer(value.get('base_probe_amount'), 'base_probe_amount'),
        'confirmation_snapshots': _integer(value.get('confirmation_snapshots', 3), 'confirmation_snapshots', 3, 20),
        'minimum_growth_bps': _integer(value.get('minimum_growth_bps', 50), 'minimum_growth_bps', 1, 10_000),
        'max_hold_snapshots': _integer(value.get('max_hold_snapshots', 10), 'max_hold_snapshots', 1, 10_000),
    }
    if not isinstance(result['id'], str) or not 1 <= len(result['id']) <= 80:
        raise RaydiumPilotError('Invalid pilot id')
    if result['order_quote_amount'] > result['initial_quote_amount']:
        raise RaydiumPilotError('Pilot order exceeds virtual balance')
    return result


class ObservedRaydiumPilot:
    """Explicit one-pool trend pilot backed only by observed snapshot evidence."""
    def __init__(self, config):
        self.config = _config(config)

    def _quotes(self, archive, log):
        snapshots = list(reversed(log.recent(self.config['pool_id'], self.config['confirmation_snapshots'])))
        if len(snapshots) < self.config['confirmation_snapshots']:
            return [], snapshots
        quotes = []
        for snapshot in snapshots:
            estimate = quote(self.config['pool_id'], snapshot['sequence'], self.config['base_mint'],
                             self.config['base_probe_amount'], archive, log)
            if estimate['output_mint'] != self.config['quote_mint']:
                raise RaydiumPilotError('Observed quote denomination does not match pilot')
            quotes.append(estimate)
        return quotes, snapshots

    def inspect(self, archive, log):
        if not isinstance(log, ObservedMarketLog) or not log.view_only:
            raise RaydiumPilotError('Pilot requires a read-only observed log')
        quotes, snapshots = self._quotes(archive, log)
        if not quotes:
            return {'schema': SCHEMA, 'pilot_id': self.config['id'], 'action': 'wait',
                    'reason': 'Awaiting consecutive observed snapshots', 'snapshot_count': len(snapshots),
                    'required_snapshots': self.config['confirmation_snapshots']}
        start, latest = quotes[0], quotes[-1]
        growth_bps = (latest['amount_out'] - start['amount_out']) * 10_000 / start['amount_out']
        return {'schema': SCHEMA, 'pilot_id': self.config['id'], 'action': 'observe',
                'growth_bps': growth_bps, 'minimum_growth_bps': self.config['minimum_growth_bps'],
                'snapshots': [{'sequence': item['snapshot']['sequence'], 'sha256': item['snapshot']['sha256'],
                               'quote_sha256': item['sha256'], 'base_probe_amount': item['amount_in'],
                               'quote_amount': item['amount_out']} for item in quotes]}

    def step(self, archive, log, ledger, at=None):
        if not isinstance(ledger, ObservedRaydiumLedger) or ledger.view_only:
            raise RaydiumPilotError('Pilot requires a writable isolated observed ledger')
        if ledger.config['pool_id'] != self.config['pool_id'] or ledger.config['initial_quote_amount'] != self.config['initial_quote_amount']:
            raise RaydiumPilotError('Observed ledger contract does not match pilot')
        now = time.time() if at is None else float(at)
        if not math.isfinite(now) or now <= 0:
            raise RaydiumPilotError('Invalid decision time')
        evidence = self.inspect(archive, log)
        if evidence['action'] == 'wait':
            return evidence
        latest_sequence = evidence['snapshots'][-1]['sequence']
        open_positions = [item for item in ledger.state['positions'].values() if item['status'] == 'open']
        if open_positions:
            position = open_positions[0]
            if latest_sequence - position['entry_sequence'] < self.config['max_hold_snapshots']:
                return {**evidence, 'action': 'hold', 'position_id': position['id'],
                        'remaining_hold_snapshots': self.config['max_hold_snapshots'] - (latest_sequence - position['entry_sequence'])}
            closed = ledger.close_position(position['id'], latest_sequence, archive, log, at=now)
            return {**evidence, 'action': 'close', 'position': closed}
        if evidence['growth_bps'] < self.config['minimum_growth_bps']:
            return {**evidence, 'action': 'wait', 'reason': 'Trend threshold not met'}
        position_id = self.config['id'] + '-s' + str(latest_sequence)
        opened = ledger.open_position(position_id, latest_sequence, self.config['order_quote_amount'], archive, log, at=now)
        return {**evidence, 'action': 'open', 'position': opened}
