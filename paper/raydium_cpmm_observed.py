"""Bounded standard-RPC Raydium CPMM snapshots for observed-only research."""
import base64
import hashlib
import json
import math
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from .engine import canonical
from .market_archive import MarketArchive
from .raydium_cpmm import CPMM_PROGRAM_ID
from .raydium_cpmm_vaults import BASE58, POOL_ACCOUNT_DISCRIMINATOR, POOL_ACCOUNT_LENGTH


SCHEMA = 'pumpwizard.raydium-cpmm-observed-snapshot/v1'
PUBLIC_RPC = 'https://api.mainnet.solana.com'
ENV_RPC = 'RAYDIUM_OBSERVED_RPC_URL'
MAX_BODY_BYTES = 2_000_000


class RaydiumObservedError(ValueError):
    pass


def _base58(data):
    number, result = int.from_bytes(data, 'big'), ''
    while number:
        number, remainder = divmod(number, 58)
        result = BASE58[remainder] + result
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + (result or '')


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RaydiumObservedError('Invalid ' + name)
    return value


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RaydiumObservedError('Invalid observed_at')
    return float(value)


def _endpoint(value):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RaydiumObservedError('Observed RPC must be a credential-free HTTPS URL')
    return value


def _rpc(endpoint, method, params):
    if method not in ('getAccountInfo', 'getMultipleAccounts'):
        raise RaydiumObservedError('Observed collector method refused')
    packet = {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}
    request = urllib.request.Request(endpoint, data=canonical(packet).encode(), headers={'Content-Type': 'application/json',
                                      'Accept': 'application/json', 'User-Agent': 'PumpWizardObserved/1.0'})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if urllib.parse.urlsplit(response.geturl()).hostname != urllib.parse.urlsplit(endpoint).hostname:
                raise RaydiumObservedError('Observed RPC redirected to another host')
            body = response.read(MAX_BODY_BYTES + 1)
            if len(body) > MAX_BODY_BYTES:
                raise RaydiumObservedError('Observed RPC response is too large')
            decoded = json.loads(body)
    except urllib.error.HTTPError as exc:
        raise RaydiumObservedError('Observed RPC HTTP ' + str(exc.code)) from None
    except (urllib.error.URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError, OSError) as exc:
        raise RaydiumObservedError('Observed RPC is unavailable or invalid') from exc
    if not isinstance(decoded, dict) or decoded.get('jsonrpc') != '2.0' or decoded.get('error') is not None:
        raise RaydiumObservedError('Observed RPC returned an error')
    return packet, decoded


def _account(value, address, name):
    if not isinstance(value, dict) or value.get('executable') is not False or not isinstance(value.get('owner'), str):
        raise RaydiumObservedError('Invalid ' + name + ' account')
    data = value.get('data')
    if not isinstance(data, list) or len(data) != 2 or data[1] != 'base64' or not isinstance(data[0], str):
        raise RaydiumObservedError('Observed ' + name + ' account is not base64')
    try:
        raw = base64.b64decode(data[0].encode('ascii'), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise RaydiumObservedError('Invalid ' + name + ' account data') from exc
    return {'address': address, 'owner': value['owner'], 'data_base64': data[0], 'data_sha256': hashlib.sha256(raw).hexdigest()}, raw


def _result(value):
    if not isinstance(value, dict) or not isinstance(value.get('result'), dict):
        raise RaydiumObservedError('Observed RPC result is missing')
    result = value['result']
    context = result.get('context')
    if not isinstance(context, dict):
        raise RaydiumObservedError('Observed RPC context is missing')
    return result, _integer(context.get('slot'), 'context slot', 1)


def _targets(pool, account):
    if account['owner'] != CPMM_PROGRAM_ID or len(account['raw']) != POOL_ACCOUNT_LENGTH:
        raise RaydiumObservedError('Unexpected Raydium CPMM pool account')
    raw = account['raw']
    if raw[:8] != POOL_ACCOUNT_DISCRIMINATOR:
        raise RaydiumObservedError('Unexpected Raydium CPMM pool layout')
    configuration = pool.get('configuration')
    if not isinstance(configuration, dict) or _base58(raw[8:40]) != configuration.get('cpmm_config'):
        raise RaydiumObservedError('Pool config does not match archived identity')
    mappings = ((raw[168:200], raw[72:104], raw[232:264]), (raw[200:232], raw[104:136], raw[264:296]))
    expected = {pool['base_mint']: 'base', pool['quote_mint']: 'quote'}
    targets = {}
    for mint_raw, vault_raw, program_raw in mappings:
        mint, vault, program = _base58(mint_raw), _base58(vault_raw), _base58(program_raw)
        side = expected.get(mint)
        if not side or side in targets or program not in (
                'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
                'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb'):
            raise RaydiumObservedError('Pool account has unsupported vault mapping')
        targets[side] = {'address': vault, 'program': program}
    if set(targets) != {'base', 'quote'} or targets['base']['address'] == targets['quote']['address']:
        raise RaydiumObservedError('Pool account vault mapping is incomplete')
    return {'config': _base58(raw[8:40]), 'base_vault': targets['base'], 'quote_vault': targets['quote']}


def collect(pool_id, archive, sequence, commitment='finalized', observed_at=None, endpoint=None, request=_rpc):
    """Fetch one bounded observed snapshot; never writes an archive or simulation."""
    if not isinstance(archive, MarketArchive) or not archive.view_only:
        raise RaydiumObservedError('Observed collection requires a read-only market archive')
    pool = archive.get_pool(pool_id) if isinstance(pool_id, str) else None
    if not pool or pool.get('program_id') != CPMM_PROGRAM_ID:
        raise RaydiumObservedError('Unknown Raydium CPMM pool')
    _integer(sequence, 'sequence', 1)
    if commitment not in ('confirmed', 'finalized'):
        raise RaydiumObservedError('Observed collector requires confirmed or finalized commitment')
    observed_at = _time(time.time() if observed_at is None else observed_at)
    endpoint = _endpoint(endpoint or os.environ.get(ENV_RPC) or PUBLIC_RPC)
    bootstrap_request, bootstrap_response = request(endpoint, 'getAccountInfo', [pool['pool_address'], {
        'encoding': 'base64', 'commitment': commitment}])
    bootstrap_result, bootstrap_slot = _result(bootstrap_response)
    pool_capture, pool_raw = _account(bootstrap_result.get('value'), pool['pool_address'], 'pool')
    pool_capture['raw'] = pool_raw
    targets = _targets(pool, pool_capture)
    addresses = [pool['pool_address'], targets['base_vault']['address'], targets['quote_vault']['address'], targets['config']]
    snapshot_request, snapshot_response = request(endpoint, 'getMultipleAccounts', [addresses, {
        'encoding': 'base64', 'commitment': commitment, 'minContextSlot': bootstrap_slot}])
    snapshot_result, context_slot = _result(snapshot_response)
    values = snapshot_result.get('value')
    if not isinstance(values, list) or len(values) != len(addresses):
        raise RaydiumObservedError('Observed RPC returned incomplete account batch')
    captures = [_account(value, address, name)[0] for value, address, name in zip(
        values, addresses, ('pool', 'base vault', 'quote vault', 'config'))]
    pool_account, base_vault, quote_vault, config = captures
    if pool_account['owner'] != CPMM_PROGRAM_ID or base_vault['owner'] != targets['base_vault']['program'] or quote_vault['owner'] != targets['quote_vault']['program'] or config['owner'] != CPMM_PROGRAM_ID:
        raise RaydiumObservedError('Observed RPC account owners do not match pool layout')
    raw = {'bootstrap': {'request': bootstrap_request, 'response': bootstrap_response},
           'snapshot': {'request': snapshot_request, 'response': snapshot_response}}
    result = {'schema': SCHEMA, 'mode': 'observed_standard_rpc_snapshot', 'source': {
        'transport': 'standard_solana_rpc', 'endpoint_kind': 'official_public' if endpoint == PUBLIC_RPC else 'operator_configured',
        'write_version': 'unavailable_not_invented'}, 'pool_id': pool_id, 'sequence': sequence,
        'context_slot': context_slot, 'bootstrap_context_slot': bootstrap_slot, 'commitment': commitment,
        'observed_at': observed_at, 'pool_account': pool_account, 'base_vault': base_vault,
        'quote_vault': quote_vault, 'config': config, 'raw_sha256': hashlib.sha256(canonical(raw).encode()).hexdigest(),
        'raw': raw, 'limitations': ['standard RPC snapshot', 'account write version unavailable',
                                    'not reconciled', 'not executable', 'not a simulated fill']}
    result['sha256'] = hashlib.sha256(canonical(result).encode()).hexdigest()
    return result
