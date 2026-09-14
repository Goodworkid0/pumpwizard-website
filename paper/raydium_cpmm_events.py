"""Offline replay parser for saved Raydium CPMM swap instructions.

The parser has no RPC client and is intentionally limited to the two documented
CPMM swap instruction encodings.  Its archived events describe instruction
intent, not transfers, fills, prices, or executable quotes.
"""
import base64
import hashlib
import math

from .engine import canonical
from .market_archive import ArchiveError, MarketArchive, event_id, solana_address
from .raydium_cpmm import CPMM_PROGRAM_ID
from .raydium_cpmm_vaults import TOKEN_PROGRAMS


SCHEMA = 'pumpwizard.raydium-cpmm-event-replay/v1'
PARSER = 'raydium-cpmm-idl-0.2.0/v1'
SWAP_BASE_INPUT = bytes((143, 190, 90, 218, 196, 30, 51, 222))
SWAP_BASE_OUTPUT = bytes((55, 217, 98, 86, 163, 74, 180, 173))
ACCOUNT_COUNT = 13


class RaydiumEventReplayError(ValueError):
    pass


def _integer(value, name, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise RaydiumEventReplayError('Invalid ' + name)
    return value


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RaydiumEventReplayError('Invalid ' + name)
    return float(value)


def _data(value):
    if not isinstance(value, str):
        raise RaydiumEventReplayError('Missing instruction data')
    try:
        result = base64.b64decode(value.encode('ascii'), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise RaydiumEventReplayError('Invalid instruction data') from exc
    if len(result) != 24:
        raise RaydiumEventReplayError('Unexpected swap instruction length')
    return result


def _accounts(value):
    if not isinstance(value, list) or len(value) != ACCOUNT_COUNT:
        raise RaydiumEventReplayError('Unexpected CPMM swap account count')
    return [solana_address(account, 'instruction_account') for account in value]


def _pool(archive, pool_address, config):
    # The archive ID includes the documented CPMM program.  A supplied pool
    # address is meaningful only when its immutable identity and config agree.
    from .market_archive import pool_id
    identity = pool_id(CPMM_PROGRAM_ID, pool_address)
    pool = archive.get_pool(identity)
    if (not pool or pool.get('dex') != 'raydium' or pool.get('pool_kind') != 'constant_product'
            or pool.get('program_id') != CPMM_PROGRAM_ID):
        raise RaydiumEventReplayError('Instruction references an unknown Raydium CPMM pool')
    configuration = pool.get('configuration')
    if not isinstance(configuration, dict) or configuration.get('cpmm_config') != config:
        raise RaydiumEventReplayError('Instruction config does not match archived pool')
    return identity, pool


def _event(transaction, instruction, instruction_index, archive):
    if not isinstance(instruction, dict):
        raise RaydiumEventReplayError('Instruction must be an object')
    if instruction.get('program_id') != CPMM_PROGRAM_ID:
        raise RaydiumEventReplayError('Unexpected CPMM program')
    accounts = _accounts(instruction.get('accounts'))
    identity, pool = _pool(archive, accounts[3], accounts[2])
    if accounts[8] not in TOKEN_PROGRAMS or accounts[9] not in TOKEN_PROGRAMS:
        raise RaydiumEventReplayError('Unsupported token program')
    input_mint, output_mint = accounts[10], accounts[11]
    if {input_mint, output_mint} != {pool['base_mint'], pool['quote_mint']}:
        raise RaydiumEventReplayError('Instruction mints do not match archived pool')
    if accounts[6] == accounts[7]:
        raise RaydiumEventReplayError('Input and output vault must differ')
    data = _data(instruction.get('data_base64'))
    arguments = {'amount_1': int.from_bytes(data[8:16], 'little'),
                 'amount_2': int.from_bytes(data[16:24], 'little')}
    if data[:8] == SWAP_BASE_INPUT:
        variant = 'swap_base_input'
        intent = {'amount_in': arguments['amount_1'], 'minimum_amount_out': arguments['amount_2']}
    elif data[:8] == SWAP_BASE_OUTPUT:
        variant = 'swap_base_output'
        intent = {'max_amount_in': arguments['amount_1'], 'amount_out': arguments['amount_2']}
    else:
        raise RaydiumEventReplayError('Unsupported Raydium CPMM instruction discriminator')
    signature = transaction.get('signature')
    try:
        event_id(signature, instruction_index)
    except ArchiveError as exc:
        raise RaydiumEventReplayError(str(exc)) from exc
    occurred_at = _time(transaction.get('occurred_at'), 'occurred_at')
    received_at = _time(transaction.get('received_at'), 'received_at')
    if received_at < occurred_at:
        raise RaydiumEventReplayError('received_at precedes occurred_at')
    tx_raw = canonical(transaction)
    instruction_raw = canonical(instruction)
    return {'pool_id': identity, 'signature': signature, 'instruction_index': instruction_index,
            'slot': _integer(transaction.get('slot'), 'slot', 1),
            'event_kind': 'swap', 'commitment': transaction.get('commitment'),
            'occurred_at': occurred_at, 'received_at': received_at, 'parser': PARSER,
            'payload': {'instruction': variant,
                        'direction': 'base_to_quote' if input_mint == pool['base_mint'] else 'quote_to_base',
                        'input_mint': input_mint, 'output_mint': output_mint,
                        'input_vault': accounts[6], 'output_vault': accounts[7],
                        'intent': intent,
                        'instruction_sha256': hashlib.sha256(instruction_raw.encode()).hexdigest(),
                        'transaction_sha256': hashlib.sha256(tx_raw.encode()).hexdigest()}}


def ingest_event_recording(recording, archive):
    """Parse one saved bounded recording and atomically append CPMM swap events."""
    if not isinstance(archive, MarketArchive):
        raise TypeError('archive must be a MarketArchive')
    if not isinstance(recording, dict) or recording.get('schema') != SCHEMA:
        raise RaydiumEventReplayError('Unsupported event recording schema')
    transactions = recording.get('transactions')
    if not isinstance(transactions, list) or not transactions or len(transactions) > 100:
        raise RaydiumEventReplayError('Recording must contain 1 to 100 transactions')
    try:
        raw = canonical(recording)
    except (TypeError, ValueError) as exc:
        raise RaydiumEventReplayError('Recording is not canonical JSON') from exc
    events = []
    for transaction in transactions:
        if not isinstance(transaction, dict) or transaction.get('success') is not True:
            raise RaydiumEventReplayError('Transaction must be a successful object')
        if transaction.get('commitment') not in ('processed', 'confirmed', 'finalized'):
            raise RaydiumEventReplayError('Invalid commitment')
        instructions = transaction.get('instructions')
        if not isinstance(instructions, list) or not instructions or len(instructions) > 100:
            raise RaydiumEventReplayError('Transaction must contain 1 to 100 instructions')
        for index, instruction in enumerate(instructions):
            events.append(_event(transaction, instruction, index, archive))
    try:
        appended = archive.add_events(events)
    except ArchiveError as exc:
        raise RaydiumEventReplayError('Archive refused market events: ' + str(exc)) from exc
    report = {'schema': SCHEMA, 'mode': 'shadow_event_replay',
              'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'raw': recording,
              'event_ids': [event['id'] if 'id' in event else event_id(event['signature'], event['instruction_index'])
                            for event in events],
              'appended_events': appended, 'archive_manifest': archive.verify()}
    report['sha256'] = hashlib.sha256(canonical(report).encode()).hexdigest()
    return report
