"""Read-only parser for saved Raydium CPMM AmmConfig account captures."""
import base64
import hashlib
import math

from .engine import canonical
from .market_archive import MarketArchive, solana_address
from .raydium_cpmm import CPMM_PROGRAM_ID


SCHEMA = 'pumpwizard.raydium-cpmm-config-replay/v1'
PARSER = 'raydium-cpmm-amm-config/v1'
FEE_DENOMINATOR = 1_000_000
ACCOUNT_LENGTH = 236
ACCOUNT_DISCRIMINATOR = hashlib.sha256(b'account:AmmConfig').digest()[:8]


class RaydiumConfigReplayError(ValueError):
    pass


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RaydiumConfigReplayError('Invalid ' + name)
    return value


def _time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RaydiumConfigReplayError('Invalid observed_at')
    return float(value)


def _capture(value, archive):
    if not isinstance(value, dict):
        raise RaydiumConfigReplayError('Config capture must be an object')
    pool_id = value.get('pool_id')
    pool = archive.get_pool(pool_id) if isinstance(pool_id, str) else None
    if not pool or pool.get('program_id') != CPMM_PROGRAM_ID:
        raise RaydiumConfigReplayError('Config capture references an unknown Raydium CPMM pool')
    address = solana_address(value.get('address'), 'config_address')
    configuration = pool.get('configuration')
    if not isinstance(configuration, dict) or configuration.get('cpmm_config') != address:
        raise RaydiumConfigReplayError('Config address does not match archived pool')
    if value.get('owner') != CPMM_PROGRAM_ID:
        raise RaydiumConfigReplayError('Unexpected config account owner')
    if value.get('commitment') not in ('processed', 'confirmed', 'finalized'):
        raise RaydiumConfigReplayError('Invalid commitment')
    try:
        raw = base64.b64decode(value.get('data_base64', '').encode('ascii'), validate=True)
    except (AttributeError, UnicodeEncodeError, ValueError) as exc:
        raise RaydiumConfigReplayError('Invalid config account data') from exc
    if len(raw) != ACCOUNT_LENGTH or raw[:8] != ACCOUNT_DISCRIMINATOR or raw[9] not in (0, 1):
        raise RaydiumConfigReplayError('Unexpected CPMM config account layout')
    values = {'trade_fee_rate': int.from_bytes(raw[12:20], 'little'),
              'protocol_fee_rate': int.from_bytes(raw[20:28], 'little'),
              'fund_fee_rate': int.from_bytes(raw[28:36], 'little'),
              'creator_fee_rate': int.from_bytes(raw[108:116], 'little')}
    if (values['trade_fee_rate'] + values['creator_fee_rate'] >= FEE_DENOMINATOR
            or values['protocol_fee_rate'] > FEE_DENOMINATOR
            or values['fund_fee_rate'] > FEE_DENOMINATOR
            or values['protocol_fee_rate'] + values['fund_fee_rate'] > FEE_DENOMINATOR):
        raise RaydiumConfigReplayError('Invalid CPMM fee rates')
    capture_raw = canonical(value)
    return {'pool_id': pool_id, 'config_address': address,
            'slot': _integer(value.get('slot'), 'slot', 1),
            'write_version': _integer(value.get('write_version'), 'write_version'),
            'commitment': value['commitment'], 'observed_at': _time(value.get('observed_at')),
            'parser': PARSER, 'index': int.from_bytes(raw[10:12], 'little'),
            'disable_create_pool': bool(raw[9]), 'fees': values,
            'account_sha256': hashlib.sha256(raw).hexdigest(),
            'capture_sha256': hashlib.sha256(capture_raw.encode()).hexdigest()}


def ingest_config_recording(recording, archive):
    """Validate saved config captures and return a proof report without mutation."""
    if not isinstance(archive, MarketArchive):
        raise TypeError('archive must be a MarketArchive')
    if not isinstance(recording, dict) or recording.get('schema') != SCHEMA:
        raise RaydiumConfigReplayError('Unsupported config recording schema')
    captures = recording.get('captures')
    if not isinstance(captures, list) or not captures or len(captures) > 100:
        raise RaydiumConfigReplayError('Recording must contain 1 to 100 captures')
    try:
        raw = canonical(recording)
    except (TypeError, ValueError) as exc:
        raise RaydiumConfigReplayError('Recording is not canonical JSON') from exc
    parsed = [_capture(capture, archive) for capture in captures]
    report = {'schema': SCHEMA, 'mode': 'shadow_config_replay',
              'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'raw': recording,
              'configs': parsed, 'archive_manifest': archive.verify()}
    report['sha256'] = hashlib.sha256(canonical(report).encode()).hexdigest()
    return report
