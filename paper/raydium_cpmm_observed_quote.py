"""Read-only CPMM estimates from an append-only standard-RPC observation.

The result is suitable only as an input to a separately labelled observed paper
simulation.  It is not a fill, executable quote, reconciled state, or strict
historical replay.
"""
import base64
import hashlib

from .engine import canonical
from .market_archive import MarketArchive
from .observed_market_log import ObservedMarketLog, normalize_snapshot
from .raydium_cpmm import CPMM_PROGRAM_ID
from .raydium_cpmm_config import ACCOUNT_DISCRIMINATOR, ACCOUNT_LENGTH, FEE_DENOMINATOR
from .raydium_cpmm_quote import SPL_TOKEN_PROGRAM, U64_MAX, _creator_fee_on_input, _fee
from .raydium_cpmm_vaults import RaydiumVaultReplayError, _pool_account, _vault


SCHEMA = 'pumpwizard.raydium-cpmm-observed-quote/v1'


class RaydiumObservedQuoteError(ValueError):
    pass


def _config(value, pool, account):
    if not isinstance(value, dict) or value.get('address') != account.get('config_address'):
        raise RaydiumObservedQuoteError('Observed config does not match pool account')
    if value.get('owner') != CPMM_PROGRAM_ID:
        raise RaydiumObservedQuoteError('Unexpected observed config owner')
    try:
        raw = base64.b64decode(value.get('data_base64', '').encode('ascii'), validate=True)
    except (AttributeError, UnicodeEncodeError, ValueError) as exc:
        raise RaydiumObservedQuoteError('Invalid observed config account') from exc
    if (len(raw) != ACCOUNT_LENGTH or raw[:8] != ACCOUNT_DISCRIMINATOR or raw[9] not in (0, 1)
            or hashlib.sha256(raw).hexdigest() != value.get('data_sha256')):
        raise RaydiumObservedQuoteError('Unexpected observed config layout')
    configuration = pool.get('configuration')
    if not isinstance(configuration, dict) or configuration.get('cpmm_config') != value['address']:
        raise RaydiumObservedQuoteError('Observed config does not match archived pool identity')
    rates = {'trade_fee_rate': int.from_bytes(raw[12:20], 'little'),
             'protocol_fee_rate': int.from_bytes(raw[20:28], 'little'),
             'fund_fee_rate': int.from_bytes(raw[28:36], 'little'),
             'creator_fee_rate': int.from_bytes(raw[108:116], 'little')}
    if (rates['trade_fee_rate'] + rates['creator_fee_rate'] >= FEE_DENOMINATOR
            or rates['protocol_fee_rate'] > FEE_DENOMINATOR
            or rates['fund_fee_rate'] > FEE_DENOMINATOR
            or rates['protocol_fee_rate'] + rates['fund_fee_rate'] > FEE_DENOMINATOR):
        raise RaydiumObservedQuoteError('Invalid observed CPMM fee rates')
    return {'fees': rates, 'account_sha256': value['data_sha256']}


def quote(pool_id, sequence, input_mint, amount_in, archive, log):
    """Estimate one base-input CPMM swap from an immutable observed snapshot."""
    if not isinstance(archive, MarketArchive) or not archive.view_only:
        raise RaydiumObservedQuoteError('Observed quote requires a read-only market archive')
    if not isinstance(log, ObservedMarketLog) or not log.view_only:
        raise RaydiumObservedQuoteError('Observed quote requires a read-only observed market log')
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise RaydiumObservedQuoteError('Invalid observed snapshot sequence')
    if isinstance(amount_in, bool) or not isinstance(amount_in, int) or not 0 < amount_in <= U64_MAX:
        raise RaydiumObservedQuoteError('Invalid amount_in')
    pool = archive.get_pool(pool_id) if isinstance(pool_id, str) else None
    snapshot = log.get_snapshot(pool_id, sequence) if pool else None
    if not pool or pool.get('program_id') != CPMM_PROGRAM_ID or not snapshot:
        raise RaydiumObservedQuoteError('Unknown observed Raydium CPMM snapshot')
    try:
        snapshot = normalize_snapshot(snapshot)
        base = _vault(snapshot['base_vault'], pool['base_mint'], 'base')
        quote_vault = _vault(snapshot['quote_vault'], pool['quote_mint'], 'quote')
        account = _pool_account(snapshot['pool_account'], pool, base, quote_vault)
    except (RaydiumVaultReplayError, ValueError) as exc:
        raise RaydiumObservedQuoteError('Observed pool snapshot is invalid: ' + str(exc)) from exc
    if not account['swap_enabled']:
        raise RaydiumObservedQuoteError('Observed pool is not swap enabled')
    if base['owner_program'] != SPL_TOKEN_PROGRAM or quote_vault['owner_program'] != SPL_TOKEN_PROGRAM:
        raise RaydiumObservedQuoteError('Token-2022 transfer-fee handling is not implemented')
    config = _config(snapshot['config'], pool, account)
    if input_mint not in (pool['base_mint'], pool['quote_mint']):
        raise RaydiumObservedQuoteError('Input mint does not belong to pool')
    base_input = input_mint == pool['base_mint']
    input_reserve = account['base_trade_reserve'] if base_input else account['quote_trade_reserve']
    output_reserve = account['quote_trade_reserve'] if base_input else account['base_trade_reserve']
    rates = config['fees']
    creator_rate = rates['creator_fee_rate'] if account['creator_fee_enabled'] else 0
    creator_on_input = _creator_fee_on_input(account, base_input)
    if creator_on_input:
        total_fee = _fee(amount_in, rates['trade_fee_rate'] + creator_rate)
        creator_fee = total_fee * creator_rate // (rates['trade_fee_rate'] + creator_rate) if creator_rate else 0
        trade_fee, net_input = total_fee - creator_fee, amount_in - total_fee
    else:
        trade_fee, net_input, creator_fee = _fee(amount_in, rates['trade_fee_rate']), None, 0
        net_input = amount_in - trade_fee
    if net_input <= 0:
        raise RaydiumObservedQuoteError('Amount is consumed by fees')
    output_before_creator_fee = net_input * output_reserve // (input_reserve + net_input)
    if not creator_on_input:
        creator_fee = _fee(output_before_creator_fee, creator_rate)
    amount_out = output_before_creator_fee - creator_fee
    if amount_out <= 0 or amount_out >= output_reserve:
        raise RaydiumObservedQuoteError('Quote produces no usable output')
    protocol_fee = _fee(trade_fee, rates['protocol_fee_rate'], ceiling=False)
    fund_fee = _fee(trade_fee, rates['fund_fee_rate'], ceiling=False)
    result = {'schema': SCHEMA, 'mode': 'observed_snapshot_base_input_quote',
              'assurance': 'observed_simulation_only', 'pool_id': pool_id,
              'snapshot': {'sequence': sequence, 'sha256': snapshot['sha256'],
                           'context_slot': snapshot['context_slot'], 'commitment': snapshot['commitment'],
                           'observed_at': snapshot['observed_at'],
                           'endpoint_kind': snapshot['source']['endpoint_kind'],
                           'write_version': 'unavailable_not_invented'},
              'config_account_sha256': config['account_sha256'],
              'input_mint': input_mint, 'output_mint': pool['quote_mint'] if base_input else pool['base_mint'],
              'amount_in': amount_in, 'amount_out': amount_out,
              'input_reserve': input_reserve, 'output_reserve': output_reserve,
              'fees': {'trade': trade_fee, 'protocol': protocol_fee, 'fund': fund_fee,
                       'creator': creator_fee, 'creator_on_input': creator_on_input},
              'limitations': ['standard RPC snapshot', 'account write version unavailable',
                              'not reconciled', 'classic SPL Token only', 'not executable',
                              'not a simulated fill']}
    result['sha256'] = hashlib.sha256(canonical(result).encode()).hexdigest()
    return result
