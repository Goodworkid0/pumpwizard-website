"""Guarded, one-shot PumpWizard migration to the already prepared production host.

No data or backup is deleted. Before remote startup, errors restart the local
collector. After remote startup, errors leave it authoritative and the local
collector stopped, avoiding diverging ledgers. Proxy activation has its own rollback.
"""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.backup_lab import backup
from lab_backup.common import load, save, lock

RELEASE='/opt/pump-paper/releases/20260913-0105'
DATA='/opt/pump-paper/data'
VHOST_HASH='c5a56a84ae0e4d3f6fc7ae9201632e067c5c71415728b9172d3a5941a1a336d8'
STATE=ROOT/'data/release-20260913.json'


def run(*args,timeout=3600,allowed=(0,),**kwargs):
    result=subprocess.run(args,capture_output=True,text=True,timeout=timeout,**kwargs)
    if result.returncode not in allowed:
        # Do not log provider output or a full command that could contain secrets.
        raise RuntimeError('Deployment command failed: '+Path(args[0]).name+' (exit '+str(result.returncode)+')')
    return result.stdout.strip()


def remote(command,**kwargs):
    return run('ssh','-o','BatchMode=yes','-o','ConnectTimeout=15','idb-host',command,**kwargs)


def notify(message):
    try:
        from lab_backup.transport import Transport
        config=load(ROOT/'data/telegram-backup/config.json',private=True)
        transport=Transport(config)
        transport.bot_api('sendMessage',{'chat_id':transport.channel_id,'text':'PumpWizard deployment: '+message})
    except Exception: print('Deployment notification unavailable.',flush=True)


def phase(name,**fields):
    state=load(STATE) if STATE.exists() else {}
    state.update(phase=name,updated_at=time.time(),**fields); save(STATE,state)
    print(name,flush=True)


def proc_identity(pid):
    try:
        command=Path('/proc/'+str(pid)+'/cmdline').read_bytes().split(b'\0')
        if b'scripts/stage_release_data.py' not in command: return None
        return Path('/proc/'+str(pid)+'/stat').read_text().rsplit(')',1)[1].split()[19]
    except FileNotFoundError: return None


def no_writers():
    # Retained lock-file names are not evidence of a running writer: test flock.
    for path in (ROOT/'data').glob('*.sqlite.lock'):
        with path.open('rb') as stream:
            fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            fcntl.flock(stream,fcntl.LOCK_UN)


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--staging-pid',type=int,required=True)
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    if not args.apply: p.error('--apply required')
    stopped=False; remote_started=False; proxy_backup=None; browser_verified=False
    with lock(ROOT/'data/deploy-release.lock'):
        if STATE.exists(): raise RuntimeError('Deployment state already exists; inspect before another cutover')
        try:
            identity=proc_identity(args.staging_pid)
            phase('waiting_for_staged_history',staging_pid=args.staging_pid)
            deadline=time.monotonic()+24*3600
            while identity and proc_identity(args.staging_pid)==identity:
                if time.monotonic()>deadline: raise TimeoutError('Staging deadline exceeded')
                time.sleep(30)
            phase('checking_staged_history')
            original=Path('/home/c/Desktop/projekti/pump-agent-backups/snapshots/20260912T220353Z')
            run('rsync','-a',str(original/'manifest.json'),'idb-host:'+RELEASE+'/staging-manifest.json')
            remote('python3 '+RELEASE+'/scripts/verify_release_data.py --manifest '+RELEASE+
                   '/staging-manifest.json --data '+DATA,timeout=1800)
            if remote('docker ps -a --filter name=^/pump-paper$ --format "{{.Names}}"'):
                raise RuntimeError('A production collector container already exists; inspect before cutover')
            run('systemctl','--user','is-active','--quiet','pump-agent.service')
            # Retain the local source and an additional frozen cutover snapshot.
            phase('pausing_local_collector')
            notify('Final synchronization starting. Local simulation is pausing; the landing page remains available.')
            run('systemctl','--user','stop','pump-agent.service',timeout=180)
            stopped=True
            if run('systemctl','--user','show','pump-agent.service','-p','MainPID','--value')!='0':
                raise RuntimeError('Local collector did not stop')
            no_writers()
            snapshot=Path('/home/c/Desktop/projekti/pump-agent-backups/snapshots')/time.strftime('cutover-%Y%m%dT%H%M%SZ',time.gmtime())
            phase('creating_frozen_cutover',snapshot=str(snapshot))
            manifest=backup(ROOT,snapshot,frozen=True)
            phase('final_data_synchronization')
            run(sys.executable,str(ROOT/'scripts/stage_release_data.py'),'--snapshot',str(snapshot),'--apply',timeout=24*3600)
            run('rsync','-a',str(snapshot/'manifest.json'),'idb-host:'+RELEASE+'/cutover-manifest.json')
            # The image was built and tested against these configurations; refuse
            # a mid-deployment rule change instead of silently starting new rules.
            for item in manifest['files']:
                if not item['path'].startswith('config/'): continue
                name=Path(item['path']).name
                actual=remote('sha256sum '+shlex.quote(RELEASE+'/'+name)).split()[0]
                if actual!=item['sha256']: raise RuntimeError('Staged configuration differs from the frozen snapshot')
            phase('verifying_final_server_data')
            remote('python3 '+RELEASE+'/scripts/verify_release_data.py --manifest '+RELEASE+
                   '/cutover-manifest.json --data '+DATA,timeout=1800)
            remote('chown 10001:10001 '+DATA+' && chmod 700 '+DATA+
                   ' && find '+DATA+' -maxdepth 1 -type f -name "*.sqlite" -exec chown 10001:10001 {} +')
            phase('starting_server_collector')
            # From this point, never automatically resume the older local ledger.
            remote_started=True
            remote('cd '+RELEASE+'/deploy && PUMP_DATA_DIR='+DATA+
                   ' docker compose -f compose.production.yml up -d --no-build app',timeout=300)
            deadline=time.monotonic()+600
            check_command='docker exec pump-paper python3 -c '+shlex.quote(
                "import json,urllib.request; h=json.load(urllib.request.urlopen('http://127.0.0.1:8765/api/public/health',timeout=10)); assert h['status']=='ok'")
            while True:
                try: remote(check_command,timeout=30); break
                except Exception:
                    if time.monotonic()>deadline: raise RuntimeError('Production readiness deadline exceeded')
                    time.sleep(15)
            phase('activating_public_routes')
            result=remote('python3 '+RELEASE+'/deploy/activate_app.py --candidate '+RELEASE+
                          '/deploy/pumpwizard-app.conf --expected-sha256 '+VHOST_HASH+' --apply',timeout=300)
            proxy_backup=result.split('Rollback config: ',1)[1].strip()
            phase('checking_public_browser',proxy_result=result)
            env={**os.environ,'PUMP_TEST_URL':'https://pumpwizard.fun'}
            run(sys.executable,str(ROOT/'tests/check_public_shell_ui.py'),env=env,timeout=180)
            run(sys.executable,str(ROOT/'tests/check_public_read_ui.py'),env=env,timeout=240)
            browser_verified=True
            phase('activating_production_operations')
            units=RELEASE+'/deploy/production-units/'
            remote('systemctl link '+units+'pump-telegram-backup.service '+units+'pump-telegram-backup.timer '+
                   units+'pump-lab-check.service '+units+'pump-lab-check.timer && systemctl daemon-reload && '+
                   'systemctl enable --now pump-telegram-backup.timer pump-lab-check.timer && '+
                   'systemctl start --no-block pump-telegram-backup.service')
            run('systemctl','--user','disable','pump-agent.service')
            run('systemctl','--user','disable','--now','pump-telegram-backup.timer','pump-lab-check.timer')
            phase('live',url='https://pumpwizard.fun',completed_at=time.time(),remote_release=RELEASE)
            notify('LIVE at https://pumpwizard.fun. Public browser, API and access-boundary checks passed. Server backups and monitoring enabled; local collector stopped. Frozen rollback snapshot retained.')
        except Exception as error:
            if proxy_backup and not browser_verified:
                try:
                    remote('python3 '+RELEASE+'/deploy/activate_app.py --candidate '+RELEASE+
                           '/deploy/pumpwizard-app.conf --expected-sha256 '+VHOST_HASH+
                           ' --restore-landing '+shlex.quote(proxy_backup)+' --apply',timeout=300)
                except Exception: print('Automatic proxy rollback needs manual inspection.',flush=True)
            if stopped and not remote_started:
                try: run('systemctl','--user','start','pump-agent.service')
                except Exception: pass
            phase('needs_attention',error=str(error),remote_started=remote_started,browser_verified=browser_verified)
            notify('Needs attention; check the deployment service log. '+
                   ('Local collector stays stopped to avoid divergent histories; server data is retained.' if remote_started
                    else 'Public routes were not switched; local collector was not moved.'))
            raise


if __name__=='__main__': main()
