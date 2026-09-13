"""Opt-in full frozen package replay against a disposable PostgreSQL runtime."""
import argparse
import copy
import gzip
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import zlib

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from paper.postgres_runtime import Backend
from paper.replay_package import decode_input, runner_type, table_proof, TABLES
from paper.research_runner import ResearchRunner

BIN=Path('/usr/lib/postgresql/15/bin')


def run(package, dsn, root):
    contract=json.loads((package/'contract.json').read_text())
    expected=json.loads((package/'expected.json').read_text())
    cls=runner_type(contract)
    if not issubclass(cls,ResearchRunner): raise ValueError('This rehearsal expects a shared-input instance')
    shared=SimpleNamespace(features={},last_input_id=None,last_at=None)
    path=root/'ledger.sqlite'
    args=[path,contract['config'],contract['ownership_config'],shared]
    kwargs={'source':contract['source']}
    if 'experiment' in contract: kwargs['definition']=contract['experiment']
    runner=cls(*args,**kwargs); runner.close()
    # Import only the exact content-addressed inputs referenced by this package.
    archive=root/'shared.sqlite'; con=sqlite3.connect(archive)
    con.execute('CREATE TABLE inputs(id TEXT PRIMARY KEY, at REAL, payload BLOB)')
    with gzip.open(package/'inputs.jsonl.gz','rt') as stream, con:
        import base64
        for line in stream:
            row=json.loads(line); decode_input(row)
            con.execute('INSERT INTO inputs VALUES (?,?,?)',(row['id'],row['at'],base64.b64decode(row['payload_zlib_base64'])))
    con.close()
    backend=Backend(dsn,'real_replay')
    started=time.monotonic()
    try:
        backend.import_ledger(archive,frozen=True)
        backend.import_ledger(path,frozen=True,shared_name='shared.sqlite')
        runner=cls(*args,**kwargs); backend.attach(runner); count=0
        try:
            with gzip.open(package/'inputs.jsonl.gz','rt') as stream:
                for line in stream:
                    row=json.loads(line); batch=decode_input(row)
                    shared.features={(f['mint'],f['window']):{k:v for k,v in f.items() if k not in ('mint','window')} for f in batch.pop('features',[])}
                    shared.last_input_id=row['id']; shared.last_at=row['at']
                    runner.step(batch); count+=1
                    if count%2000==0: print('Replayed PostgreSQL inputs: '+str(count),flush=True)
            assert runner.state==expected['state'],'Final state differs'
            for table in TABLES: assert table_proof(runner.db,table)==expected['tables'][table],table
            return {'status':'verified','inputs':count,'seconds':round(time.monotonic()-started,2),'tables':expected['tables'],
                    'scope':'One frozen real instance, complete PostgreSQL replay; no live database cutover.'}
        finally: runner.close()
    finally: backend.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(); p.add_argument('--package',type=Path,required=True); p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists(): p.error('Output must be new')
    with tempfile.TemporaryDirectory(prefix='pump-pg-real-') as temporary:
        root=Path(temporary); cluster=root/'pg'; sock=root/'sock'; sock.mkdir()
        subprocess.run([str(BIN/'initdb'),'-D',str(cluster),'-A','trust','-U','pump_test','--no-locale','--encoding=UTF8'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run([str(BIN/'pg_ctl'),'-D',str(cluster),'-l',str(root/'pg.log'),'-o',f'-k {sock} -h ""','-w','start'],check=True,stdout=subprocess.DEVNULL)
        try:
            result=run(args.package,f'host={sock} user=pump_test dbname=postgres',root)
            with args.output.open('x') as stream: json.dump(result,stream,indent=2)
            print(json.dumps(result,indent=2))
        finally: subprocess.run([str(BIN/'pg_ctl'),'-D',str(cluster),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
