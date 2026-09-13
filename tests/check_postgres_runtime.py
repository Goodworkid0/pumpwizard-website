"""Actual PostgreSQL/SQLite shadow transitions in a disposable local cluster."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from test_replay_package import fixture, quote
from paper.postgres_runtime import Backend
from paper.research_runner import ResearchRunner
from paper.replay_package import table_proof, TABLES
from paper.shared_research import SharedResearch
from paper.ladder import LadderRunner
from paper.versioned_trials import TrialLadderRunner, TrialResearchRunner
from paper.forward_trials_v3 import V3LadderRunner, V3ResearchRunner
from scripts.backup_postgres_runtime import backup_runtime
from scripts.backup_lab import restore

BIN=Path('/usr/lib/postgresql/15/bin')


def runner_matrix(dsn,root):
    import sqlite3
    v2={'trial_id':'synthetic','group':'persistent','arm':'combined','label':'combined',
        'parent_id':'original','control_id':'control','risk_enabled':False,'ownership_required':False,
        'ownership_telemetry':True,'exits_enabled':True,'profit_activation_return':.2,'profit_trailing_drawdown':.1}
    v3={'trial_id':'synthetic','generation':'V3','comparison_title':'Test','group':'persistent',
        'arm':'combined','label':'combined','parent_id':'original','control_id':'control',
        'confirmation_enabled':True,'protection_enabled':True,'confirmation_delay_seconds':60,
        'break_even_activation_return':.1}
    for index,(cls,definition) in enumerate(((LadderRunner,None),(TrialLadderRunner,v2),
            (V3LadderRunner,v3),(ResearchRunner,None),(TrialResearchRunner,v2),(V3ResearchRunner,v3))):
        folder=root/cls.__name__; folder.mkdir(); fixture(folder,cls,definition)
        twin=folder/'twin'; twin.mkdir()
        for name in ('ledger.sqlite','shared.sqlite'):
            with sqlite3.connect(folder/name) as source, sqlite3.connect(twin/name) as target: source.backup(target)
            source.close(); target.close()
        backend=Backend(dsn,'matrix'+str(index)); runners=[]; archives=[]
        try:
            backend.import_ledger(twin/'shared.sqlite',frozen=True)
            backend.import_ledger(twin/'ledger.sqlite',frozen=True,
                                  shared_name='shared.sqlite' if issubclass(cls,ResearchRunner) else None)
            for directory in (folder,twin):
                archive=SharedResearch(directory/'shared.sqlite'); archives.append(archive)
                with sqlite3.connect(directory/'ledger.sqlite') as con:
                    contract=json.loads(con.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
                con.close()
                args=[directory/'ledger.sqlite',contract['config'],contract['ownership_config']]
                if issubclass(cls,ResearchRunner): args.append(archive)
                runner=cls(*args,source=contract['source'],**({'definition':definition} if definition else {}))
                runners.append(runner)
            archives[1].db.close(); archives[1].db=backend.database('shared.sqlite'); backend.attach(runners[1])
            for step in range(24):
                at=1600+step*30
                batch={'at':at,'quotes':[quote(at,150000+step*3000 if step<20 else 60000)],'evidence':{}}
                for archive,runner in zip(archives,runners): archive.prepare(batch,runner.market); runner.step(batch)
                assert runners[0].state==runners[1].state,cls.__name__
            assert runners[0].report()==runners[1].report(),cls.__name__
            for table in TABLES: assert table_proof(runners[0].db,table)==table_proof(runners[1].db,table),(cls.__name__,table)
        finally:
            for runner in runners: runner.close()
            for archive in archives: archive.close()
            backend.close()
    print('All six Ladder/Research original, V2 and V3 runner types matched SQLite state, reports and table proofs.')


def run(dsn, root):
    runner_matrix(dsn,root)
    source=root/'source'; source.mkdir(); fixture(source)
    import sqlite3
    with sqlite3.connect(source/'ledger.sqlite') as db:
        contract=json.loads(db.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
    db.close()
    pg=Backend(dsn,'shadow')
    try:
        pg.import_ledger(source/'shared.sqlite',frozen=True)
        pg.import_ledger(source/'ledger.sqlite',frozen=True,shared_name='shared.sqlite')
        try: Backend(dsn,'shadow')
        except ValueError: pass
        else: raise AssertionError('Two writers accepted')
        a_shared=SharedResearch(source/'shared.sqlite')
        other=root/'other'; other.mkdir()
        import shutil
        # Frozen identical bootstraps; runtime PostgreSQL never writes back into SQLite.
        for name in ('ledger.sqlite','shared.sqlite'):
            with sqlite3.connect(other/name) as target:
                source_db=sqlite3.connect(source/name); source_db.backup(target); source_db.close()
            target.close()
        b_shared=SharedResearch(other/'shared.sqlite')
        b_shared.db.close(); b_shared.db=pg.database('shared.sqlite')
        a=ResearchRunner(source/'ledger.sqlite',contract['config'],contract['ownership_config'],a_shared,contract['source'])
        b=ResearchRunner(other/'ledger.sqlite',contract['config'],contract['ownership_config'],b_shared,contract['source'])
        pg.attach(b)
        for index in range(30):
            at=1600+index*30; cap=150000+index*2000 if index<25 else 60000
            batch={'at':at,'quotes':[quote(at,cap)],'evidence':{}}
            a_shared.prepare(batch,a.market); b_shared.prepare(batch,b.market)
            assert a_shared.last_input_id==b_shared.last_input_id
            a.step(batch); b.step(batch); b.step(batch)  # duplicate is a no-op
            assert a.state==b.state
            for table in TABLES: assert table_proof(a.db,table)==table_proof(b.db,table),table
        assert a.report()==b.report()
        # Forced SQL failure must roll back state and ID allocation, including retry.
        before=table_proof(b.db,'events')
        try:
            with b.db:
                b.db.execute('INSERT INTO events(at,mint,action,data) VALUES (?,?,?,?)',(9999,'X','test','{}'))
                b.db.execute('INSERT INTO unknown VALUES (?)',('fail',))
        except Exception: pass
        else: raise AssertionError('Expected failed transaction')
        assert table_proof(b.db,'events')==before
        for runner in (a,b):
            with runner.db: runner.db.execute('INSERT INTO events(at,mint,action,data) VALUES (?,?,?,?)',(9999,'X','test','{}'))
        assert table_proof(a.db,'events')==table_proof(b.db,'events')
        ro=Backend(dsn,'shadow',writer=False)
        try:
            assert table_proof(ro.database('ledger.sqlite'),'trades')==table_proof(a.db,'trades')
            try: ro.database('ledger.sqlite').execute('INSERT INTO events(at,mint,action,data) VALUES (?,?,?,?)',(0,'X','bad','{}'))
            except Exception: pass
            else: raise AssertionError('Read-only backend wrote')
        finally: ro.close()
        state=copy.deepcopy(b.state); b.close()
        b=ResearchRunner(other/'ledger.sqlite',contract['config'],contract['ownership_config'],b_shared,contract['source'])
        pg.attach(b); assert b.state==state
        dump=root/'runtime.dump'
        subprocess.run([str(BIN/'pg_dump'),'-Fc','-f',str(dump),dsn],check=True)
        pg.lease.execute('CREATE DATABASE recovered_runtime')
        recovered_dsn=dsn.replace('dbname=postgres','dbname=recovered_runtime')
        subprocess.run([str(BIN/'pg_restore'),'--exit-on-error','-d',recovered_dsn,str(dump)],check=True)
        restored=Backend(recovered_dsn,'shadow',writer=False)
        try:
            for table in TABLES: assert table_proof(restored.database('ledger.sqlite'),table)==table_proof(b.db,table)
            restored_state=json.loads(restored.database('ledger.sqlite').execute("SELECT value FROM meta WHERE key='state'").fetchone()[0])
            assert restored_state==state
        finally: restored.close()
        a.close(); b.close(); a_shared.close(); b_shared.close()
        app=root/'backup-app'; (app/'data').mkdir(parents=True); fixture(app/'data')
        (app/'data/ledger.sqlite').rename(app/'data/long-hold-test.sqlite')
        (app/'data/shared.sqlite').rename(app/'data/shared-research.sqlite')
        backup_backend=Backend(dsn,'backup_test')
        try:
            backup_backend.import_ledger(app/'data/shared-research.sqlite',frozen=True)
            backup_backend.import_ledger(app/'data/long-hold-test.sqlite',frozen=True,shared_name='shared-research.sqlite')
            backup_backend.database('long-hold-test.sqlite').execute('INSERT INTO meta VALUES (?,?)',('runtime-only','preserved'))
            snapshot=root/'runtime-backup'
            backup_runtime(app,snapshot,dsn,'backup_test')
            recovered=root/'sqlite-recovery'; restore(snapshot,recovered)
            with sqlite3.connect(recovered/'data/long-hold-test.sqlite') as db:
                assert db.execute("SELECT value FROM meta WHERE key='runtime-only'").fetchone()[0]=='preserved'
            db.close()
        finally: backup_backend.close()
        print('PostgreSQL runtime: exact SQLite shadow state/tables, duplicate inputs, transactional IDs, rollback, restart, writer exclusion, read-only access and independent pg_dump/restore passed.')
        print('Authoritative PostgreSQL -> verified SQLite backup -> independent restore passed (not the stale source mirrors).')
    finally: pg.close()


if __name__=='__main__':
    with tempfile.TemporaryDirectory(prefix='pump-pg-runtime-') as temp:
        root=Path(temp); cluster=root/'pg'; sock=root/'sock'; sock.mkdir()
        subprocess.run([str(BIN/'initdb'),'-D',str(cluster),'-A','trust','-U','pump_test','--no-locale','--encoding=UTF8'],check=True,stdout=subprocess.DEVNULL)
        subprocess.run([str(BIN/'pg_ctl'),'-D',str(cluster),'-l',str(root/'pg.log'),'-o',f'-k {sock} -h ""','-w','start'],check=True,stdout=subprocess.DEVNULL)
        try: run(f'host={sock} user=pump_test dbname=postgres',root)
        finally: subprocess.run([str(BIN/'pg_ctl'),'-D',str(cluster),'-m','fast','-w','stop'],check=True,stdout=subprocess.DEVNULL)
