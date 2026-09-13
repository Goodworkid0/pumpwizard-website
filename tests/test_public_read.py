import copy
import json
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from paper.brain_service import BrainService
from paper.ladder import LadderRunner
from paper.memory import Memory
from paper.public_read import PublicReadService, ReadError, catalog, read_brain

ROOT = Path(__file__).resolve().parents[1]


class PublicReadTests(unittest.TestCase):
    def test_refresh_cadence_includes_build_time(self):
        with patch.object(self.service,'refresh_once'), patch.object(self.service.stop,'is_set',side_effect=[False,True]), \
                patch.object(self.service.stop,'wait') as wait, patch('paper.public_read.time.monotonic',side_effect=[100,110]):
            self.service._loop()
        wait.assert_called_once_with(5)

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name); (root/'data').mkdir()
        config = json.loads((ROOT/'long_hold_experiments.json').read_text())['experiments'][0]['config']
        risk = json.loads((ROOT/'ownership.config.json').read_text())
        runner = LadderRunner(root/'data/long-hold-test.sqlite', config, risk, source='synthetic_test')
        self.addCleanup(runner.close)
        memory = Memory(root/'data/coin-memory.sqlite', json.loads((ROOT/'brain.config.json').read_text()))
        self.addCleanup(memory.close)
        brain = BrainService.__new__(BrainService)
        brain.root=root; brain.runner=runner; brain.memory=memory; brain.primary_strategy={'id':'test','label':'Test','runner':runner}
        brain.experiment_runners=[]; brain.sampler=None; brain.ladder_enabled=True; brain.shared=None
        brain.strategy_lock=threading.RLock(); brain.research_catalog=[]; brain.status={}; brain.research_timing={}
        brain.community_path=root/'community.config.json'
        self.brain=brain; self.runner=runner
        self.service=PublicReadService(brain, autostart=False); self.addCleanup(self.service.close)

    def add_trades(self, first, last):
        with self.runner.db:
            for n in range(first,last+1):
                trade={'mint':'M'+str(n),'symbol':'T','entry_at':1,'exit_at':n+2,'entry_cap':100,'last_cap':90,
                    'pnl_usd':-1,'exit_reason':'stop_loss'}
                self.runner.db.execute('INSERT INTO trades(id,mint,data) VALUES (?,?,?)',(n,trade['mint'],json.dumps(trade)))

    def test_read_clone_uses_committed_state_and_cannot_write(self):
        state=copy.deepcopy(self.runner.state); state['cash_usd']=321
        with self.runner.db:
            self.runner.db.execute("INSERT OR REPLACE INTO meta VALUES ('state',?)",(json.dumps(state),))
        with read_brain(self.brain) as shadow:
            self.assertIsNot(shadow.runner.db,self.runner.db)
            self.assertIsNot(shadow.runner.lock,self.runner.lock)
            self.assertEqual(shadow.runner.state['cash_usd'],321)
            with self.assertRaises(sqlite3.OperationalError): shadow.runner.db.execute('DELETE FROM trades')
        self.assertNotEqual(self.runner.state['cash_usd'],321)

    def test_requests_never_rebuild_reports_and_stale_is_explicit(self):
        with self.assertRaises(ReadError) as exc: self.service.response('/api/public/summary',{})
        self.assertEqual(exc.exception.status,503)
        self.assertTrue(self.service.refresh_once())
        with patch.object(self.brain,'public_view',side_effect=AssertionError('request touched simulator')):
            for _ in range(50):
                data=json.loads(self.service.response('/api/public/summary',{}))
                self.assertNotIn('events',data['strategies'][0]['report'])
                self.assertEqual(data['strategies'][0]['report']['open_position_count'],0)
        with patch('paper.public_read.time.time',return_value=time.time()+100):
            self.assertTrue(json.loads(self.service.response('/api/public/summary',{}))['_snapshot']['stale'])
        self.service.last_error=True
        self.assertTrue(json.loads(self.service.response('/api/public/live-feed',{}))['_snapshot']['refresh_failed'])
        previous=self.service.generation
        with patch('paper.public_read.read_brain',side_effect=RuntimeError('synthetic failure')):
            with self.assertLogs('paper.public_read',level='ERROR'):
                self.assertFalse(self.service.refresh_once())
        self.assertIs(self.service.generation,previous)

    def test_history_keyset_has_no_gaps_when_new_trades_arrive(self):
        self.add_trades(1,250); self.assertTrue(self.service.refresh_once())
        first=json.loads(self.service.history('test',{'limit':['200']}))
        self.assertEqual(len(first['trades']),200); self.assertEqual(first['total'],250)
        self.add_trades(251,255)
        second=json.loads(self.service.history('test',{'limit':['200'],'snapshot_max_id':[str(first['snapshot_max_id'])],'before':[str(first['next_before'])]}))
        self.assertEqual(second['total'],250); self.assertIsNone(second['next_before'])
        self.assertEqual([r['id'] for r in first['trades']+second['trades']],list(range(250,0,-1)))
        self.assertEqual(json.loads(self.service.history('test',{}))['total'],255)
        for query in ({'limit':['201']},{'before':['-1']},{'snapshot_max_id':['bad']}):
            with self.assertRaises(ReadError): self.service.history('test',query)
        with self.assertRaises(ReadError): self.service.history('../test',{})

    def test_catalog_bounds_filters_and_comparison_outside_page(self):
        self.assertTrue(self.service.refresh_once())
        generation,_=self.service.snapshot()
        base=generation['catalog'][0]
        generation=copy.deepcopy(generation)
        generation['catalog']=[]
        for n in range(60):
            item=copy.deepcopy(base);item['id']='test-'+str(n);item['label']='Instance '+str(n)
            item['report']['pnl_usd']=n;item['report']['closed_win_rate']=None if n==59 else 0
            generation['catalog'].append(item)
        data=catalog(generation,{'page':['2'],'compare':['test-0,test-1']})
        self.assertEqual(data['total'],60);self.assertEqual(len(data['strategies']),24)
        self.assertEqual([s['id'] for s in data['comparison']],['test-0','test-1'])
        data=catalog(generation,{'q':['test-59']})
        self.assertEqual(data['total'],1)
        data=catalog(generation,{'sort':['closed_win_rate'],'page_size':['100']})
        self.assertEqual(data['strategies'][-1]['id'],'test-59')
        for query in ({'page_size':['101']},{'sort':['drop']},{'compare':['a,b,c,d,e']}):
            with self.assertRaises(ReadError): catalog(generation,query)

    def test_large_csv_refuses_instead_of_truncating(self):
        with self.runner.db:
            self.runner.db.executemany('INSERT INTO equity(at,value,cash) VALUES (?,500,500)',[(n,) for n in range(10001)])
        with self.assertRaises(ReadError) as exc: self.service.export('/api/public/history.csv')
        self.assertEqual(exc.exception.status,413)


if __name__=='__main__':unittest.main()
