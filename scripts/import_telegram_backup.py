"""One-time, read-only credential import from OTS. Never emits secret values."""
import argparse
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lab_backup.common import BackupError, save

# Runs inside the selected existing container. SQLite is opened read-only; no OTS
# startup/import side effects, no session/key changes. stdout is a private pipe
# captured by this process and is NEVER printed or forwarded to a tool result.
EXTRACT = r'''
import base64,json,os,sqlite3
from pathlib import Path
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
root=Path(os.environ.get('OTS_DATA_DIR','/data'))
path=root/'ots.db'
if not path.exists():path=root/'otg.db'
key=bytes.fromhex(os.environ.get('OTS_MASTER_KEY','').strip() or (root/'master.key').read_text().strip())
db=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
try:
    values={}
    for name in ('api_id','api_hash','bot_token','channel_id'):
        row=db.execute('SELECT value,encrypted FROM settings WHERE key=?',('telegram.'+name,)).fetchone()
        if not row:raise ValueError('missing setting')
        value,encrypted=row
        if encrypted:
            if not value.startswith('v1:'):raise ValueError('unsupported vault')
            raw=base64.urlsafe_b64decode(value[3:])
            value=AESGCM(key).decrypt(raw[:12],raw[12:],None).decode()
        values[name]=value
    print(json.dumps(values))
finally:db.close()
'''


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--container',default='ots'); p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    try:
        if not args.apply: raise BackupError('Import requires --apply')
        if not re.fullmatch(r'[A-Za-z0-9_.-]+',args.container): raise BackupError('Invalid container name')
        destination=ROOT/'data/telegram-backup'
        if destination.exists(): raise BackupError('Private backup setup already exists; refusing to overwrite keys')
        result=subprocess.run(['docker','exec',args.container,'python3','-c',EXTRACT],
                              stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30,check=False)
        if result.returncode or len(result.stdout)>8192: raise BackupError('Could not privately read the existing OTS credentials')
        values=json.loads(result.stdout)
        values['api_id']=int(values['api_id']); values['channel_id']=int(values['channel_id'])
        if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}',values['bot_token']): raise BackupError('Invalid saved bot credentials')
        if not re.fullmatch(r'[0-9a-fA-F]{32}',values['api_hash']): raise BackupError('Invalid saved API credentials')
        if not str(values['channel_id']).startswith('-100'): raise BackupError('Saved destination is not a channel')
        destination.mkdir(mode=0o700)
        save(destination/'credentials.json',values)
        save(destination/'key.json',{'schema':'pumpwizard.backup-key/v1','key':secrets.token_hex(32)})
        save(destination/'config.json',{'credentials':str(destination/'credentials.json'),
            'key':str(destination/'key.json'),'session':str(destination/'session.json'),
            'backup_root':str(ROOT.parent/'pump-agent-backups'), 'part_bytes':128*1024**2,
            'notify':True,'minimum_free_bytes':10*1024**3})
        print('Private Telegram backup setup created. OTS unchanged; new encryption key, no copied OTS session or content keys.')
    except Exception as exc:
        p.exit(1,(str(exc) if isinstance(exc,BackupError) else 'Credential import failed; no secret values logged')+'\n')


if __name__=='__main__': main()
