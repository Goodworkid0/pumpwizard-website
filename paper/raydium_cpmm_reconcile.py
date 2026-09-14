"""Read-only reconciliation of archived Raydium CPMM swap intent and vault states."""
import hashlib

from .engine import canonical
from .market_archive import MarketArchive
from .raydium_cpmm import CPMM_PROGRAM_ID
from .raydium_cpmm_events import PARSER as EVENT_PARSER
from .raydium_cpmm_vaults import PARSER as VAULT_PARSER


SCHEMA = 'pumpwizard.raydium-cpmm-reconciliation/v1'


class RaydiumReconciliationError(ValueError):
    pass


def _locator(value, name):
    if not isinstance(value, dict):
        raise RaydiumReconciliationError('Missing ' + name + ' state locator')
    slot, version = value.get('slot'), value.get('write_version')
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
        raise RaydiumReconciliationError('Invalid ' + name + ' slot')
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise RaydiumReconciliationError('Invalid ' + name + ' write_version')
    return slot, version


def _amount(state, side):
    try:
        value = state['state'][side + '_vault']['amount']
    except (KeyError, TypeError) as exc:
        raise RaydiumReconciliationError('State lacks ' + side + ' vault amount') from exc
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RaydiumReconciliationError('Invalid ' + side + ' vault amount')
    return value


def _comparison(item, archive):
    if not isinstance(item, dict) or not isinstance(item.get('event_id'), str):
        raise RaydiumReconciliationError('Comparison needs an event_id')
    event = archive.get_event(item['event_id'])
    if not event or event.get('event_kind') != 'swap' or event.get('parser') != EVENT_PARSER:
        raise RaydiumReconciliationError('Comparison references an unsupported swap event')
    pool = archive.get_pool(event['pool_id'])
    if not pool or pool.get('program_id') != CPMM_PROGRAM_ID:
        raise RaydiumReconciliationError('Swap event does not reference a Raydium CPMM pool')
    before_locator = _locator(item.get('before'), 'before')
    after_locator = _locator(item.get('after'), 'after')
    before = archive.get_pool_state(event['pool_id'], *before_locator)
    after = archive.get_pool_state(event['pool_id'], *after_locator)
    if not before or not after or before.get('parser') != VAULT_PARSER or after.get('parser') != VAULT_PARSER:
        raise RaydiumReconciliationError('Comparison requires archived CPMM vault states')
    if before_locator == after_locator or before['slot'] > event['slot'] or after['slot'] < event['slot']:
        raise RaydiumReconciliationError('States do not bracket the event slot')
    base_delta = _amount(after, 'base') - _amount(before, 'base')
    quote_delta = _amount(after, 'quote') - _amount(before, 'quote')
    payload = event.get('payload')
    if not isinstance(payload, dict) or not isinstance(payload.get('intent'), dict):
        raise RaydiumReconciliationError('Event lacks swap intent')
    direction, intent = payload.get('direction'), payload['intent']
    if direction == 'base_to_quote':
        input_delta, output_delta = base_delta, -quote_delta
    elif direction == 'quote_to_base':
        input_delta, output_delta = quote_delta, -base_delta
    else:
        raise RaydiumReconciliationError('Unsupported swap direction')
    if input_delta <= 0 or output_delta <= 0:
        raise RaydiumReconciliationError('Vault deltas do not match swap direction')
    if payload.get('instruction') == 'swap_base_input':
        maximum, minimum = intent.get('amount_in'), intent.get('minimum_amount_out')
    elif payload.get('instruction') == 'swap_base_output':
        maximum, minimum = intent.get('max_amount_in'), intent.get('amount_out')
    else:
        raise RaydiumReconciliationError('Unsupported swap instruction')
    if (isinstance(maximum, bool) or not isinstance(maximum, int) or maximum < input_delta
            or isinstance(minimum, bool) or not isinstance(minimum, int) or minimum > output_delta):
        raise RaydiumReconciliationError('Vault deltas do not satisfy swap intent bounds')
    return {'event_id': event['id'], 'pool_id': event['pool_id'], 'before': dict(zip(('slot', 'write_version'), before_locator)),
            'after': dict(zip(('slot', 'write_version'), after_locator)),
            'base_delta': base_delta, 'quote_delta': quote_delta,
            'input_vault_delta': input_delta, 'output_vault_delta': output_delta,
            'event_sha256': event['content_sha256'], 'before_state_sha256': before['content_sha256'],
            'after_state_sha256': after['content_sha256']}


def reconcile(recording, archive):
    """Produce a proof report after strict, read-only event/state reconciliation."""
    if not isinstance(archive, MarketArchive):
        raise TypeError('archive must be a MarketArchive')
    if not isinstance(recording, dict) or recording.get('schema') != SCHEMA:
        raise RaydiumReconciliationError('Unsupported reconciliation schema')
    comparisons = recording.get('comparisons')
    if not isinstance(comparisons, list) or not comparisons or len(comparisons) > 100:
        raise RaydiumReconciliationError('Recording must contain 1 to 100 comparisons')
    try:
        raw = canonical(recording)
    except (TypeError, ValueError) as exc:
        raise RaydiumReconciliationError('Recording is not canonical JSON') from exc
    reconciled = [_comparison(item, archive) for item in comparisons]
    report = {'schema': SCHEMA, 'mode': 'shadow_event_state_reconciliation',
              'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'raw': recording,
              'comparisons': reconciled, 'archive_manifest': archive.verify()}
    report['sha256'] = hashlib.sha256(canonical(report).encode()).hexdigest()
    return report
