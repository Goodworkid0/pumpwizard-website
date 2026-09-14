"""Read-only validation of Raydium CPMM quotes against reconciled historical swaps."""
import hashlib

from .engine import canonical
from .market_archive import MarketArchive
from .raydium_cpmm_events import PARSER as EVENT_PARSER
from .raydium_cpmm_quote import SCHEMA as QUOTE_SCHEMA, RaydiumShadowQuoteError, quote
from .raydium_cpmm_reconcile import (SCHEMA as RECONCILIATION_SCHEMA,
                                     RaydiumReconciliationError, reconcile)


SCHEMA = 'pumpwizard.raydium-cpmm-shadow-validation/v1'


class RaydiumShadowValidationError(ValueError):
    pass


def _locator(value):
    if not isinstance(value, dict):
        raise RaydiumShadowValidationError('Missing quote state locator')
    slot, version = value.get('slot'), value.get('write_version')
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
        raise RaydiumShadowValidationError('Invalid quote state slot')
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise RaydiumShadowValidationError('Invalid quote state write_version')
    return slot, version


def _replayed_proof(value, archive):
    if not isinstance(value, dict) or value.get('schema') != RECONCILIATION_SCHEMA:
        raise RaydiumShadowValidationError('Validation case needs a reconciliation proof')
    raw = value.get('raw')
    if not isinstance(raw, dict):
        raise RaydiumShadowValidationError('Validation needs a reproducible reconciliation recording')
    try:
        replayed = reconcile(raw, archive)
    except RaydiumReconciliationError as exc:
        raise RaydiumShadowValidationError('Reconciliation proof no longer verifies: ' + str(exc)) from exc
    if value.get('sha256') != replayed['sha256']:
        raise RaydiumShadowValidationError('Reconciliation proof does not match current archive evidence')
    return replayed


def _comparison(proof, event_id, pool_id, locator):
    for item in proof['comparisons']:
        before = item.get('before')
        if (item.get('event_id') == event_id and item.get('pool_id') == pool_id and isinstance(before, dict)
                and (before.get('slot'), before.get('write_version')) == locator):
            return item
    raise RaydiumShadowValidationError('Case quote state is not the reconciled state before its event')


def _case(value, archive):
    if not isinstance(value, dict) or value.get('quote_request', {}).get('schema') != QUOTE_SCHEMA:
        raise RaydiumShadowValidationError('Each validation case needs a shadow quote request')
    request = value['quote_request']
    event_id = value.get('event_id')
    if not isinstance(event_id, str):
        raise RaydiumShadowValidationError('Each validation case needs an event_id')
    proof = _replayed_proof(request.get('reconciliation_report'), archive)
    locator = _locator(request.get('state'))
    try:
        calculated = quote(request, archive)
    except RaydiumShadowQuoteError as exc:
        raise RaydiumShadowValidationError('Quote request is not eligible: ' + str(exc)) from exc
    if calculated['reconciliation_anchor'] != {'event_id': event_id, 'state_role': 'before'}:
        raise RaydiumShadowValidationError('Quote is not anchored to the state before its validation event')
    event = archive.get_event(event_id)
    if not event or event.get('parser') != EVENT_PARSER or event.get('event_kind') != 'swap':
        raise RaydiumShadowValidationError('Validation event is not an archived Raydium CPMM swap')
    payload = event.get('payload')
    intent = payload.get('intent') if isinstance(payload, dict) else None
    if (not isinstance(intent, dict) or payload.get('instruction') != 'swap_base_input'
            or request['input_mint'] != payload.get('input_mint')
            or request['amount_in'] != intent.get('amount_in')):
        raise RaydiumShadowValidationError('Validation only supports matching base-input swap evidence')
    comparison = _comparison(proof, event_id, request.get('pool_id'), locator)
    actual_in = comparison.get('input_vault_delta')
    actual_out = comparison.get('output_vault_delta')
    if (isinstance(actual_in, bool) or not isinstance(actual_in, int) or actual_in <= 0
            or isinstance(actual_out, bool) or not isinstance(actual_out, int) or actual_out <= 0):
        raise RaydiumShadowValidationError('Reconciliation proof has invalid vault deltas')
    if actual_in != request['amount_in']:
        raise RaydiumShadowValidationError('Observed input does not match exact base-input instruction')
    result = {'event_id': event_id, 'pool_id': request['pool_id'],
              'quote_sha256': calculated['sha256'], 'reconciliation_sha256': proof['sha256'],
              'state': calculated['state'], 'amount_in': request['amount_in'],
              'predicted_amount_out': calculated['amount_out'], 'observed_amount_out': actual_out,
              'output_difference': calculated['amount_out'] - actual_out,
              'matched': calculated['amount_out'] == actual_out}
    result['sha256'] = hashlib.sha256(canonical(result).encode()).hexdigest()
    return result


def validate(recording, archive):
    """Validate up to 100 immutable historical base-input cases without mutation."""
    if not isinstance(archive, MarketArchive):
        raise TypeError('archive must be a MarketArchive')
    if not isinstance(recording, dict) or recording.get('schema') != SCHEMA:
        raise RaydiumShadowValidationError('Unsupported shadow validation schema')
    cases = recording.get('cases')
    if not isinstance(cases, list) or not cases or len(cases) > 100:
        raise RaydiumShadowValidationError('Validation recording must contain 1 to 100 cases')
    try:
        raw = canonical(recording)
    except (TypeError, ValueError) as exc:
        raise RaydiumShadowValidationError('Validation recording is not canonical JSON') from exc
    before = archive.verify()
    results = [_case(item, archive) for item in cases]
    after = archive.verify()
    if before != after:
        raise RaydiumShadowValidationError('Read-only validation unexpectedly changed the archive')
    report = {'schema': SCHEMA, 'mode': 'shadow_historical_quote_validation',
              'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'raw': recording,
              'cases': results,
              'summary': {'total': len(results), 'matched': sum(item['matched'] for item in results),
                          'mismatched': sum(not item['matched'] for item in results),
                          'passed': all(item['matched'] for item in results)},
              'archive_manifest': after,
              'limitations': ['historical validation only', 'classic SPL Token only',
                              'base-input swaps only', 'not executable', 'not a simulated fill']}
    report['sha256'] = hashlib.sha256(canonical(report).encode()).hexdigest()
    return report
