"""Verify preserved history, then explicitly reclaim exact superseded ledger files."""
import argparse
import hashlib
import itertools
import json
from pathlib import Path
import re
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from paper.shared_inputs import ARCHIVE,marked,validate
from paper.engine import canonical
from scripts.compact_shared_inputs import digest_file,save,table_rows


def prefix_proof(con,table,count):
    h=hashlib.sha256();n=0
    _,_,rows=table_rows(con,table)
    for row in itertools.islice(rows,count):
        values=[{'blob':bytes(v).hex()} if isinstance(v,(bytes,memoryview)) else v for v in row]
        h.update((canonical(values)+'\n').encode());n+=1
    return {'rows':n,'sha256':h.hexdigest()}


def finalize(data,work,apply=False):
    data,work=Path(data).resolve(),Path(work).resolve()
    if work.parent!=data or work.is_symlink(): raise ValueError('Migration work must be a direct data child')
    state=json.loads((work/'migration.json').read_text())
    if state['phase']!='switched' or len(state['switched'])!=len(state['rows']): raise ValueError('Migration is incomplete')
    archive=sqlite3.connect((data/ARCHIVE).as_uri()+'?mode=ro',uri=True,timeout=10)
    try:
        for row in archive.execute('SELECT id,at,payload FROM inputs'):validate(*row)
    finally:archive.close()
    candidates=[]
    for record in state['rows']:
        name=record['source']
        if not re.fullmatch(r'long-hold-[a-z0-9-]+\.sqlite',name): raise ValueError('Invalid original ledger name')
        old=work/'originals'/name;current=data/name
        if old.is_symlink() or current.is_symlink(): raise ValueError('Symlink ledger refused')
        if not old.exists(): continue
        if digest_file(old)!=record['source_sha256']: raise ValueError('Original ledger hash changed')
        a=sqlite3.connect(old.as_uri()+'?mode=ro&immutable=1',uri=True)
        b=sqlite3.connect(current.as_uri()+'?mode=ro',uri=True,timeout=10);b.execute('PRAGMA query_only=ON');b.execute('BEGIN')
        try:
            if not marked(b): raise ValueError('Current ledger is not converted')
            contract="SELECT value FROM meta WHERE key='contract'"
            if a.execute(contract).fetchone()!=b.execute(contract).fetchone():raise ValueError('Instance contract changed')
            old_state=json.loads(a.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0])
            new_state=json.loads(b.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0])
            if old_state['started_at']!=new_state['started_at'] or new_state['last_at']<old_state['last_at']:
                raise ValueError('Instance state regressed')
            for table,expected in record['preserved_tables'].items():
                if table=='meta':continue
                if prefix_proof(b,table,expected['rows'])!=expected: raise ValueError('Historical rows changed: '+name+'/'+table)
            b.execute('ATTACH DATABASE ? AS archive',((data/ARCHIVE).as_uri()+'?mode=ro',))
            if b.execute('SELECT count(*) FROM input_refs r LEFT JOIN archive.inputs a ON r.id=a.id WHERE a.id IS NULL OR a.at!=r.at').fetchone()[0]:
                raise ValueError('Incomplete shared input coverage')
            # Original source reference identity/ordering is checked against the
            # current shared payload hashes before its only embedded copy is removed.
            h=hashlib.sha256();count=0
            for ident,at,payload in a.execute('SELECT id,at,payload FROM inputs ORDER BY rowid'):
                if b.execute('SELECT at FROM input_refs WHERE id=?',(ident,)).fetchone()!=(at,):raise ValueError('Original input reference lost')
                h.update((canonical([ident,at,hashlib.sha256(payload).hexdigest()])+'\n').encode());count+=1
            if h.hexdigest()!=record['input_sha256'] or count!=record['input_rows_converted']:raise ValueError('Original input proof changed')
        finally:a.close();b.close()
        candidates.append((old,record['source_sha256']))
    removed=0
    if apply:
        for path,expected in candidates:
            if digest_file(path)!=expected:raise ValueError('Original changed before reclamation')
            size=path.stat().st_size;path.unlink();removed+=size
    result={'status':'reclaimed' if apply else 'verified','files':len(candidates),
            'candidate_bytes':sum(r['source_bytes'] for r in state['rows'] if any(p.name==r['source'] for p,_ in candidates)),
            'removed_bytes':removed,'verified_at':time.time(),
            'scope':'Only verified superseded embedded-input ledger files; live data, migration proofs and Telegram backups retained'}
    save(work/'reclamation.json',result)
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--data',type=Path,required=True)
    p.add_argument('--work',type=Path,required=True);p.add_argument('--apply',action='store_true')
    args=p.parse_args();print(json.dumps(finalize(args.data,args.work,args.apply),indent=2))
