"""Bounded read-only inventory and recent input deduplication sample; no payload export."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3
import time


def profile(root, sample_rows=64):
    started=time.time(); rows=[]; unique={}; copies=Counter(); occurrences=0; bytes_with_copies=0
    for path in sorted(root.glob('*.sqlite')):
        if path.is_symlink() or not path.is_file(): raise ValueError('Regular database files required')
        con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=3)
        con.execute('PRAGMA query_only=ON')
        deadline=time.monotonic()+5
        con.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
        try:
            tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            item={'database':path.name,'file_bytes':path.stat().st_size,
                  'has_input_refs':'input_refs' in tables,'sample_rows':0,'sample_payload_bytes':0}
            if 'inputs' in tables:
                columns={r[1] for r in con.execute('PRAGMA table_info(inputs)')}
                if {'id','at','payload'}<=columns:
                    item['input_count']=con.execute('SELECT count(*) FROM inputs').fetchone()[0]
                    sample=con.execute('SELECT id,length(payload),at FROM inputs ORDER BY rowid DESC LIMIT ?',
                                       (sample_rows,)).fetchall()
                    for ident,size,at in sample:
                        if not isinstance(size,int): raise ValueError('Input payload missing')
                        if ident in unique and unique[ident]!=size: raise ValueError('Input ID has conflicting encoded sizes')
                        unique[ident]=size; copies[ident]+=1
                        item['sample_rows']+=1;item['sample_payload_bytes']+=size
                        occurrences+=1; bytes_with_copies+=size
                    if sample: item['sample_latest_at']=max(r[2] for r in sample)
            rows.append(item)
        finally: con.close()
    groups={}
    for name,selected in (
            ('ledgers_with_embedded_inputs',[r for r in rows if r['database'].startswith('long-hold-') and r.get('input_count',0)>0]),
            ('ledgers_with_reference_only_inputs',[r for r in rows if r['database'].startswith('long-hold-') and r['has_input_refs'] and not r.get('input_count',0)]),
            ('other_databases',[r for r in rows if not r['database'].startswith('long-hold-')])):
        groups[name]={'files':len(selected),'file_bytes':sum(r['file_bytes'] for r in selected),
                      'input_rows':sum(r.get('input_count',0) for r in selected)}
    return {'schema':'pumpwizard.input-storage-profile/v1','measured_at':started,
            'elapsed_seconds':round(time.time()-started,2),'sqlite_files':len(rows),
            'sqlite_file_bytes':sum(r['file_bytes'] for r in rows),'groups':groups,
            'sample':{'max_rows_per_database':sample_rows,'input_occurrences':occurrences,
                      'unique_input_ids':len(unique),'payload_bytes_with_copies':bytes_with_copies,
                      'payload_bytes_if_shared':sum(unique.values()),
                      'maximum_instances_sharing_sampled_id':max(copies.values(),default=0),
                      'duplicate_payload_fraction':1-sum(unique.values())/bytes_with_copies if bytes_with_copies else None},
            'largest_databases':sorted(rows,key=lambda r:r['file_bytes'],reverse=True)[:8],
            'scope':'Read-only recent-row sample. IDs and encoded lengths compared; complete payload hash verification and full-history unique size are separate migration gates. Active files are not an atomic snapshot.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data',type=Path,required=True)
    parser.add_argument('--sample-rows',type=int,default=64)
    args=parser.parse_args()
    if not 1<=args.sample_rows<=256: parser.error('Sample rows must be between 1 and 256')
    print(json.dumps(profile(args.data,args.sample_rows),indent=2))
