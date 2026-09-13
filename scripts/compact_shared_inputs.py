"""Verified lossless conversion into compact ledgers; explicit frozen-writer cutover."""
import argparse
from contextlib import ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from paper.shared_inputs import Archive, ARCHIVE, SCHEMA, validate
from paper.engine import canonical


def identifier(name): return '"'+name.replace('"','""')+'"'


def save(path,value):
    temporary=path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(value,stream,indent=2,allow_nan=False);stream.flush();os.fsync(stream.fileno())
    temporary.replace(path)
    sync_dir(path.parent)


def sync_dir(path):
    fd=os.open(path,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


def digest_file(path):
    with path.open('rb') as stream: return hashlib.file_digest(stream,'sha256').hexdigest()


def table_rows(con,name):
    columns=[row[1] for row in con.execute('PRAGMA table_info('+identifier(name)+')')]
    sql=con.execute('SELECT sql FROM sqlite_master WHERE type=\'table\' AND name=?',(name,)).fetchone()[0]
    without='WITHOUT ROWID' in sql.upper()
    selected=','.join(identifier(c) for c in columns)
    return columns,without,con.execute('SELECT '+('' if without else 'rowid,')+selected+
                                      ' FROM '+identifier(name)+' ORDER BY '+(selected if without else 'rowid'))


def proof(con,name):
    h=hashlib.sha256(); count=0
    _,_,rows=table_rows(con,name)
    for row in rows:
        values=[{'blob':bytes(v).hex()} if isinstance(v,(bytes,memoryview)) else v for v in row]
        h.update((canonical(values)+'\n').encode());count+=1
    return {'rows':count,'sha256':h.hexdigest()}


class Importer:
    def __init__(self,archive): self.archive=archive;self.cache={}
    def ensure(self,ident,at,payload):
        encoded=hashlib.sha256(payload).hexdigest()
        if ident in self.cache:
            if self.cache[ident]!=(at,encoded): raise ValueError('Conflicting historical input')
            return encoded
        validate(ident,at,payload)
        old=self.archive.db.execute('SELECT at,payload FROM inputs WHERE id=?',(ident,)).fetchone()
        if old is None: self.archive.db.execute('INSERT INTO inputs VALUES (?,?,?)',(ident,at,payload))
        elif old[0]!=at or old[1]!=payload: raise ValueError('Historical/archive input conflict')
        self.cache[ident]=(at,encoded)
        return encoded


def convert(source,destination,importer):
    """Caller owns writer locks; source snapshot and destination are never merged."""
    source,destination=Path(source),Path(destination)
    if source.is_symlink() or not source.is_file() or destination.exists(): raise ValueError('Unsafe conversion paths')
    con=sqlite3.connect(source.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
    con.execute('PRAGMA query_only=ON');con.execute('BEGIN')
    target=sqlite3.connect(destination)
    target.execute('PRAGMA journal_mode=DELETE');target.execute('PRAGMA synchronous=FULL')
    try:
        objects=con.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type,name").fetchall()
        tables=[name for kind,name,_ in objects if kind=='table']
        for kind,_,sql in objects:
            if kind=='table': target.execute(sql)
        with target:
            for name in tables:
                if name=='inputs': continue
                columns,without,rows=table_rows(con,name)
                names=columns if without else ['rowid',*columns]
                target.executemany('INSERT INTO '+identifier(name)+'('+','.join(identifier(c) for c in names)+') VALUES ('+
                                   ','.join('?' for _ in names)+')',rows)
            target.execute('CREATE TABLE IF NOT EXISTS input_refs(id TEXT PRIMARY KEY,at REAL)')
            target.execute('CREATE TABLE IF NOT EXISTS input_storage(key TEXT PRIMARY KEY,value TEXT)')
            old=target.execute("SELECT value FROM input_storage WHERE key='schema'").fetchone()
            if old and old[0]!=SCHEMA: raise ValueError('Unknown source storage version')
            target.execute('INSERT OR IGNORE INTO input_storage VALUES (?,?)',('schema',SCHEMA))
            count=0; payload_bytes=0; input_proof=hashlib.sha256()
            cursor=con.execute('SELECT id,at,payload FROM inputs ORDER BY rowid')
            while batch:=cursor.fetchmany(256):
                with importer.archive.db:
                    for ident,at,payload in batch:
                        encoded=importer.ensure(ident,at,payload)
                        input_proof.update((canonical([ident,at,encoded])+'\n').encode())
                        existing=target.execute('SELECT at FROM input_refs WHERE id=?',(ident,)).fetchone()
                        if existing is not None and existing[0]!=at: raise ValueError('Reference timestamp conflict')
                        target.execute('INSERT OR IGNORE INTO input_refs VALUES (?,?)',(ident,at))
                        count+=1;payload_bytes+=len(payload)
            for kind,_,sql in objects:
                if kind!='table': target.execute(sql)
            # Preserve AUTOINCREMENT sequence state, including removed rows.
            if con.execute("SELECT 1 FROM sqlite_master WHERE name='sqlite_sequence'").fetchone():
                target.execute('DELETE FROM sqlite_sequence')
                target.executemany('INSERT INTO sqlite_sequence VALUES (?,?)',con.execute('SELECT * FROM sqlite_sequence'))
        proofs={}
        for name in tables:
            if name in ('inputs','input_refs','input_storage'): continue
            before=proof(con,name); after=proof(target,name)
            if before!=after: raise ValueError('Non-input history differs: '+name)
            proofs[name]=before
        if 'input_refs' in tables:
            for ident,at in con.execute('SELECT id,at FROM input_refs'):
                if target.execute('SELECT at FROM input_refs WHERE id=?',(ident,)).fetchone()!=(at,):
                    raise ValueError('Existing reference changed')
        target.execute('ATTACH DATABASE ? AS archive',(importer.archive.path.resolve().as_uri()+'?mode=ro',))
        if target.execute('SELECT count(*) FROM input_refs r LEFT JOIN archive.inputs a ON r.id=a.id WHERE a.id IS NULL OR a.at!=r.at').fetchone()[0]:
            raise ValueError('Missing shared input or timestamp mismatch')
        if target.execute('PRAGMA main.quick_check').fetchall()!=[('ok',)]: raise ValueError('Compact SQLite integrity failed')
        target.execute('DETACH DATABASE archive')
        info=source.stat()
        target.close();target=None
        destination.chmod(info.st_mode&0o777)
        if os.geteuid()==0: os.chown(destination,info.st_uid,info.st_gid)
        with destination.open('rb') as stream: os.fsync(stream.fileno())
        return {'source':source.name,'source_bytes':info.st_size,'compact_bytes':destination.stat().st_size,
                'input_rows_converted':count,'input_payload_bytes':payload_bytes,'input_sha256':input_proof.hexdigest(),
                'preserved_tables':proofs,'source_sha256':digest_file(source),'compact_sha256':digest_file(destination)}
    finally:
        if target is not None: target.close()
        con.close()


def prepare(data):
    archive=Archive(data/ARCHIVE); importer=Importer(archive); total=0
    try:
        for path in sorted(data.glob('long-hold-*.sqlite')):
            con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=5)
            try:
                cursor=con.execute('SELECT id,at,payload FROM inputs ORDER BY rowid'); count=0
                while batch:=cursor.fetchmany(256):
                    with archive.db:
                        for row in batch: importer.ensure(*row);count+=1
                if count: print(json.dumps({'prepared':path.name,'inputs':count}),flush=True)
                total+=count
            finally: con.close()
        return {'status':'archive_prepared','input_occurrences':total,'unique_inputs_verified':len(importer.cache)}
    finally: archive.close()


def compact(data,work):
    if work.exists() or work.is_symlink(): raise ValueError('Use a new migration work directory')
    usage=shutil.disk_usage(data)
    if usage.free<max(10*1024**3,usage.total*.1)+8*1024**3: raise ValueError('Insufficient migration workspace')
    work.mkdir();(work/'compact').mkdir();(work/'originals').mkdir()
    archive=Archive(data/ARCHIVE); importer=Importer(archive)
    state={'schema':SCHEMA,'phase':'converting','data':str(data.resolve()),'rows':[],'switched':[],'started_at':time.time()}
    save(work/'migration.json',state)
    try:
        with ExitStack() as locks:
            paths=sorted(data.glob('long-hold-*.sqlite'))
            for path in paths:
                handle=locks.enter_context(open(str(path)+'.lock','a'))
                fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
            for path in paths:
                con=sqlite3.connect(path,timeout=5)
                try:
                    checkpoint=con.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                    if checkpoint[0]: raise ValueError('Live reader/writer prevented checkpoint')
                    count=con.execute('SELECT count(*) FROM inputs').fetchone()[0]
                finally: con.close()
                if not count: continue
                result=convert(path,work/'compact'/path.name,importer)
                state['rows'].append(result);save(work/'migration.json',state)
                print(json.dumps({k:result[k] for k in ('source','source_bytes','compact_bytes','input_rows_converted')}),flush=True)
            archive.db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            state['phase']='verified';save(work/'migration.json',state)
            for row in state['rows']:
                name=row['source'];source=data/name
                if digest_file(source)!=row['source_sha256']: raise ValueError('Source changed before switch')
                state['switching']=name;save(work/'migration.json',state)
                source.rename(work/'originals'/name)
                (work/'compact'/name).rename(source)
                sync_dir(data);sync_dir(work/'originals')
                state['switched'].append(name);state.pop('switching',None);save(work/'migration.json',state)
            # Auto-detection from ledger markers also supports portable restoration.
            save(data/'input-storage.json',{'schema':SCHEMA,'activated_at':time.time()})
            state['phase']='switched';state['completed_at']=time.time();save(work/'migration.json',state)
        return {k:v for k,v in state.items() if k not in ('rows','switched')}
    finally: archive.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('command',choices=['prepare','compact'])
    p.add_argument('--data',type=Path,required=True);p.add_argument('--work',type=Path)
    p.add_argument('--apply',action='store_true');p.add_argument('--frozen',action='store_true')
    args=p.parse_args()
    if not args.apply: p.error('Explicit --apply required')
    if args.command=='compact' and (not args.frozen or args.work is None): p.error('Stopped writers, --frozen and new --work required')
    result=prepare(args.data) if args.command=='prepare' else compact(args.data,args.work)
    print(json.dumps(result,indent=2))
