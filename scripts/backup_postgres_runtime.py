"""Export a PostgreSQL runtime snapshot into independently restorable SQLite ledgers.

The fleet/shared inputs use one PostgreSQL REPEATABLE READ snapshot. Non-runtime
SQLite telemetry is copied separately and explicitly is not globally atomic.
No source database is changed; the destination must be new.
"""
from contextlib import closing
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from paper.postgres_runtime import Backend, TABLES
from scripts.backup_lab import inventory, prepare_destination, database_check, digest, sync, write_manifest, verify


def backup_runtime(root,destination,dsn,namespace):
    import psycopg
    root,destination=Path(root).resolve(),Path(destination).absolute()
    if destination==root or root in destination.parents: raise ValueError('Keep backups outside the application root')
    sources,configs=inventory(root)
    config_hashes={p.name:digest(p) for p in configs}
    usage=shutil.disk_usage(destination.parent); floor=max(10*1024**3,usage.total*.10)
    def space(*_):
        if shutil.disk_usage(destination.parent).free<floor: raise ValueError('Backup disk reserve reached')
    space()
    prepare_destination(destination)
    manifest={'schema':'pumpwizard.backup/v1','started_at':time.time(),'files':[],'secrets_included':False,
              'consistency':'postgres-repeatable-read-fleet-plus-separate-online-sqlite-telemetry',
              'runtime_backend':'postgres','runtime_namespace':namespace}
    backend=Backend(dsn,namespace,writer=False)
    exported=set()
    def record(path,relative,started):
        path.chmod(0o600); sync(path)
        manifest['files'].append({'path':relative,'bytes':path.stat().st_size,'sha256':digest(path),
            'table_counts':database_check(path) if relative.startswith('data/') else None,
            'started_at':started,'completed_at':time.time()})
    try:
        with backend.connection() as con, con.transaction():
            con.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            schemas=con.execute('SELECT nspname FROM pg_namespace WHERE left(nspname,%s)=%s ORDER BY nspname',
                                (len(namespace)+1,namespace+'_')).fetchall()
            for (schema,) in schemas:
                name=con.execute('SELECT name FROM '+schema+'.runtime_identity').fetchone()[0]
                if backend.schema(name)!=schema: raise ValueError('Runtime identity mismatch')
                path=destination/'data'/name; started=time.time()
                tables=[r[0] for r in con.execute('SELECT tablename FROM pg_tables WHERE schemaname=%s AND tablename<>%s ORDER BY tablename',
                                                 (schema,'runtime_identity'))]
                if not set(tables)<=TABLES.keys(): raise ValueError('Unknown runtime table')
                with closing(sqlite3.connect(path)) as target, target:
                    for table in tables:
                        definition=TABLES[table].replace('DOUBLE PRECISION','REAL').replace('BYTEA','BLOB').replace('BIGINT','INTEGER')
                        target.execute('CREATE TABLE '+table+' ('+definition+')')
                        with con.cursor(name='export_'+table) as rows:
                            rows.itersize=64; rows.execute('SELECT * FROM '+schema+'.'+table)
                            for index,row in enumerate(rows):
                                if index%256==0: space()
                                target.execute('INSERT INTO '+table+' VALUES ('+','.join('?' for _ in row)+')',row)
                        space()
                exported.add(name); record(path,'data/'+name,started)
        required={p.name for p in sources if p.name.startswith('long-hold-') or p.name=='shared-research.sqlite'}
        if exported!=required: raise ValueError('PostgreSQL fleet and source registry differ; refusing an incomplete backup')
        for source in sources:
            if source.name in exported: continue
            target=destination/'data'/source.name; started=time.time()
            with closing(sqlite3.connect(source.as_uri()+'?mode=ro',uri=True)) as con, closing(sqlite3.connect(target)) as copy:
                con.backup(copy,pages=256,progress=space); copy.execute('PRAGMA journal_mode=DELETE')
            record(target,'data/'+source.name,started)
        for source in configs:
            target=destination/'config'/source.name; started=time.time(); shutil.copyfile(source,target)
            record(target,'config/'+source.name,started)
        if inventory(root)!=(sources,configs) or config_hashes!={p.name:digest(p) for p in configs}:
            raise ValueError('Source configuration or ledger inventory changed during backup')
        manifest['completed_at']=time.time(); write_manifest(destination,manifest)
        return verify(destination)
    finally: backend.close()


if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--root',type=Path,default=ROOT)
    p.add_argument('--destination',type=Path,required=True); p.add_argument('--namespace',required=True)
    args=p.parse_args()
    result=backup_runtime(args.root,args.destination,os.environ['PUMP_POSTGRES_DSN'],args.namespace)
    print(json.dumps({'status':'verified','files':len(result['files']),'consistency':result['consistency']}))
