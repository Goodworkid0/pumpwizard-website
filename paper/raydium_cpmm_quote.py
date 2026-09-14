"""Strictly read-only, historical Raydium CPMM base-input shadow quotes."""
import hashlib

from .engine import canonical
from .market_archive import MarketArchive
from .raydium_cpmm_config import SCHEMA as CONFIG_SCHEMA, ingest_config_recording
from .raydium_cpmm_reconcile import SCHEMA as RECONCILIATION_SCHEMA
from .raydium_cpmm_vaults import PARSER as VAULT_PARSER


SCHEMA = 'pumpwizard.raydium-cpmm-shadow-quote/v1'
SPL_TOKEN_PROGRAM = 'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA'
FEE_DENOMINATOR = 1_000_000
U64_MAX = (1 << 64) - 1


class RaydiumShadowQuoteError(ValueError):
    pass


def _ceil_div(numerator, denominator):
    return (numerator + denominator - 1) // denominator


def _fee(amount, rate, ceiling=True):
    return _ceil_div(amount * rate, FEE_DENOMINATOR) if ceiling else amount * rate // FEE_DENOMINATOR


def _locator(value):
    if not isinstance(value, dict):
        raise RaydiumShadowQuoteError('Missing state locator')
    slot, version = value.get('slot'), value.get('write_version')
    if isinstance(slot, bool) or not isinstance(slot, int) or slot < 1:
        raise RaydiumShadowQuoteError('Invalid state slot')
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise RaydiumShadowQuoteError('Invalid state write_version')
    return slot, version


def _valid_report(report):
    if not isinstance(report, dict) or not isinstance(report.get('sha256'), str):
        raise RaydiumShadowQuoteError('Missing reconciliation proof report')
    copied = dict(report)
    actual = copied.pop('sha256')
    if hashlib.sha256(canonical(copied).encode()).hexdigest() != actual:
        raise RaydiumShadowQuoteError('Invalid reconciliation proof report')
    if report.get('schema') != RECONCILIATION_SCHEMA or not isinstance(report.get('comparisons'), list):
        raise RaydiumShadowQuoteError('Unsupported reconciliation proof report')
    return report


def _reconciled(report, archive, pool_id, locator):
    for comparison in report['comparisons']:
        if not isinstance(comparison, dict) or comparison.get('pool_id') != pool_id:
            continue
        event = archive.get_event(comparison.get('event_id')) if isinstance(comparison.get('event_id'), str) else None
        state = archive.get_pool_state(pool_id, *locator)
        if not event or not state or event.get('content_sha256') != comparison.get('event_sha256'):
            continue
        for role, state_hash in (('before', comparison.get('before_state_sha256')),
                                 ('after', comparison.get('after_state_sha256'))):
            anchor = comparison.get(role)
            if (isinstance(anchor, dict) and (anchor.get('slot'), anchor.get('write_version')) == locator
                    and state.get('content_sha256') == state_hash):
                return {'event_id': event['id'], 'state_role': role}
    raise RaydiumShadowQuoteError('State is not anchored by a current reconciliation proof')


def _creator_fee_on_input(pool_account, base_input):
    mode = pool_account.get('creator_fee_mode')
    if mode == 0:
        return True
    if mode == 1:
        return base_input
    if mode == 2:
        return not base_input
    raise RaydiumShadowQuoteError('Unsupported creator fee mode')


def quote(recording, archive):
    """Calculate a proof-linked base-input quote; never writes or submits anything."""
    if not isinstance(archive, MarketArchive):
        raise TypeError('archive must be a MarketArchive')
    if not isinstance(recording, dict) or recording.get('schema') != SCHEMA:
        raise RaydiumShadowQuoteError('Unsupported shadow quote schema')
    try:
        raw = canonical(recording)
    except (TypeError, ValueError) as exc:
        raise RaydiumShadowQuoteError('Quote request is not canonical JSON') from exc
    pool_id = recording.get('pool_id')
    locator = _locator(recording.get('state'))
    pool = archive.get_pool(pool_id) if isinstance(pool_id, str) else None
    state = archive.get_pool_state(pool_id, *locator) if pool else None
    if not pool or not state or state.get('parser') != VAULT_PARSER:
        raise RaydiumShadowQuoteError('Unknown or unsupported captured pool state')
    proof = _valid_report(recording.get('reconciliation_report'))
    anchor = _reconciled(proof, archive, pool_id, locator)
    account = state.get('state', {}).get('pool_account')
    if not isinstance(account, dict) or account.get('swap_enabled') is not True:
        raise RaydiumShadowQuoteError('Pool state is not quote-eligible')
    base_vault = state['state'].get('base_vault', {})
    quote_vault = state['state'].get('quote_vault', {})
    if base_vault.get('owner_program') != SPL_TOKEN_PROGRAM or quote_vault.get('owner_program') != SPL_TOKEN_PROGRAM:
        raise RaydiumShadowQuoteError('Token-2022 transfer-fee handling is not implemented')
    config_recording = recording.get('config_recording')
    if not isinstance(config_recording, dict) or config_recording.get('schema') != CONFIG_SCHEMA:
        raise RaydiumShadowQuoteError('Missing compatible config recording')
    config_report = ingest_config_recording(config_recording, archive)
    if len(config_report['configs']) != 1:
        raise RaydiumShadowQuoteError('Quote requires exactly one config snapshot')
    config = config_report['configs'][0]
    if (config['pool_id'] != pool_id or config['config_address'] != account.get('config_address')
            or config['slot'] != state['slot'] or config['commitment'] != state['commitment']):
        raise RaydiumShadowQuoteError('Config snapshot is not compatible with pool state')
    input_mint, amount_in = recording.get('input_mint'), recording.get('amount_in')
    if input_mint not in (pool['base_mint'], pool['quote_mint']):
        raise RaydiumShadowQuoteError('Input mint does not belong to pool')
    if isinstance(amount_in, bool) or not isinstance(amount_in, int) or not 0 < amount_in <= U64_MAX:
        raise RaydiumShadowQuoteError('Invalid amount_in')
    base_input = input_mint == pool['base_mint']
    input_reserve = account['base_trade_reserve'] if base_input else account['quote_trade_reserve']
    output_reserve = account['quote_trade_reserve'] if base_input else account['base_trade_reserve']
    if not isinstance(input_reserve, int) or not isinstance(output_reserve, int) or input_reserve <= 0 or output_reserve <= 0:
        raise RaydiumShadowQuoteError('Invalid trade reserves')
    rates = config['fees']
    creator_rate = rates['creator_fee_rate'] if account.get('creator_fee_enabled') else 0
    creator_on_input = _creator_fee_on_input(account, base_input)
    if creator_on_input:
        total_fee = _fee(amount_in, rates['trade_fee_rate'] + creator_rate)
        creator_fee = total_fee * creator_rate // (rates['trade_fee_rate'] + creator_rate) if creator_rate else 0
        trade_fee = total_fee - creator_fee
        net_input = amount_in - total_fee
    else:
        trade_fee = _fee(amount_in, rates['trade_fee_rate'])
        creator_fee = 0
        net_input = amount_in - trade_fee
    if net_input <= 0:
        raise RaydiumShadowQuoteError('Amount is consumed by fees')
    output_before_creator_fee = net_input * output_reserve // (input_reserve + net_input)
    if not creator_on_input:
        creator_fee = _fee(output_before_creator_fee, creator_rate)
    amount_out = output_before_creator_fee - creator_fee
    if amount_out <= 0 or amount_out >= output_reserve:
        raise RaydiumShadowQuoteError('Quote produces no usable output')
    protocol_fee = _fee(trade_fee, rates['protocol_fee_rate'], ceiling=False)
    fund_fee = _fee(trade_fee, rates['fund_fee_rate'], ceiling=False)
    result = {'schema': SCHEMA, 'mode': 'shadow_historical_base_input_quote',
              'pool_id': pool_id, 'state': {'slot': state['slot'], 'write_version': state['write_version'],
                                             'content_sha256': state['content_sha256']},
              'config': {'slot': config['slot'], 'write_version': config['write_version'],
                         'account_sha256': config['account_sha256'], 'capture_sha256': config['capture_sha256']},
              'input_mint': input_mint, 'output_mint': pool['quote_mint'] if base_input else pool['base_mint'],
              'amount_in': amount_in, 'amount_out': amount_out,
              'input_reserve': input_reserve, 'output_reserve': output_reserve,
              'fees': {'trade': trade_fee, 'protocol': protocol_fee, 'fund': fund_fee,
                       'creator': creator_fee, 'creator_on_input': creator_on_input},
              'request_sha256': hashlib.sha256(raw.encode()).hexdigest(),
              'reconciliation_sha256': proof['sha256'],
              'reconciliation_anchor': anchor,
              'observed_at': state['observed_at'],
              'limitations': ['historical state only', 'classic SPL Token only', 'not executable', 'not a simulated fill']}
    result['sha256'] = hashlib.sha256(canonical(result).encode()).hexdigest()
    return result
