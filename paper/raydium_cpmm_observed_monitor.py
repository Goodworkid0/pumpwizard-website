"""Bounded collection loop for the observed-only Raydium CPMM data lane.

This module creates neither a paper fill nor a strategy decision.  It joins the
read-only Raydium identity archive to the separate observed log and reports only
the continuity/freshness that standard RPC can support.
"""
import math
import time

from .market_archive import MarketArchive
from .observed_market_log import ObservedLogError, ObservedMarketLog
from .raydium_cpmm_observed import RaydiumObservedError, collect


class RaydiumObservedMonitorError(ValueError):
    pass


def _cadence(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 15:
        raise RaydiumObservedMonitorError('Observed monitor cadence must be at least 15 seconds')
    return float(value)


def _health(snapshot, previous, cadence_seconds, checked_at):
    checked_at = float(checked_at)
    age = max(0.0, checked_at - snapshot['observed_at'])
    result = {'assurance': 'observed_simulation_only',
              'snapshot_age_seconds': age,
              'commitment': snapshot['commitment'],
              'context_slot': snapshot['context_slot'],
              'write_version': 'unavailable_not_invented',
              'sequence': snapshot['sequence'],
              'sequence_status': 'initial' if previous is None else 'continuous'}
    if previous is None:
        result.update(previous_sequence=None, observed_gap_seconds=None, cadence_status='baseline')
        return result
    gap = snapshot['observed_at'] - previous['observed_at']
    result.update(previous_sequence=previous['sequence'], observed_gap_seconds=gap,
                  cadence_status='gap_detected' if gap > cadence_seconds * 1.5 else 'within_interval')
    return result


def capture_once(pool_id, archive, log, commitment='finalized', cadence_seconds=60,
                 observed_at=None, checked_at=None, endpoint=None, request=None):
    """Capture, append, and assess one snapshot without changing any trading state."""
    if not isinstance(archive, MarketArchive) or not archive.view_only:
        raise RaydiumObservedMonitorError('Observed monitor requires a read-only market archive')
    if not isinstance(log, ObservedMarketLog) or log.view_only:
        raise RaydiumObservedMonitorError('Observed monitor requires a writable observed market log')
    cadence_seconds = _cadence(cadence_seconds)
    previous_rows = log.recent(pool_id, limit=1)
    previous = previous_rows[0] if previous_rows else None
    sequence = log.next_sequence(pool_id)
    kwargs = {'observed_at': observed_at, 'endpoint': endpoint}
    if request is not None:
        kwargs['request'] = request
    try:
        snapshot = collect(pool_id, archive, sequence, commitment, **kwargs)
        added = log.append(snapshot)
    except (RaydiumObservedError, ObservedLogError) as exc:
        raise RaydiumObservedMonitorError(str(exc)) from exc
    if not added:
        raise RaydiumObservedMonitorError('Observed monitor refused a duplicate capture')
    checked_at = time.time() if checked_at is None else checked_at
    health = _health(snapshot, previous, cadence_seconds, checked_at)
    return {'mode': 'observed_standard_rpc_monitor', 'pool_id': pool_id,
            'snapshot_sha256': snapshot['sha256'], 'health': health,
            'limitations': snapshot['limitations']}
