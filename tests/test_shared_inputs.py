import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zlib

from paper.engine import canonical
from paper.shared_inputs import Archive, attach, read_connection
from paper.shared_research import SharedResearch
from paper.ladder import LadderRunner
from paper.research_runner import ResearchRunner
from paper.versioned_trials import TrialLadderRunner, TrialResearchRunner
from paper.forward_trials_v3 import V3LadderRunner, V3ResearchRunner
from paper.replay_package import TABLES, table_proof, create, verify
from test_replay_package import fixture, quote

V2={'trial_id':'synthetic','group':'persistent','arm':'combined','label':'combined',
    'parent_id':'original','control_id':'control','risk_enabled':False,'ownership_required':False,
    'ownership_telemetry':True,'exits_enabled':True,'profit_activation_return':.2,'profit_trailing_drawdown':.1}
V3={'trial_id':'synthetic','generation':'V3','comparison_title':'Test','group':'persistent',
    'arm':'combined','label':'combined','parent_id':'original','control_id':'control',
    'confirmation_enabled':True,'protection_enabled':True,'confirmation_delay_seconds':60,
    'break_even_activation_return':.1}


class SharedInputsTests(unittest.TestCase):
    def test_all_six_runners_keep_state_trades_contracts_restart_and_recordings(self):
        for cls,definition in ((LadderRunner,None),(TrialLadderRunner,V2),(V3LadderRunner,V3),
                               (ResearchRunner,None),(TrialResearchRunner,V2),(V3ResearchRunner,V3)):
            with self.subTest(runner=cls.__name__), tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp); original=root/'original'; twin=root/'twin'
                original.mkdir(); twin.mkdir(); fixture(original,cls,definition)
                for name in ('ledger.sqlite','shared.sqlite'):
                    a=sqlite3.connect(original/name); b=sqlite3.connect(twin/name); a.backup(b); a.close(); b.close()
                archives=[]; runners=[]; shared_archive=Archive(twin/'shared.sqlite')
                try:
                    for directory in (original,twin):
                        shared=SharedResearch(directory/'shared.sqlite'); archives.append(shared)
                        con=sqlite3.connect(directory/'ledger.sqlite')
                        contract=json.loads(con.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0]); con.close()
                        args=[directory/'ledger.sqlite',contract['config'],contract['ownership_config']]
                        if issubclass(cls,ResearchRunner): args.append(shared)
                        runner=cls(*args,source=contract['source'],**({'definition':definition} if definition else {})); runners.append(runner)
                    if not issubclass(cls,ResearchRunner): attach(runners[1],shared_archive)
                    for index in range(24):
                        at=1600+index*30
                        batch={'at':at,'quotes':[quote(at,150000+index*3000 if index<20 else 60000)],'evidence':{}}
                        for shared,runner in zip(archives,runners): shared.prepare(batch,runner.market); runner.step(batch)
                        runners[1].step(batch)
                        self.assertEqual(runners[0].state,runners[1].state)
                    self.assertEqual(runners[0].report(),runners[1].report())
                    self.assertEqual(runners[0].contract,runners[1].contract)
                    for table in TABLES: self.assertEqual(table_proof(runners[0].db,table),table_proof(runners[1].db,table))
                    if not issubclass(cls,ResearchRunner):
                        self.assertEqual(runners[1].db.con.execute('SELECT count(*) FROM inputs').fetchone()[0],18)
                        self.assertEqual(runners[1].db.con.execute('SELECT count(*) FROM input_refs').fetchone()[0],24)
                        runners[0].export_recording(root/'a.jsonl'); runners[1].export_recording(root/'b.jsonl')
                        self.assertEqual((root/'a.jsonl').read_bytes(),(root/'b.jsonl').read_bytes())
                        state=copy.deepcopy(runners[1].state); runners[1].close()
                        runner=cls(twin/'ledger.sqlite',contract['config'],contract['ownership_config'],source=contract['source'],
                                   **({'definition':definition} if definition else {})); runners[1]=runner
                        attach(runner,shared_archive); self.assertEqual(runner.state,state)
                finally:
                    for runner in runners: runner.close()
                    for shared in archives: shared.close()
                    shared_archive.close()
                result=create(twin/'ledger.sqlite',twin/'shared.sqlite',root/'package',frozen_source=True)
                self.assertEqual(result,verify(root/'package'))

    def test_archive_identity_and_ledger_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); fixture(root,LadderRunner)
            con=sqlite3.connect(root/'ledger.sqlite'); con.row_factory=sqlite3.Row
            from types import SimpleNamespace
            runner=SimpleNamespace(db=con)
            archive=Archive(root/'shared.sqlite'); attach(runner,archive)
            try:
                raw=canonical({'at':2000,'quotes':[],'evidence':{}}).encode(); ident=hashlib.sha256(raw).hexdigest(); payload=zlib.compress(raw)
                with self.assertRaises(sqlite3.OperationalError):
                    with runner.db:
                        runner.db.execute('INSERT INTO inputs VALUES (?,?,?)',(ident,2000,payload))
                        runner.db.execute('INSERT INTO unknown VALUES (?)',(1,))
                self.assertEqual(con.execute('SELECT count(*) FROM input_refs').fetchone()[0],0)
                self.assertIsNotNone(archive.db.execute('SELECT id FROM inputs WHERE id=?',(ident,)).fetchone())
                with runner.db: runner.db.execute('INSERT INTO inputs VALUES (?,?,?)',(ident,2000,payload))
                self.assertEqual(con.execute('SELECT count(*) FROM input_refs').fetchone()[0],1)
                with self.assertRaises(ValueError): archive.put(ident,2001,payload)
                with self.assertRaises(ValueError): archive.put('0'*64,2000,payload)
            finally: runner.db.close(); archive.close()
