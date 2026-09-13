"""Guarded SQLite-preserving promotion of the separately tested public worker.

Run on the production host. PostgreSQL cutover is deliberately a separate frozen
data operation; this release never silently changes the authoritative database.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import time
import urllib.request
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DATA=Path('/opt/pump-paper/data')
BACKUPS=Path('/opt/pump-paper/backups')
IMAGE='pump-paper:hardening-20260913'
ROLLBACK='pump-paper-rollback-20260913-hardening'
CORE=('ladder.py','model.py','research_runner.py','shared_research.py','experiment_catalog.py','versioned_trials.py','forward_trials_v3.py')


def command(*args,timeout=180):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout)
    if result.returncode: raise RuntimeError('Release command failed: '+args[0])
    return result.stdout.strip()


def write_state(**updates):
    path=ROOT/'worker-deployment.json'
    value=json.loads(path.read_text()) if path.exists() else {}
    value.update(updates,updated_at=time.time())
    temporary=path.with_suffix('.tmp'); temporary.write_text(json.dumps(value,indent=2)); temporary.replace(path)


def health():
    with urllib.request.urlopen('http://127.0.0.1:8765/api/public/health',timeout=10) as response:
        return json.load(response)


def ledger_identity():
    result={}
    for path in sorted(DATA.glob('long-hold-*.sqlite')):
        con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
        try:
            stored=con.execute("SELECT value FROM meta WHERE key='state'").fetchone()
            if stored is None: raise ValueError('Ledger has no committed state: '+path.name)
            state=json.loads(stored[0])
            result[path.name]={'started_at':state['started_at'],'last_at':state['last_at'],
                              'closed':con.execute('SELECT count(*) FROM trades').fetchone()[0]}
        finally: con.close()
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__); parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if ROOT!=Path('/opt/pump-paper/releases/20260913-hardening'): raise ValueError('Run from the staged production release')
    marker=BACKUPS/'production-drill.json'
    backup_ok=marker.exists() and json.loads(marker.read_text()).get('status')=='verified'
    usage=shutil.disk_usage(DATA)
    soak_path=ROOT/'staging-final-soak.json'
    soak=json.loads(soak_path.read_text()) if soak_path.exists() else {}
    checks={'restore_verified':backup_ok,'disk_headroom':usage.free>=max(10*1024**3,usage.total*.10),
            'staging_soak':soak.get('failures')==0 and len(soak.get('samples',[]))>=60}
    print(json.dumps(checks),flush=True)
    if not args.apply: return
    if not all(checks.values()): raise ValueError('Release gates are not complete')
    if (ROOT/'worker-deployment.json').exists(): raise ValueError('Deployment state exists; inspect before retrying')
    with (ROOT/'promotion.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        from scripts.reconcile_restore import reconcile
        restored=Path(json.loads(marker.read_text())['destination'])
        if restored.parent!=BACKUPS or not restored.name.startswith('production-restore-'):
            raise ValueError('Unexpected production restore directory')
        recovery=reconcile(restored)
        hashes=command('docker','exec','pump-paper','python3','-c',
            'import hashlib,json; from pathlib import Path; print(json.dumps({n:hashlib.sha256((Path("/app/paper")/n).read_bytes()).hexdigest() for n in '+repr(CORE)+'}))')
        actual=json.loads(hashes)
        if any(actual[n]!=hashlib.sha256((ROOT/'paper'/n).read_bytes()).hexdigest() for n in CORE):
            raise ValueError('Simulation implementation changed; do not migrate immutable contracts')
        image=command('docker','image','inspect',IMAGE,'--format','{{.Id}}')
        candidate=json.loads(command('docker','run','--rm','--network','none','--read-only',image,'python3','-c',
            'import hashlib,json; from pathlib import Path; print(json.dumps({n:hashlib.sha256((Path("/app/paper")/n).read_bytes()).hexdigest() for n in '+repr(CORE)+'}))'))
        if candidate!=actual: raise ValueError('Built image simulation implementation differs from live contracts')
        from scripts.backup_lab import CONFIGS
        config_code='import json,hashlib; from pathlib import Path; print(json.dumps({n:hashlib.sha256((Path("/app")/n).read_bytes()).hexdigest() for n in '+repr(CONFIGS)+' if (Path("/app")/n).exists()}))'
        old_config=json.loads(command('docker','exec','pump-paper','python3','-c',config_code))
        new_config=json.loads(command('docker','run','--rm','--network','none','--read-only',image,'python3','-c',config_code))
        if old_config!=new_config: raise ValueError('Application configuration differs from production; inspect before migration')
        if command('docker','ps','-aq','--filter','name=^/'+ROLLBACK+'$'): raise ValueError('Rollback container already exists')
        previous_image=command('docker','inspect','pump-paper','--format','{{.Image}}')
        proxy_hash=hashlib.sha256(Path('/opt/pump-paper/deploy/pumpwizard.conf').read_bytes()).hexdigest()
        environment={**os.environ,'PUMP_IMAGE':image,'PUMP_DATA_DIR':str(DATA),'PUMP_LEDGER_BACKEND':'sqlite'}
        compose=['docker','compose','-f',str(ROOT/'deploy/compose.workers.yml')]
        subprocess.run(compose+['config','--quiet'],env=environment,check=True)
        resolved=json.loads(subprocess.check_output(compose+['config','--format','json'],env=environment,text=True))
        for service in resolved['services'].values():
            if service.get('tmpfs')!=['/tmp:size=64m,mode=1777']:
                raise ValueError('Unexpected temporary-filesystem configuration')
        command('docker','run','--rm','--network','none','--read-only','--tmpfs','/tmp:size=64m,mode=1777',
                image,'python3','-c','import tempfile; d=tempfile.TemporaryDirectory(); d.cleanup()')
        before=ledger_identity(); stopped=False; renamed=False; started=False
        write_state(phase='preflight_verified',previous_image=previous_image,proxy_hash=proxy_hash,recovery=recovery,image=image)
        try:
            command('docker','stop','--time','45','pump-paper'); stopped=True
            command('docker','rename','pump-paper',ROLLBACK); renamed=True
            write_state(phase='starting_workers')
            started=True
            subprocess.run(compose+['up','-d','--no-build'],env=environment,check=True,timeout=180)
            deadline=time.monotonic()+300
            while True:
                try:
                    if health()['status']=='ok': break
                except (OSError,ValueError): pass
                if time.monotonic()>deadline: raise RuntimeError('New workers did not become healthy')
                time.sleep(3)
            after=ledger_identity()
            if set(before)!=set(after) or any(after[n]['started_at']!=before[n]['started_at'] or after[n]['closed']<before[n]['closed'] or (before[n]['last_at'] is not None and after[n]['last_at']<before[n]['last_at']) for n in before):
                raise RuntimeError('Instance history continuity check failed')
            mounts=json.loads(command('docker','inspect','pump-paper','--format','{{json .Mounts}}'))
            if not any(m['Destination']=='/app/data' and m['RW'] is False for m in mounts):
                raise RuntimeError('Public worker data mount is not read-only')
            write_state(phase='activating_routes',instances=len(after))
            result=command('python3',str(ROOT/'deploy/activate_app.py'),'--candidate',str(ROOT/'deploy/pumpwizard-app.conf'),
                           '--expected-sha256',proxy_hash,'--apply',timeout=300)
            write_state(phase='live',proxy_result=result,completed_at=time.time(),backend='sqlite')
            print('Workers live. Instance histories preserved; public data mount is read-only. Previous image/container retained.',flush=True)
        except BaseException:
            if started:
                subprocess.run(compose+['stop'],env=environment,timeout=120)
                # Retain failed containers for inspection, never remove their data.
                if command('docker','ps','-aq','--filter','name=^/pump-paper$'):
                    command('docker','rename','pump-paper','pump-public-failed-20260913-hardening')
            if renamed: command('docker','rename',ROLLBACK,'pump-paper')
            if stopped: command('docker','start','pump-paper')
            write_state(phase='rolled_back')
            raise


if __name__=='__main__': main()
