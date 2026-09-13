import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from paper.activity_status import activity_status
from paper.brief import facts_for,render_brief
from paper.dashboard import build_view
from paper.engine import Engine
from paper.memory import Memory
from paper.model import load_config

ROOT=Path(__file__).resolve().parents[1]


class ActivityStatusTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        root=Path(self.tmp.name);self.now=10000
        self.engine=Engine(root/'engine.sqlite',load_config(ROOT/'paper.config.json'),'synthetic_demo')
        self.addCleanup(self.engine.close)
        self.engine.entry_policy={'policy_hash':'test-policy'}
        self.engine.state.update(last_at=self.now,started_at=self.now-7200,last_source_status='ok',
            snapshots={'TEST':{'observed_at':self.now,'market_cap_usd':20000}})
        self.memory=Memory(root/'memory.sqlite',json.loads((ROOT/'brain.config.json').read_text()))
        self.addCleanup(self.memory.close)
        self.brain=SimpleNamespace(memory=self.memory,ladder_enabled=False)
        with self.engine.db:
            self.engine.db.execute("INSERT INTO decisions(at,mint,action,reason,data) VALUES (?,?,?,?,?)",(self.now-4000,'TEST','fill_exit','time_limit','{}'))
            for i in range(3):
                self.engine.db.execute("INSERT INTO decisions(at,mint,action,reason,data) VALUES (?,?,?,?,?)",(self.now-i*5,'TEST','skip','ownership_unverified','{}'))

    def screen(self,status,reasons,age=10):
        with self.memory.db:self.memory.store_ownership({'mint':'TEST','checked_at':self.now-age,'policy_hash':'test-policy','status':status,'reasons':reasons})

    def test_rpc_failure_is_visible_without_claiming_the_feed_stopped(self):
        self.screen('unknown',['Ownership RPC unavailable (HTTP 429)'])
        d=activity_status(self.engine,self.now,self.brain)
        self.assertEqual(d['status'],'ownership_unavailable');self.assertTrue(d['warning'])
        self.assertEqual(d['last_fill_at'],self.now-4000)
        self.assertEqual(d['reasons'],[{'reason':'ownership_unverified','evaluations':3,'coins':1}])
        self.assertEqual(d['checks_last_hour']['rpc_failures'],1)

    def test_fresh_clear_screen_and_recovered_connection_remove_rpc_warning(self):
        self.screen('clear',[])
        d=activity_status(self.engine,self.now,self.brain)
        self.assertFalse(d['warning']);self.assertEqual(d['fresh_ownership_passes'],1)
        self.assertEqual(d['status'],'scanning')

    def test_expired_clear_screen_does_not_count_as_a_pass(self):
        self.screen('clear',[],age=121)
        d=activity_status(self.engine,self.now,self.brain)
        self.assertEqual(d['fresh_ownership_passes'],0);self.assertEqual(d['status'],'ownership_pending')

    def test_feed_outage_takes_priority(self):
        self.screen('unknown',['Ownership RPC unavailable'])
        self.engine.state['last_at']=self.now-40
        self.assertEqual(activity_status(self.engine,self.now,self.brain)['status'],'feed_unavailable')

    def test_curated_brief_cannot_hide_the_entry_blocker(self):
        self.screen('unknown',['Ownership RPC unavailable'])
        view=build_view(self.engine,self.now,period='today')
        view['trading_status']=activity_status(self.engine,self.now,self.brain)
        brief=render_brief(view,{'headline_id':'active','fact_ids':['exposure','outcomes']})
        self.assertIn('entry_blocker',brief['fact_ids'])
        self.assertEqual(brief['headline'],'Entries waiting for ownership data.')
