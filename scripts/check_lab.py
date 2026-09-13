"""Read-only operations probe. Emits JSON and exits nonzero when attention is needed."""
import argparse
import json
from pathlib import Path
import shutil
import time
import urllib.error
import urllib.request


def backup_age(root, now):
    completed = []
    for path in root.glob('*/manifest.json'):
        if path.is_symlink() or path.parent.is_symlink(): continue
        try:
            if path.stat().st_size > 5_000_000: continue
            manifest = json.loads(path.read_text())
            at = manifest.get('completed_at')
            if (manifest.get('schema') == 'pumpwizard.backup/v1' and manifest.get('files')
                    and isinstance(at, (int, float)) and 0 < at <= now):
                completed.append(at)
        except (OSError, ValueError): continue
    return round(now-max(completed)) if completed else None


def retained_backup_age(root, now):
    """Age of the retained recovery copy; not an expensive integrity recheck."""
    try:
        record=root/'retention-last.json'
        if record.is_symlink() or record.stat().st_size>5_000_000: return None
        state=json.loads(record.read_text()); copy=Path(state['kept_copy'])
        if state.get('status')!='complete' or root.resolve() not in copy.resolve().parents: return None
        if any(p.is_symlink() for p in (copy,*copy.parents) if p==root or root in p.parents): return None
        manifest=copy/'manifest.json'
        if manifest.is_symlink() or manifest.stat().st_size>5_000_000: return None
        data=json.loads(manifest.read_text()); at=data.get('completed_at')
        if data.get('schema')!='pumpwizard.backup/v1' or not data.get('files'): return None
        if at!=state.get('snapshot_completed_at') or not isinstance(at,(int,float)) or not 0<at<=now: return None
        return round(now-at)
    except (OSError,ValueError,KeyError,TypeError): return None


def remote_backup_age(root, now):
    completed = []
    for path in root.glob('*/remote-receipt.json'):
        if path.is_symlink() or path.parent.is_symlink(): continue
        try:
            if path.stat().st_size > 5_000_000: continue
            receipt = json.loads(path.read_text())
            snapshot, verified = receipt.get('snapshot_completed_at'), receipt.get('verified_at')
            if (receipt.get('schema') == 'pumpwizard.ots-job/v1' and receipt.get('status') == 'verified'
                    and receipt.get('file_id') and receipt.get('archive_sha256') and receipt.get('key_id')
                    and isinstance(snapshot,(int,float)) and isinstance(verified,(int,float))
                    and 0 < snapshot <= verified <= now):
                completed.append(snapshot)
        except (OSError,ValueError): continue
    # Age the source snapshot, not a recently uploaded copy of old data.
    return round(now-max(completed)) if completed else None


def telegram_backup_age(root, now):
    completed=[]
    for path in root.glob('*/telegram-receipt.json'):
        if path.is_symlink() or path.parent.is_symlink(): continue
        try:
            if path.stat().st_size>5_000_000: continue
            receipt=json.loads(path.read_text())
            snapshot,verified=receipt.get('snapshot_completed_at'),receipt.get('verified_at')
            if (receipt.get('schema')=='pumpwizard.telegram-receipt/v1' and receipt.get('status')=='verified'
                    and receipt.get('purpose')=='production' and receipt.get('manifest_message_id')
                    and receipt.get('parts') and receipt.get('key_id')
                    and isinstance(snapshot,(int,float)) and isinstance(verified,(int,float)) and 0<snapshot<=verified<=now):
                completed.append(snapshot)
        except (OSError,ValueError,AttributeError): continue
    return round(now-max(completed)) if completed else None


def check(url, data, backups=None, max_backup_age=86400, minimum_free=10*1024**3, ots_jobs=None, telegram_jobs=None):
    checks, details = {}, {}
    try:
        request=urllib.request.Request(url,headers={'User-Agent':'PumpWizard-ReleaseCheck/1.0','Accept':'application/json'})
        try: response = urllib.request.urlopen(request, timeout=8)
        except urllib.error.HTTPError as error:
            if error.code != 503: raise
            response = error
        with response:
            raw = response.read(32769)
            if len(raw)>32768: raise ValueError('Oversized health response')
            health = json.loads(raw)
            flags = health.get('checks', {})
            checks['readiness'] = (getattr(response, 'status', 200) == 200 and health.get('status') == 'ok'
                                   and bool(flags) and all(v is True for v in flags.values()))
            details['service'] = health
    except (OSError, ValueError, AttributeError):
        checks['readiness'] = False
        # Do not log URLs/provider errors; they can contain private credentials.
        details['service'] = {'status': 'unreachable_or_degraded'}
    usage = shutil.disk_usage(data)
    details['disk_free_bytes'] = usage.free
    details['disk_free_fraction'] = round(usage.free/usage.total, 4)
    checks['disk'] = usage.free >= minimum_free and usage.free/usage.total >= .10
    if backups is not None:
        age = backup_age(backups, time.time())
        details['backup_age_seconds'] = age
        checks['backup_recent'] = age is not None and age <= max_backup_age
    if ots_jobs is not None:
        age = remote_backup_age(ots_jobs,time.time())
        details['remote_snapshot_age_seconds'] = age
        checks['remote_backup_recent'] = age is not None and age <= max_backup_age
    if telegram_jobs is not None:
        age=telegram_backup_age(telegram_jobs,time.time())
        details['telegram_snapshot_age_seconds']=age
        checks['telegram_backup_recent']=age is not None and age<=max_backup_age
    return {'status': 'ok' if all(checks.values()) else 'attention', 'checked_at': time.time(),
            'checks': checks, 'details': details,
            'backup_scope': 'Completed manifest age only; run backup_lab.py verify for file integrity' if backups else 'Local backup monitoring not configured',
            'remote_backup_scope': ('Verified standalone Telegram receipt age only; not a current Telegram read-back' if telegram_jobs
                                    else 'Verified OTS receipt age only; not a current Telegram read-back' if ots_jobs
                                    else 'Remote backup monitoring not configured')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://127.0.0.1:8765/api/public/health')
    parser.add_argument('--data', type=Path, default=Path(__file__).resolve().parents[1]/'data')
    parser.add_argument('--backup-root', type=Path)
    parser.add_argument('--ots-jobs', type=Path, help='Parent of OTS job directories with remote verification receipts')
    parser.add_argument('--telegram-jobs', type=Path, help='Parent of standalone Telegram backup jobs')
    parser.add_argument('--max-backup-age', type=int, default=86400)
    args = parser.parse_args()
    try: result = check(args.url, args.data, args.backup_root, args.max_backup_age, ots_jobs=args.ots_jobs,telegram_jobs=args.telegram_jobs)
    except OSError: result = {'status': 'attention', 'error': 'Monitoring path unavailable'}
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result['status'] == 'ok' else 1)


if __name__ == '__main__': main()
