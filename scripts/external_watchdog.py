"""Independent-host public uptime check. Bot API only; no MTProto session or writer."""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lab_backup.common import load, save, lock
from scripts.monitor_lab import alert_transition


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=ROOT/'data/telegram-backup/config.json')
    parser.add_argument('--url',default='https://pumpwizard.fun/api/public/health')
    parser.add_argument('--notify',action='store_true')
    args=parser.parse_args()
    if not args.url.startswith('https://'): parser.error('HTTPS public endpoint required')
    healthy=False
    try:
        result=subprocess.run(['curl','--silent','--show-error','--fail','--max-time','15',args.url],capture_output=True,timeout=20)
        if len(result.stdout)>32768: raise ValueError('Oversized health response')
        document=json.loads(result.stdout)
        healthy=result.returncode==0 and document['status']=='ok' and bool(document['checks']) and all(v is True for v in document['checks'].values())
    except (OSError,ValueError,KeyError,subprocess.TimeoutExpired): pass
    checks={'public_https_readiness':healthy}
    if args.notify:
        from lab_backup.transport import Transport
        config=load(args.config,private=True)
        transport=Transport(config)
        with lock(args.config.parent/'external-watchdog.lock'):
            path=args.config.parent/'external-watchdog-state.json'
            previous=load(path) if path.exists() else {}
            def notify(message): transport.bot_api('sendMessage',{'chat_id':transport.channel_id,'text':'External uptime check: '+message})
            save(path,alert_transition(previous,checks,time.time(),notify))
    print(json.dumps(checks)); return 0 if healthy else 1


if __name__=='__main__': raise SystemExit(main())
