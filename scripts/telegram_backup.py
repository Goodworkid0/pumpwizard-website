"""Standalone PumpWizard Telegram backup commands (no OTS service required)."""
import argparse
import asyncio
import datetime
import json
import os
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lab_backup.common import BackupError, load, lock
from lab_backup.common import save
from lab_backup.archive import prepare
from lab_backup.store import upload, restore, recover_manifest
from scripts.backup_lab import backup, verify, plan
from lab_backup.capacity import budget


def progress(config,phase):
    save(Path(config['session']).parent/'progress.json',{'phase':phase,'updated_at':time.time()})


async def first_restore_drill(config,transport,key,receipt):
    """One full production rehearsal, never overwrite a destination or the lab."""
    root=Path(config['backup_root']); marker=root/'production-drill.json'
    if marker.exists():
        record=load(marker)
        if record.get('status')=='verified' and record.get('key_id')==receipt['key_id']: return
        raise BackupError('Existing restore drill marker requires inspection')
    name='production-restore-'+datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    progress(config,'restore')
    recovered=await recover_manifest(transport,key,receipt['manifest_message_id'],root/(name+'-manifest'))
    destination=root/name
    await restore(recovered,transport,key,destination)
    save(marker,{'status':'verified','id':receipt['id'],'key_id':receipt['key_id'],
                 'archive_sha256':receipt['archive_sha256'],'verified_at':time.time(),'destination':str(destination)})
    print('Production Telegram restore drill verified.',flush=True)
    if config.get('notify'): await transport.notify('PumpWizard production restore drill verified. No live databases changed.')


async def probe(config):
    from lab_backup.transport import Transport
    async with Transport(config) as transport:
        print('Telegram ready: configured bot authenticated with an independent session; private channel; auto-delete off. No message sent.')


async def run(args,config):
    if config.get('backup_enabled',True) is False and args.command in ('daily','smoke','upload'):
        print('Telegram backups are disabled by operator configuration. No backup was started.',flush=True)
        return
    from lab_backup.transport import Transport
    key=bytes.fromhex(load(config['key'],private=True)['key'])
    if args.command=='probe': return await probe(config)
    if args.command in ('daily','smoke'):
        root=Path(config['backup_root'])
        if root.exists() and (root.is_symlink() or not root.is_dir()): raise BackupError('Invalid backup root')
        root.mkdir(mode=0o700,exist_ok=True)
        (root/'snapshots').mkdir(mode=0o700,exist_ok=True); (root/'jobs').mkdir(mode=0o700,exist_ok=True)
        if shutil.disk_usage(root).free<config['minimum_free_bytes']: raise BackupError('Backup disk is below free-space floor')
        stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
        name=('smoke-' if args.command=='smoke' else '')+stamp
        job=root/'jobs'/name
        if args.command=='daily':
            from lab_backup.retention import prune
            retention=prune(config,apply=True)
            if retention.get('removed_bytes'): print('Verified local retention reclaimed '+str(retention['removed_bytes'])+' bytes. All Telegram backups retained.',flush=True)
            # Resume the most recent unfinished production transfer; never create
            # a new full backup while the previous run has unverified parts.
            unfinished=[p.parent for p in (root/'jobs').glob('*/job.json')
                        if load(p).get('purpose','production')=='production' and load(p).get('status')!='verified']
            if unfinished:
                job=sorted(unfinished)[-1]
            elif config.get('restore_first_backup') and not (root/'production-drill.json').exists() and (
                    completed:=sorted(p.parent for p in (root/'jobs').glob('*/telegram-receipt.json')
                                      if load(p).get('purpose')=='production')):
                job=completed[-1]
            else:
                async with Transport(config): pass  # live channel check before expensive disk work
                sizing=plan(ROOT,root/'snapshots')
                usage=shutil.disk_usage(root)
                # Snapshot + pessimistic archive; first full drill retains both
                # the downloaded archive and extracted snapshot too.
                capacity=budget(sizing['estimated_source_bytes'],usage,config['minimum_free_bytes'],
                                config.get('restore_first_backup') and not (root/'production-drill.json').exists())
                if not capacity['sufficient']:
                    raise BackupError('Insufficient backup and recovery headroom; add storage or review approved retention')
                progress(config,'snapshot')
                print('Creating verified SQLite snapshot.',flush=True)
                registry=ROOT/'data/public-registry.json'
                runtime=json.loads(registry.read_text()).get('ledger_backend') if registry.exists() else 'sqlite'
                if runtime=='postgres':
                    from scripts.backup_postgres_runtime import backup_runtime
                    credentials=load(config['postgres_credentials'],private=True)
                    backup_runtime(ROOT,root/'snapshots'/name,credentials['dsn'],credentials['namespace'])
                else: backup(ROOT,root/'snapshots'/name)
                progress(config,'archive')
                print('Compressing and verifying snapshot archive.',flush=True)
                await asyncio.to_thread(prepare,root/'snapshots'/name,job)
        else:
            with tempfile.TemporaryDirectory(prefix='pumpwizard-smoke-') as temp:
                synthetic=Path(temp); (synthetic/'data').mkdir()
                con=sqlite3.connect(synthetic/'data/synthetic.sqlite')
                try:
                    con.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY, payload BLOB)')
                    con.execute('INSERT INTO evidence VALUES(1,?)',(secrets.token_bytes(args.smoke_bytes),)); con.commit()
                finally: con.close()
                backup(synthetic,root/'snapshots'/name)
            state=prepare(root/'snapshots'/name,job); state['purpose']='smoke'; save(job/'job.json',state)
        async with Transport(config) as transport:
            print('Uploading encrypted parts and verifying Telegram read-back.',flush=True)
            receipt=await upload(job,transport,key,args.smoke_part_bytes if args.command=='smoke' else config['part_bytes'])
            if args.command=='daily' and config.get('restore_first_backup'):
                await first_restore_drill(config,transport,key,receipt)
            if args.command=='smoke':
                destination=root/('restore-'+name)
                await restore(receipt,transport,key,destination)
                # Also prove recovery using only the encrypted channel manifest,
                # independent content key and bot credentials.
                recovered=await recover_manifest(transport,key,receipt['manifest_message_id'],root/('manifest-'+name))
                if recovered['archive_sha256']!=receipt['archive_sha256']: raise BackupError('Channel manifest recovery differs')
            if config.get('notify'):
                await transport.notify('PumpWizard '+('synthetic backup/restore test' if args.command=='smoke' else 'backup')+
                    ' verified. Run '+receipt['id']+'. Encrypted parts: '+str(len(receipt['parts']))+'.')
        print('Backup verified: '+str(job/'telegram-receipt.json'),flush=True)
        if args.command=='daily':
            retention=prune(config,apply=True)
            if retention.get('removed_bytes'): print('Verified local retention reclaimed '+str(retention['removed_bytes'])+' bytes. All Telegram backups retained.',flush=True)
        progress(config,'complete')
        return
    async with Transport(config) as transport:
        if args.command=='restore':
            await restore(load(args.receipt),transport,key,args.destination)
            progress(config,'complete')
            print('Telegram download and SQLite restore verified. No collector started.')
        elif args.command=='upload':
            receipt=await upload(args.job,transport,key,config['part_bytes'])
            print('Backup verified: '+receipt['id'])
        else:
            await recover_manifest(transport,key,args.message_id,args.destination)
            print('Verified recovery manifest downloaded.')


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=ROOT/'data/telegram-backup/config.json')
    p.add_argument('command',choices=['probe','smoke','daily','upload','restore','recover-manifest'])
    p.add_argument('--apply',action='store_true')
    p.add_argument('--job',type=Path); p.add_argument('--receipt',type=Path); p.add_argument('--destination',type=Path)
    p.add_argument('--message-id',type=int)
    p.add_argument('--smoke-bytes',type=int,default=2*1024**2)
    p.add_argument('--smoke-part-bytes',type=int,default=1024**2)
    args=p.parse_args()
    try:
        config=load(args.config,private=True)
        if config.get('backup_enabled',True) is False and args.command in ('daily','smoke','upload'):
            print('Telegram backups are disabled by operator configuration. No backup was started.')
            return 0
        if not 1024<=args.smoke_bytes<=128*1024**2 or not 1024<=args.smoke_part_bytes<=128*1024**2:
            raise BackupError('Smoke sizes must be between 1 KiB and 128 MiB')
        if args.command in ('smoke','daily','upload') and not args.apply: raise BackupError('Telegram writes require --apply')
        if args.command=='upload' and args.job is None: raise BackupError('--job is required')
        if args.command=='restore' and (args.receipt is None or args.destination is None): raise BackupError('--receipt and --destination are required')
        if args.command=='recover-manifest' and (args.message_id is None or args.destination is None): raise BackupError('--message-id and --destination are required')
        with lock(args.config.parent/'operation.lock'):
            # First production recovery downloads the full archive twice (the
            # upload read-back and independent restore); do not give it the same
            # deadline as an ordinary daily transfer.
            first_drill=(args.command=='daily' and config.get('restore_first_backup') and
                         not (Path(config['backup_root'])/'production-drill.json').exists())
            try: asyncio.run(asyncio.wait_for(run(args,config),timeout=(24 if first_drill else 12)*3600))
            except Exception:
                progress(config,'failed')
                if args.command in ('daily','smoke','upload') and args.apply and config.get('notify'):
                    try:
                        from lab_backup.transport import Transport
                        Transport(config).bot_api('sendMessage',{'chat_id':load(config['credentials'],private=True)['channel_id'],
                            'text':'PumpWizard backup needs attention. Inspect the local backup service log; existing backups were retained.'})
                    except Exception: pass
                raise
    except Exception as exc:
        p.exit(1,(str(exc) if isinstance(exc,BackupError) else 'Telegram backup failed; credentials were not logged')+'\n')


if __name__=='__main__': main()
