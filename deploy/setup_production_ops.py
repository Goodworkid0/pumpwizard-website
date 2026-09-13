"""Prepare production-only backup paths and unit files; never enables services."""
import json
import os
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def main():
    os.umask(0o077)
    if str(ROOT)!='/opt/pump-paper/releases/20260913-0105': raise RuntimeError('Unexpected release root')
    private=Path('/opt/pump-paper/data/telegram-backup')
    private.mkdir(mode=0o700,exist_ok=False)
    backup=Path('/opt/pump-paper/backups'); backup.mkdir(mode=0o700,exist_ok=False)
    config={'credentials':str(private/'credentials.json'),'key':str(private/'key.json'),
            'session':str(private/'session.json'),'backup_root':str(backup),'part_bytes':134217728,
            'notify':True,'minimum_free_bytes':10737418240,'restore_first_backup':True,
            'upload_connections':4,'health_url':'https://pumpwizard.fun/api/public/health'}
    (private/'config.json').write_text(json.dumps(config,indent=2))
    units=ROOT/'deploy/production-units'; units.mkdir(exist_ok=False)
    for name in ('pump-telegram-backup.service','pump-telegram-backup.timer','pump-lab-check.service','pump-lab-check.timer'):
        source=(ROOT/'deploy'/name).read_text().replace('/home/c/Desktop/projekti/pump-agent',str(ROOT))
        (units/name).write_text(source)
    print('Private paths and unit candidates prepared. No sessions copied; no timers started.')


if __name__=='__main__': main()
