"""Print only non-secret operational sizes and backup progress."""
import json
from pathlib import Path
import shutil
import time

ROOT=Path(__file__).resolve().parents[1]
config=json.loads((ROOT/'data/telegram-backup/config.json').read_text())
root=Path(config['backup_root'])
rows=[]
for path in sorted(root.glob('jobs/*/job.json')):
    row=json.loads(path.read_text())
    if row.get('purpose','production')!='production': continue
    rows.append({'job':path.parent.name,'status':row['status'],
        'archive_gib':round(row['archive_bytes']/1024**3,2),
        'restore_gib':round(sum(v['bytes'] for v in row['members'].values())/1024**3,2),
        'parts':len(row.get('parts',[])),
        'verified_parts':sum(bool(p.get('verified_at')) for p in row.get('parts',[])),
        'uncertain_sends':sum(bool(p.get('sending')) for p in row.get('parts',[]))})
marker=root/'production-drill.json'
restores=[{'directory':p.parent.name,'downloaded_gib':round(p.stat().st_size/1024**3,2)}
          for p in sorted(root.glob('production-restore-*/download.tar.gz'))]
progress_path=Path(config['session']).parent/'progress.json'
progress=json.loads(progress_path.read_text()) if progress_path.exists() else {}
print(json.dumps({'production_jobs':rows,'free_gib':round(shutil.disk_usage(root).free/1024**3,2),
    'restore_verified':marker.exists() and json.loads(marker.read_text()).get('status')=='verified',
    'restores':restores,'transfer_phase':progress.get('phase'),
    'progress_age_seconds':round(time.time()-progress['updated_at']) if progress.get('updated_at') else None},indent=2))
