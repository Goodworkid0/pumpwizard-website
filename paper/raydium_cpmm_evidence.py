"""Seal a bounded, offline Raydium CPMM validation corpus from saved evidence."""
import hashlib
import math
import re
import tempfile
from pathlib import Path

from .engine import canonical
from .market_archive import ArchiveError, MarketArchive
from .raydium_cpmm import RaydiumCatalogError, ingest_catalog
from .raydium_cpmm_config import RaydiumConfigReplayError, SCHEMA as CONFIG_SCHEMA
from .raydium_cpmm_events import RaydiumEventReplayError, ingest_event_recording
from .raydium_cpmm_reconcile import SCHEMA as RECONCILIATION_SCHEMA
from .raydium_cpmm_validation import (SCHEMA as VALIDATION_SCHEMA,
                                      RaydiumShadowValidationError, validate)
from .raydium_cpmm_vaults import RaydiumVaultReplayError, ingest_vault_recording


SCHEMA = 'pumpwizard.raydium-cpmm-evidence-corpus/v1'
CATALOG_SOURCE = 'raydium-api-v3-fixed-catalog'
MAX_RECORDINGS = 100
LABEL = re.compile(r'[a-z0-9][a-z0-9._-]{0,79}\Z')


class RaydiumEvidenceError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _time(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise RaydiumEvidenceError('Invalid ' + name)
    return float(value)


def _items(value, name):
    if not isinstance(value, list) or not value or len(value) > MAX_RECORDINGS:
        raise RaydiumEvidenceError(name + ' must contain 1 to ' + str(MAX_RECORDINGS) + ' recordings')
    return value


def _capture(value):
    if not isinstance(value, dict) or value.get('network') != 'solana-mainnet-beta':
        raise RaydiumEvidenceError('Corpus must identify Solana mainnet-beta evidence')
    label = value.get('source_label')
    if not isinstance(label, str) or not LABEL.fullmatch(label):
        raise RaydiumEvidenceError('Invalid capture source_label')
    return {'network': value['network'], 'source_label': label,
            'captured_at': _time(value.get('captured_at'), 'captured_at')}


def _catalog(value):
    if not isinstance(value, dict) or value.get('source') != CATALOG_SOURCE or not isinstance(value.get('page'), dict):
        raise RaydiumEvidenceError('Corpus needs one fixed Raydium catalog page')
    return value['page'], _time(value.get('captured_at'), 'catalog captured_at')


def _finalized_cases(validation, archive):
    coverage = {'pools': set(), 'directions': set(), 'creator_fee_modes': set()}
    for item in validation.get('cases', []):
        request = item.get('quote_request') if isinstance(item, dict) else None
        if not isinstance(request, dict):
            raise RaydiumEvidenceError('Validation case is malformed')
        proof = request.get('reconciliation_report')
        raw = proof.get('raw') if isinstance(proof, dict) else None
        if not isinstance(raw, dict) or raw.get('schema') != RECONCILIATION_SCHEMA:
            raise RaydiumEvidenceError('Validation case lacks reproducible reconciliation input')
        event_id = item.get('event_id')
        event = archive.get_event(event_id) if isinstance(event_id, str) else None
        if not event or event.get('commitment') != 'finalized':
            raise RaydiumEvidenceError('Corpus event is not finalized')
        matching = [comparison for comparison in raw.get('comparisons', [])
                    if isinstance(comparison, dict) and comparison.get('event_id') == event_id]
        if len(matching) != 1:
            raise RaydiumEvidenceError('Validation case needs exactly one matching reconciliation comparison')
        for locator in (matching[0].get('before'), matching[0].get('after')):
            if not isinstance(locator, dict):
                raise RaydiumEvidenceError('Reconciliation comparison lacks a state locator')
            state = archive.get_pool_state(request.get('pool_id'), locator.get('slot'), locator.get('write_version'))
            if not state or state.get('commitment') != 'finalized':
                raise RaydiumEvidenceError('Corpus pool state is not finalized')
        config = request.get('config_recording')
        captures = config.get('captures') if isinstance(config, dict) and config.get('schema') == CONFIG_SCHEMA else None
        if not isinstance(captures, list) or len(captures) != 1 or captures[0].get('commitment') != 'finalized':
            raise RaydiumEvidenceError('Corpus config snapshot is not finalized')
        account = archive.get_pool_state(request['pool_id'], request['state']['slot'], request['state']['write_version'])
        pool_account = account.get('state', {}).get('pool_account') if account else None
        payload = event.get('payload', {})
        if not isinstance(pool_account, dict) or not isinstance(payload, dict):
            raise RaydiumEvidenceError('Corpus case lacks decoded pool or event evidence')
        coverage['pools'].add(request['pool_id'])
        coverage['directions'].add(payload.get('direction'))
        coverage['creator_fee_modes'].add(str(pool_account.get('creator_fee_mode')) + ':' +
                                          ('enabled' if pool_account.get('creator_fee_enabled') else 'disabled'))
    return {key: sorted(value) for key, value in coverage.items()}


def seal(recording):
    """Replay saved evidence in a temporary archive and emit a review-only proof."""
    if not isinstance(recording, dict) or recording.get('schema') != SCHEMA:
        raise RaydiumEvidenceError('Unsupported evidence corpus schema')
    capture = _capture(recording.get('capture'))
    page, catalog_at = _catalog(recording.get('catalog'))
    event_recordings = _items(recording.get('event_recordings'), 'event_recordings')
    vault_recordings = _items(recording.get('vault_recordings'), 'vault_recordings')
    validation = recording.get('validation')
    if not isinstance(validation, dict) or validation.get('schema') != VALIDATION_SCHEMA:
        raise RaydiumEvidenceError('Corpus needs a compatible validation recording')
    try:
        raw = canonical(recording)
    except (TypeError, ValueError) as exc:
        raise RaydiumEvidenceError('Evidence corpus is not canonical JSON') from exc
    try:
        with tempfile.TemporaryDirectory(prefix='pumpwizard-cpmm-evidence-') as directory:
            archive = MarketArchive(Path(directory) / 'evidence.sqlite')
            try:
                catalog = ingest_catalog(page, archive, catalog_at, CATALOG_SOURCE)
                events = [ingest_event_recording(item, archive) for item in event_recordings]
                vaults = [ingest_vault_recording(item, archive) for item in vault_recordings]
                validation_report = validate(validation, archive)
                coverage = _finalized_cases(validation, archive)
                manifest = archive.verify()
            finally:
                archive.close()
    except (ArchiveError, RaydiumCatalogError, RaydiumConfigReplayError, RaydiumEventReplayError,
            RaydiumVaultReplayError, RaydiumShadowValidationError) as exc:
        raise RaydiumEvidenceError('Corpus replay failed: ' + str(exc)) from exc
    report = {'schema': SCHEMA, 'mode': 'sealed_historical_evidence_corpus',
              'raw_sha256': hashlib.sha256(raw.encode()).hexdigest(), 'raw': recording,
              'capture': capture,
              'artifacts': {'catalog_sha256': catalog['sha256'],
                            'event_recording_sha256': [_hash(item) for item in event_recordings],
                            'vault_recording_sha256': [_hash(item) for item in vault_recordings],
                            'validation_sha256': validation_report['sha256']},
              'coverage': coverage, 'validation': validation_report['summary'],
              'archive_manifest': manifest,
              'review': {'ready_for_human_review': validation_report['summary']['passed'],
                         'simulation_eligible': False,
                         'reason': 'A sealed corpus is evidence for review, never a production enablement.'},
              'limitations': ['offline replay only', 'finalized evidence required', 'classic SPL Token only',
                              'not executable', 'does not enable simulation or public P/L']}
    report['sha256'] = _hash(report)
    return report
