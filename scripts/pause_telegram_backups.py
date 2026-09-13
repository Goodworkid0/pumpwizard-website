"""Persist an explicit backup pause without modifying credentials or recovery files."""
import argparse
import json
from pathlib import Path
import sys
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from lab_backup.common import load, lock, save


def pause(path):
    with lock(path.parent/'operation.lock'):
        config=load(path,private=True)
        if config.get('backup_enabled',True) is not False:
            config.update(backup_enabled=False,backup_disabled_at=time.time(),
                          backup_disabled_reason='Operator requested pause during database storage rework')
            save(path,config)
        return {'backup_enabled':False,'disabled_at':config.get('backup_disabled_at'),
                'scope':'New backups disabled; existing backups and recovery credentials retained'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=ROOT/'data/telegram-backup/config.json')
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    if not args.apply: p.error('Persisting the backup pause requires --apply; stop the backup service first')
    print(json.dumps(pause(args.config),indent=2))
