"""Guarded production compaction and worker cutover; retains original ledgers."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT=Path('/opt/pump-paper/releases/20260914-shared-inputs')
DATA=Path('/opt/pump-paper/data')
WORK=DATA/'input-migration-20260914'
IMAGE='pump-paper:shared-inputs-20260914'
CORE=('ladder.py','model.py','research_runner.py','shared_research.py','experiment_catalog.py','versioned_trials.py','forward_trials_v3.py')


def run(*args,timeout=180):
    p=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if p.returncode: raise RuntimeError('Command failed: '+args[0]+' '+p.stderr[-1500:])
    return p.stdout.strip()


def status(**values):
    path=ROOT/'deployment.json';old=json.loads(path.read_text()) if path.exists() else {}
    old.update(values,updated_at=time.time());tmp=path.with_suffix('.tmp')
    with tmp.open('w') as stream:json.dump(old,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    tmp.replace(path)


def rollback_files():
    import sqlite3
    journal=WORK/'migration.json'
    if not journal.exists():return
    state=json.loads(journal.read_text())
    for row in reversed(state['rows']):
        name=row['source'];old=WORK/'originals'/name;current=DATA/name
        if not old.exists():continue
        if current.exists():
            a=sqlite3.connect(old.as_uri()+'?mode=ro&immutable=1',uri=True)
            b=sqlite3.connect(current.as_uri()+'?mode=ro',uri=True)
            try:
                query="SELECT value FROM meta WHERE key='state'"
                if a.execute(query).fetchone()!=b.execute(query).fetchone():
                    raise RuntimeError('New ledger advanced; stale rollback refused')
            finally:a.close();b.close()
            current.rename(WORK/'compact'/name)
        old.rename(current)
    flag=DATA/'input-storage.json'
    if flag.exists():flag.rename(WORK/'rolled-back-input-storage.json')
    state['phase']='rolled_back';journal.write_text(json.dumps(state,indent=2))


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');args=p.parse_args()
    if not args.apply:p.error('--apply required')
    if (ROOT/'deployment.json').exists() or WORK.exists():raise ValueError('Cutover already attempted; inspect recorded state')
    image=run('docker','image','inspect',IMAGE,'--format','{{.Id}}')
    source='import hashlib,json; from pathlib import Path; print(json.dumps({n:hashlib.sha256((Path("/app/paper")/n).read_bytes()).hexdigest() for n in '+repr(CORE)+'}))'
    old=json.loads(run('docker','exec','pump-collector','python3','-c',source))
    new=json.loads(run('docker','run','--rm','--network','none',image,'python3','-c',source))
    if old!=new:raise ValueError('Immutable strategy implementation changed')
    config='import hashlib,json; from pathlib import Path; from scripts.backup_lab import CONFIGS; print(json.dumps({n:hashlib.sha256((Path("/app")/n).read_bytes()).hexdigest() for n in CONFIGS if (Path("/app")/n).exists()}))'
    if json.loads(run('docker','exec','pump-collector','python3','-c',config))!=json.loads(run('docker','run','--rm','--network','none',image,'python3','-c',config)):
        raise ValueError('Strategy configuration changed')
    paused=json.loads((DATA/'telegram-backup/config.json').read_text())
    if paused.get('backup_enabled') is not False:raise ValueError('Telegram backup pause must remain active')
    run('curl','-fsS','--max-time','15','https://pumpwizard.fun/api/public/health')
    for name in ('pump-collector-before-shared-inputs','pump-public-before-shared-inputs'):
        if run('docker','ps','-aq','--filter','name=^/'+name+'$'):raise ValueError('Rollback container already exists')
    env={**os.environ,'PUMP_IMAGE':image,'PUMP_DATA_DIR':str(DATA),'PUMP_LEDGER_BACKEND':'sqlite'}
    compose=['docker','compose','-p','pump-shared-inputs','-f',str(ROOT/'deploy/compose.workers.yml')]
    subprocess.run(compose+['config','--quiet'],env=env,check=True)
    status(phase='preflight_verified',image=image,implementation_hashes=old)
    stopped=False;renamed=[];started=False
    try:
        run('docker','stop','--timeout','45','pump-collector','pump-paper',timeout=120);stopped=True
        status(phase='compacting')
        with (ROOT/'compaction.log').open('w') as log:
            result=subprocess.run(['docker','run','--rm','--network','none','--read-only',
                '-v',str(DATA)+':/app/data',image,'python3','scripts/compact_shared_inputs.py','compact',
                '--data','/app/data','--work','/app/data/input-migration-20260914','--apply','--frozen'],stdout=log,stderr=log,timeout=1800)
        if result.returncode:raise RuntimeError('Compaction failed; inspect compaction.log')
        status(phase='starting_workers')
        for old_name,new_name in (('pump-collector','pump-collector-before-shared-inputs'),('pump-paper','pump-public-before-shared-inputs')):
            run('docker','rename',old_name,new_name);renamed.append((old_name,new_name))
        started=True
        subprocess.run(compose+['up','-d','--no-build'],env=env,check=True,timeout=180)
        deadline=time.monotonic()+300
        while time.monotonic()<deadline:
            try:
                with urllib.request.urlopen('http://127.0.0.1:8765/api/public/health',timeout=8) as response:
                    if json.load(response)['status']=='ok':break
            except (OSError,ValueError):pass
            time.sleep(3)
        else:raise RuntimeError('New workers did not become healthy')
        run('curl','-fsS','--max-time','15','https://pumpwizard.fun/api/public/health')
        journal=json.loads((WORK/'migration.json').read_text())
        if journal['phase']!='switched':raise ValueError('Migration did not complete')
        status(phase='live',ledgers_compacted=len(journal['rows']),
               original_bytes=sum(r['source_bytes'] for r in journal['rows']),
               compact_bytes=sum(r['compact_bytes'] for r in journal['rows']),completed_at=time.time())
        print('Shared-input workers live. Originals retained pending final history verification.',flush=True)
    except BaseException:
        status(phase='needs_recovery')
        if started:subprocess.run(compose+['stop'],env=env,timeout=120)
        try:
            rollback_files()
            for old_name,new_name in reversed(renamed):
                if run('docker','ps','-aq','--filter','name=^/'+old_name+'$'):
                    run('docker','rename',old_name,old_name+'-shared-inputs-failed')
                run('docker','rename',new_name,old_name)
            if stopped:run('docker','start','pump-collector','pump-paper')
            status(phase='rolled_back')
        except BaseException:status(phase='recovery_required_no_stale_restart')
        raise


if __name__=='__main__':main()
