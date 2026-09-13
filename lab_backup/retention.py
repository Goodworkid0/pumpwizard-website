"""Opt-in local retention. Never deletes Telegram messages or live ledgers."""
from pathlib import Path
import time

from scripts.backup_lab import digest, verify
from .common import BackupError, load, save
from .store import validate_receipt


def prune(config, *, apply=False):
    if config.get('local_retention')!='latest_verified':
        return {'status':'disabled','removed_bytes':0}
    raw=Path(config['backup_root'])
    if raw.is_symlink() or not raw.is_absolute() or len(raw.parts)<4:
        raise BackupError('Retention requires an explicit application backup directory')
    root=raw.resolve()
    def safe(path):
        if root not in path.parents or any(p.is_symlink() for p in (path,*path.parents) if p==root or root in p.parents):
            raise BackupError('Unsafe retention path')
        return path
    records=[]
    for path in sorted(root.glob('jobs/*/telegram-receipt.json')):
        safe(path); receipt=load(path)
        if receipt.get('purpose')!='production': continue
        validate_receipt(receipt)
        job=load(safe(path.parent/'job.json'))
        if job.get('status')!='verified' or job.get('id')!=receipt['id']:
            continue
        records.append((path.parent,receipt))
    marker=root/'production-drill.json'
    if not records or not marker.exists(): return {'status':'awaiting_verified_recovery','removed_bytes':0}
    drill=load(safe(marker))
    job,latest=max(records,key=lambda pair:pair[1]['snapshot_completed_at'])
    if drill.get('status')!='verified' or drill.get('key_id')!=latest['key_id']:
        raise BackupError('Retention requires a verified recovery drill with the current key')
    keep=root/'snapshots'/job.name
    if drill.get('id')==latest['id']:
        candidate=Path(drill['destination'])
        if candidate.parent!=root or not candidate.name.startswith('production-restore-'):
            raise BackupError('Unexpected recovery directory')
        if (candidate/'manifest.json').exists(): keep=candidate
    safe(keep)
    manifest=verify(keep)
    expected=latest['members']['manifest.json']['sha256']
    if digest(keep/'manifest.json')!=expected or manifest['completed_at']!=latest['snapshot_completed_at']:
        raise BackupError('Latest local copy does not match the verified Telegram receipt')
    # Resolve an exact allowlist before deleting anything. Missing older files
    # are tolerated so an interrupted cleanup can resume; changed files are not.
    targets=[]; retire=[]
    def target(path,size,sha):
        safe(path)
        if not path.exists(): return
        if not path.is_file() or path.stat().st_size!=size or digest(path)!=sha:
            raise BackupError('A local backup candidate changed; retention stopped')
        targets.append((path,size,sha))
    by_id={r['id']:r for _,r in records}
    snapshots=[(root/'snapshots'/j.name,r) for j,r in records]
    for folder in sorted(root.glob('production-restore-*')):
        metadata=folder/'telegram-restore.json'
        if not metadata.exists(): continue  # never prune incomplete/unidentified restores
        restored=load(safe(metadata)); receipt=by_id.get(restored.get('id'))
        if receipt and restored.get('archive_sha256')==receipt['archive_sha256']:
            snapshots.append((folder,receipt))
            target(folder/'download.tar.gz',receipt['archive_bytes'],receipt['archive_sha256'])
    for old_job,receipt in records:
        target(old_job/'archive.tar.gz',receipt['archive_bytes'],receipt['archive_sha256'])
    for folder,receipt in snapshots:
        if folder==keep or not (folder/'manifest.json').exists(): continue
        safe(folder)
        old_manifest=folder/'manifest.json'
        if digest(old_manifest)!=receipt['members']['manifest.json']['sha256']:
            raise BackupError('Older snapshot manifest differs from its Telegram receipt')
        retired=folder/'retired-manifest.json'
        if retired.exists(): raise BackupError('Ambiguous retired snapshot metadata')
        for name,member in receipt['members'].items():
            if name!='manifest.json': target(folder/name,member['bytes'],member['sha256'])
        retire.append((old_manifest,retired))
    unique={str(path):(path,size,sha) for path,size,sha in targets}
    result={'status':'planned','kept_copy':str(keep),'receipt_id':latest['id'],
            'snapshot_completed_at':latest['snapshot_completed_at'],
            'candidate_bytes':sum(size for _,size,_ in unique.values()),
            'candidate_files':len(unique),'removed_bytes':0,'telegram_deletions':0,'checked_at':time.time()}
    if not apply: return result
    # Caller holds the same operation lock as upload/restore. The retained copy
    # has already passed hashes, SQLite integrity and complete table counts.
    save(root/'retention-plan.json',{**result,'paths':list(unique)})
    for path,size,sha in unique.values():
        safe(path)
        if not path.exists(): continue
        if path.stat().st_size!=size or digest(path)!=sha: raise BackupError('Retention candidate changed')
        path.unlink(); result['removed_bytes']+=size
    for source,destination in retire: source.rename(destination)
    result.update(status='complete',completed_at=time.time())
    save(root/'retention-last.json',result)
    return result
