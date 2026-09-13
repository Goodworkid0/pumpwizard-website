"""Apply the operator-approved latest-local / all-Telegram retention policy."""
import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from lab_backup.common import load, save, lock
from lab_backup.retention import prune

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config',type=Path,default=ROOT/'data/telegram-backup/config.json')
    p.add_argument('--apply',action='store_true'); p.add_argument('--enable',action='store_true')
    args=p.parse_args()
    with lock(args.config.parent/'operation.lock'):
        config=load(args.config,private=True)
        if args.enable:
            config['local_retention']='latest_verified'
            if args.apply: save(args.config,config)
        print(json.dumps(prune(config,apply=args.apply),indent=2))
