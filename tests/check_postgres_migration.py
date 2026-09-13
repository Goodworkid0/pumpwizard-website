"""An actual import/verify rehearsal in an isolated, temporary PostgreSQL cluster."""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from scripts.migrate_postgres import Postgres, inspect

class TestPostgres(Postgres):
    def execute(self, sql):
        result=subprocess.run(self.command,input='SET standard_conforming_strings=on;\n'+sql,text=True,env=self.env,capture_output=True)
        if result.returncode:raise AssertionError(result.stderr)

BIN=Path('/usr/lib/postgresql/15/bin')
with tempfile.TemporaryDirectory(prefix='pumpwizard-pg-test-') as temporary:
    root=Path(temporary); cluster=root/'cluster'; sockets=root/'sockets'; sockets.mkdir()
    subprocess.run([str(BIN/'initdb'),'-D',str(cluster),'-A','trust','-U','pump_test','--no-locale','--encoding=UTF8'],check=True,stdout=subprocess.DEVNULL)
    subprocess.run([str(BIN/'pg_ctl'),'-D',str(cluster),'-l',str(root/'postgres.log'),'-o',f"-k {sockets} -p 55439 -c listen_addresses=''",'-w','start'],check=True,stdout=subprocess.DEVNULL)
    try:
        pg=TestPostgres('migration_test',{'PGHOST':str(sockets),'PGPORT':'55439','PGUSER':'pump_test','PGDATABASE':'postgres'})
        pg.initialize()
        path=root/'long-hold-test.sqlite'
        con=sqlite3.connect(path);con.row_factory=sqlite3.Row
        con.executescript('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT); CREATE TABLE inputs(id TEXT PRIMARY KEY,payload BLOB); CREATE TABLE trades(id INTEGER PRIMARY KEY,mint TEXT,data TEXT); CREATE TABLE equity(id INTEGER PRIMARY KEY,at REAL,value REAL,cash REAL);')
        con.execute('INSERT INTO meta VALUES (?,?)',('state',json.dumps({'cash_usd':499,'positions':{},'weird':"quote '\t\n\\N"})))
        con.execute('INSERT INTO meta VALUES (?,?)',('contract',json.dumps({'config':{'budget_usd':500}})))
        con.execute('INSERT INTO inputs VALUES (?,?)',('shared-input',bytes(range(256))*2000))
        con.execute('INSERT INTO trades VALUES (1,?,?)',('mint',json.dumps({'pnl_usd':-1,'entry_at':1,'exit_at':2})))
        con.execute('INSERT INTO equity VALUES (1,2,499,499)');con.commit()
        meta=inspect(con)
        assert pg.import_source('rehearsal',path,con,meta)=='imported_and_verified'
        assert pg.import_source('rehearsal',path,con,meta)=='already_verified'
        assert pg.verify('rehearsal',path.stem,meta)
        assert pg.query("SELECT state->>'cash_usd' AS cash FROM migration_test.instance_state")[0]['cash']=='499'
        assert pg.query("SELECT trade->>'pnl_usd' AS pnl FROM migration_test.closed_positions")[0]['pnl']=='-1'
        # Source transactions roll back in full if validation fails.
        wrong=copy.deepcopy(meta);wrong['rows']+=1
        try:pg.import_source('must_rollback',path,con,wrong)
        except RuntimeError:pass
        else:raise AssertionError('Expected rollback')
        assert not pg.query("SELECT source_id FROM migration_test.sources WHERE batch_id='must_rollback'")
        # Changed source cannot overwrite an existing immutable batch.
        con.execute("UPDATE meta SET value='{}' WHERE key='state'");con.commit()
        try:pg.import_source('rehearsal',path,con,inspect(con))
        except ValueError:pass
        else:raise AssertionError('Changed source was accepted')
        # Destination tampering is detected by the full row digest validation.
        pg.execute("UPDATE migration_test.source_rows SET payload='{}' WHERE table_name='trades'")
        try:pg.verify('rehearsal',path.stem,meta)
        except ValueError:pass
        else:raise AssertionError('Corrupt destination was accepted')
        con.close()
        print('PostgreSQL rehearsal passed: exact text/blobs, counts/hashes, views, idempotence, rollback and tamper detection.')
    finally:
        subprocess.run([str(BIN/'pg_ctl'),'-D',str(cluster),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
