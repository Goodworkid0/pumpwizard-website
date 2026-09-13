"""Explicit live synthetic transfer test using a separate, temporary bot session."""
import argparse
import asyncio
from pathlib import Path
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from lab_backup.common import load
from scripts.telegram_backup import run

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply',action='store_true')
    args=parser.parse_args()
    if not args.apply: parser.error('Synthetic encrypted Telegram messages require --apply')
    with tempfile.TemporaryDirectory(prefix='pump-telegram-transfer-') as temp:
        root=Path(temp); config=load(ROOT/'data/telegram-backup/config.json',private=True)
        config={**config,'session':str(root/'session.json'),'backup_root':str(root/'backups'),
                'download_connections':4,'notify':False,'minimum_free_bytes':1024**3}
        started=time.monotonic()
        options=argparse.Namespace(command='smoke',smoke_bytes=16*1024**2,smoke_part_bytes=4*1024**2)
        asyncio.run(asyncio.wait_for(run(options,config),timeout=300))
        print('Independent-session synthetic Telegram upload, parallel read-back and restore passed in '+str(round(time.monotonic()-started,1))+' seconds.')
