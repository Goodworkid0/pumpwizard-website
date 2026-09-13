import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from test_replay_package import fixture, quote, ROOT
from paper.replay_package import table_proof, TABLES
from paper.research_runner import ResearchRunner
from paper.shared_research import SharedResearch
from paper.engine import canonical
from scripts.replay_chain import verify_chain


class ChainTests(unittest.TestCase):
    def test_two_trusted_code_eras_and_continuity(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); source=root/'source'; source.mkdir(); fixture(source)
            artifact=root/'artifact'; artifact.mkdir()
            second=root/'release2'; (second/'paper').mkdir(parents=True)
            for path in (ROOT/'paper').glob('*.py'): shutil.copyfile(path,second/'paper'/path.name)
            path=second/'paper/ladder.py'; path.write_text(path.read_text()+'\n# Synthetic trusted release revision.\n')
            with sqlite3.connect(source/'ledger.sqlite') as db:
                contract=json.loads(db.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
            db.close()
            shared=SharedResearch(source/'shared.sqlite')
            runner=ResearchRunner(source/'ledger.sqlite',contract['config'],contract['ownership_config'],shared,contract['source'])
            segments=[]
            try:
                for era in range(2):
                    if era:
                        files=('ladder.py','model.py',*ResearchRunner.extra_code_files)
                        runner.contract['code_hash']=hashlib.sha256(b''.join((second/'paper'/n).read_bytes() for n in files)).hexdigest()
                        with runner.db: runner.db.execute("UPDATE meta SET value=? WHERE key='contract'",(canonical(runner.contract),))
                    checkpoint=artifact/f'checkpoint-{era}.sqlite'
                    target=sqlite3.connect(checkpoint); runner.db.backup(target); target.execute('PRAGMA journal_mode=DELETE'); target.close()
                    with gzip.open(artifact/f'inputs-{era}.jsonl.gz','wt') as stream:
                        for n in range(5):
                            at=1600+era*150+n*30
                            batch={'at':at,'quotes':[quote(at,180000+n*1000)],'evidence':{}}
                            ident=shared.prepare(batch,runner.market)
                            payload=shared.db.execute('SELECT payload FROM inputs WHERE id=?',(ident,)).fetchone()[0]
                            stream.write(canonical({'id':ident,'at':at,'payload_zlib_base64':base64.b64encode(payload).decode()})+'\n')
                            runner.step(batch)
                    expected={'state':runner.state,'tables':{t:table_proof(runner.db,t) for t in TABLES}}
                    (artifact/f'expected-{era}.json').write_text(canonical(expected))
                    segments.append({'release':str(era),'checkpoint':checkpoint.name,'inputs':f'inputs-{era}.jsonl.gz','expected':f'expected-{era}.json'})
            finally: runner.close(); shared.close()
            path=artifact/'chain.json'; path.write_text(canonical({'schema':'pumpwizard.replay-chain/v1','segments':segments}))
            releases={'0':str(ROOT),'1':str(second)}
            with self.assertRaises(ValueError): verify_chain(path,releases)
            result=verify_chain(path,releases,trusted=True)
            self.assertEqual(result['segments'],2); self.assertEqual(result['inputs'],10)
            self.assertEqual(result['scope'],'checkpoint_forward_only')
            with sqlite3.connect(artifact/'checkpoint-1.sqlite') as db:
                state=json.loads(db.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0]); state['cash_usd']+=1
                db.execute("UPDATE meta SET value=? WHERE key='state'",(canonical(state),))
            db.close()
            with self.assertRaises(ValueError): verify_chain(path,releases,trusted=True)
