"""Offline replay of recorded Raydium CPMM vault accounts.

This module has no HTTP/RPC client and is not imported by the production
tracker.  It turns saved, slot-versioned SPL Token account captures into
immutable pool states only after checking their owner program, mint, and binary
token amount against a previously archived Raydium CPMM pool identity.
"""
import base64
import hashlib
import math

from .engine import canonical
from .market_archive import ArchiveError, MarketArchive, solana_address
from .raydium_cpmm import CPMM_PROGRAM_ID


SCHEMA = 'pumpwizard.raydium-cpmm-vault-replay/v1'
PARSER = 'raydium-cpmm-spl-vault/v1'
TOKEN_PROGRAMS = frozenset((
    'TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA',
    'TokenzQdBNbLqP5VEhdkAS6EPFLC1PHnBqCXEpPxuEb',
))
BASE58 = '123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'


class RaydiumVaultReplayError(ValueError):
    pass


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RaydiumVaultReplayError('Invalid ' + name)
    return value


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RaydiumVaultReplayError('Invalid ' + name)
    return float(value)


def _base58(data):
    number = int.from_bytes(data, 'big')
    result = ''
    while number:
        number, remainder = divmod(number, 58)
        result = BASE58[remainder] + result
    return '1' * (len(data) - len(data.lstrip(b'\0'))) + (result or '')


def _vault(value, expected_mint, label):
    if not isinstance(value, dict):
        raise RaydiumVaultReplayError('Missing ' + label + ' vault')
    address = solana_address(value.get('address'), label + '_vault')
    owner = solana_address(value.get('owner'), label + '_vault_owner')
    if owner not in TOKEN_PROGRAMS:
        raise RaydiumVaultReplayError('Unsupported ' + label + ' vault owner')
    encoded = value.get('data_base64')
    if not isinstance(encoded, str):
        raise RaydiumVaultReplayError('Missing ' + label + ' vault data')
    try:
        raw = base64.b64decode(encoded.encode('ascii'), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise RaydiumVaultReplayError('Invalid ' + label + ' vault data') from exc
    # The first 165 bytes of SPL Token and Token-2022 accounts share this base
    # account layout: mint [0:32], amount u64 LE [64:72], initialized state [108].
    if len(raw) < 165 or _base58(raw[:32]) != expected_mint or raw[108] not in (1, 2):
        raise RaydiumVaultReplayError('Invalid ' + label + ' vault account')
    return {'address': address, 'mint': expected_mint,
            'amount': int.from_bytes(raw[64:72], 'little'),
            'owner_program': owner,
            'data_sha256': hashlib.sha256(raw).hexdigest()}


def _state(capture, archive):
    if not isinstance(capture, dict):
        raise RaydiumVaultReplayError('Capture must be an object')
    pool_id = capture.get('pool_id')
    pool = archive.get_pool(pool_id) if isinstance(pool_id, str) else None
    if (not pool or pool.get('dex') != 'raydium' or pool.get('pool_kind') != 'constant_product'
            or pool.get('program_id') != CPMM_PROGRAM_ID):
        raise RaydiumVaultReplayError('Capture references an unknown Raydium CPMM pool')
    commitment = capture.get('commitment')
    if commitment not in ('processed', 'confirmed', 'finalized'):
        raise RaydiumVaultReplayError('Invalid commitment')
    raw = canonical(capture)
    base = _vault(capture.get('base_vault'), pool['base_mint'], 'base')
    quote = _vault(capture.get('quote_vault'), pool['quote_mint'], 'quote')
    if base['address'] == quote['address']:
        raise RaydiumVaultReplayError('Vault addresses must differ')
    return {'pool_id': pool_id, 'slot': _integer(capture.get('slot'), 'slot', 1),
            'write_version': _integer(capture.get('write_version'), 'write_version'),
            'commitment': commitment, 'observed_at': _time(capture.get('observed_at'), 'observed_at'),
            'parser': PARSER,
            'state': {'base_vault': base, 'quote_vault': quote,
                      'capture_sha256': hashlib.sha256(raw.encode()).hexdigest()}}


def ingest_vault_recording(recording, archive):
    """Replay one bounded saved recording, atomically appending only pool states."""
    if not isinstance(archive, MarketArchive):
        raise TypeError('archive must be a MarketArchive')
    if not isinstance(recording, dict) or recording.get('schema') != SCHEMA:
        raise RaydiumVaultReplayError('Unsupported vault recording schema')
    captures = recording.get('captures')
    if not isinstance(captures, list) or not captures or len(captures) > 100:
        raise RaydiumVaultReplayError('Recording must contain 1 to 100 captures')
    try:
        raw = canonical(recording)
    except (TypeError, ValueError) as exc:
        raise RaydiumVaultReplayError('Recording is not canonical JSON') from exc
    states = [_state(capture, archive) for capture in captures]
    try:
        appended = archive.add_pool_states(states)
    except ArchiveError as exc:
        raise RaydiumVaultReplayError('Archive refused pool states: ' + str(exc)) from exc
    report = {'schema': SCHEMA, 'mode': 'shadow_vault_state_replay',
              'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'raw': recording,
              'state_ids': [{'pool_id': state['pool_id'], 'slot': state['slot'],
                             'write_version': state['write_version']} for state in states],
              'appended_pool_states': appended, 'archive_manifest': archive.verify()}
    report['sha256'] = hashlib.sha256(canonical(report).encode()).hexdigest()
    return report
