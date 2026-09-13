"""Lab health and backup alerts; no database writes or shared Telegram session."""
import argparse
import json
import os
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lab_backup.common import load, save, lock
from scripts.check_lab import check, retained_backup_age
from scripts.backup_lab import plan
from lab_backup.capacity import budget


def alert_transition(previous,checks,now,notify):
    """Three consecutive identical failed checks, hourly reminders, one recovery."""
    failures=sorted(k for k,v in checks.items() if v is not True)
    consecutive=previous.get('consecutive',0)+1 if failures==previous.get('failures') else 1
    state={**previous,'failures':failures,'consecutive':consecutive,'checked_at':now}
    notified=previous.get('notified_failures',[])
    if failures and consecutive>=3 and (failures!=notified or now-previous.get('notified_at',0)>=3600):
        notify('PumpWizard needs attention: '+', '.join(failures)+'. Existing data and backups were retained.')
        state.update(notified_failures=failures,notified_at=now)
    elif not failures and notified:
        notify('PumpWizard recovered: all enabled monitoring checks are healthy.')
        state.update(notified_failures=[],notified_at=now)
    return state


def collect(config,now):
    if config.get('backup_enabled',True) is False:
        result=check(config.get('health_url','http://127.0.0.1:8765/api/public/health'),ROOT/'data')
        result['details']['backups']={'status':'disabled_by_operator',
            'reason':config.get('backup_disabled_reason','Temporarily disabled'),
            'disabled_at':config.get('backup_disabled_at')}
        result['backup_scope']='Backup jobs and backup-specific checks disabled by operator; existing backups retained'
        result['remote_backup_scope']='Telegram backups disabled by operator'
        return result
    root=Path(config['backup_root'])
    result=check(config.get('health_url','http://127.0.0.1:8765/api/public/health'),ROOT/'data',root/'snapshots',
                 max_backup_age=36*3600,telegram_jobs=root/'jobs')
    if config.get('local_retention')=='latest_verified' and (root/'retention-last.json').exists():
        age=retained_backup_age(root,now)
        result['details']['backup_age_seconds']=age
        result['checks']['backup_recent']=age is not None and age<=36*3600
    import shutil
    usage=shutil.disk_usage(root)
    result['checks']['backup_disk']=usage.free>=max(config['minimum_free_bytes'],usage.total*.10)
    sizing=plan(ROOT,root/'snapshots')
    capacity=budget(sizing['estimated_source_bytes'],usage,config['minimum_free_bytes'],
                    config.get('restore_first_backup') and not (root/'production-drill.json').exists())
    result['details']['next_full_backup_capacity']=capacity
    result['checks']['next_backup_capacity']=capacity['sufficient']
    path=Path(config['session']).parent/'progress.json'
    if path.exists():
        progress=load(path)
        age=now-progress['updated_at']
        result['details']['transfer']={k:progress[k] for k in ('phase','bytes','total_bytes') if k in progress}
        result['details']['transfer']['age_seconds']=round(age)
        result['checks']['backup_progress']=(progress['phase']!='failed' and age>=0 and
            (progress['phase']=='complete' or age<=30*60))
    if config.get('restore_first_backup'):
        marker=root/'production-drill.json'
        result['checks']['production_restore_test']=(marker.exists() and load(marker).get('status')=='verified')
    result['status']='ok' if all(result['checks'].values()) else 'attention'
    return result


def main():
    os.umask(0o077)
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'data/telegram-backup/config.json')
    parser.add_argument('--notify',action='store_true',help='Send debounced alerts to the configured private channel')
    args=parser.parse_args()
    try:
        config=load(args.config,private=True)
        now=time.time()
        try: result=collect(config,now)
        except Exception: result={'status':'attention','checks':{'monitor_inputs':False},'checked_at':now}
        if args.notify:
            if not config.get('notify'): raise ValueError('Notifications disabled')
            from lab_backup.transport import Transport
            transport=Transport(config)
            def notify(message):
                # Uses only Bot API; does not open or contend for the MTProto session.
                transport.bot_api('sendMessage',{'chat_id':transport.channel_id,'text':message})
            with lock(args.config.parent/'monitor.lock'):
                path=args.config.parent/'monitor-state.json'
                previous=load(path,private=True) if path.exists() else {}
                save(path,alert_transition(previous,result['checks'],now,notify))
        print(json.dumps(result,indent=2))
        return 0 if result['status']=='ok' else 1
    except Exception:
        print(json.dumps({'status':'attention','error':'Monitoring or alert delivery failed; no credentials logged'}))
        return 1


if __name__=='__main__': raise SystemExit(main())
