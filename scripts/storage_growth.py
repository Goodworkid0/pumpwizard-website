"""Small filesystem-only time series for database growth; no ledger queries."""
from pathlib import Path
import shutil

from lab_backup.common import load,lock,save


def summarize(samples,usage):
    elapsed=samples[-1]['at']-samples[0]['at'] if len(samples)>1 else 0
    daily=None
    if elapsed>=1800:
        daily=max(0,samples[-1]['sqlite_bytes']-samples[0]['sqlite_bytes'])*86400/elapsed
    reserve=max(10*1024**3,usage.total*.1)
    return {'sqlite_bytes':samples[-1]['sqlite_bytes'],'wal_bytes':samples[-1]['wal_bytes'],
            'observed_seconds':round(elapsed),'estimated_bytes_per_day':daily,
            'estimated_days_to_disk_reserve':max(0,usage.free-reserve)/daily if daily else None,
            'scope':'Filesystem samples; requires 30 minutes in the current storage format. Short-term growth is not a capacity guarantee.'}


def record(data,now):
    data=Path(data)
    sample={'at':now,'sqlite_bytes':sum(p.stat().st_size for p in data.glob('*.sqlite') if p.is_file()),
            'wal_bytes':sum(p.stat().st_size for p in data.glob('*.sqlite-wal') if p.is_file())}
    flag=data/'input-storage.json'
    generation=load(flag).get('activated_at') if flag.exists() else 'embedded'
    path=data/'storage-growth.json'
    with lock(data/'storage-growth.lock'):
        previous=load(path) if path.exists() else {}
        samples=previous.get('samples',[]) if previous.get('generation')==generation else []
        samples=[s for s in samples if 0<now-s['at']<=86400]
        if not samples or now-samples[-1]['at']>=60:samples.append(sample)
        samples=samples[-1441:]
        result=summarize(samples,shutil.disk_usage(data))
        save(path,{'generation':generation,'samples':samples,'summary':result})
    return result
