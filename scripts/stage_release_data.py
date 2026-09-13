"""Stage an existing verified snapshot using four disjoint SSH/rsync streams."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--snapshot',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    args=p.parse_args()
    manifest=json.loads((args.snapshot/'manifest.json').read_text())
    if manifest.get('schema')!='pumpwizard.backup/v1' or not manifest.get('completed_at'):
        raise ValueError('Completed snapshot required')
    files=[f for f in manifest['files'] if f['path'].startswith('data/')]
    buckets=[[] for _ in range(4)]; sizes=[0]*4
    for entry in sorted(files,key=lambda f:f['bytes'],reverse=True):
        name=Path(entry['path']).name
        if entry['path']!='data/'+name or not name.endswith('.sqlite'): raise ValueError('Invalid snapshot path')
        source=args.snapshot/'data'/name
        if source.is_symlink() or source.stat().st_size!=entry['bytes']: raise ValueError('Snapshot source changed')
        bucket=min(range(4),key=lambda i:sizes[i]); buckets[bucket].append(name); sizes[bucket]+=entry['bytes']
    print(json.dumps({'databases':len(files),'stream_bytes':sizes,'apply':args.apply}),flush=True)
    if not args.apply: return
    def transfer(names):
        command=['rsync','-a','--compress','--compress-level=1','--partial','--from0','--files-from=-',
                 '--stats',str(args.snapshot/'data')+'/', 'idb-host:/opt/pump-paper/data/']
        result=subprocess.run(command,input='\0'.join(names)+'\0',text=True,capture_output=True,timeout=24*3600)
        if result.returncode: raise RuntimeError('Staging stream failed with exit '+str(result.returncode))
        print('Staging stream complete: '+str(len(names))+' databases',flush=True)
    with ThreadPoolExecutor(max_workers=4) as pool: list(pool.map(transfer,buckets))
    print('All snapshot files staged. Final stopped-writer synchronization and verification still required.',flush=True)


if __name__=='__main__': main()
