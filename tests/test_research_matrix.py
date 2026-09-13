import copy
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from paper.experiment_catalog import catalog, FAMILIES
from paper.ladder import validate_config
from paper.memory import Memory
from paper.research_runner import ResearchRunner
from paper.shared_research import SharedResearch, summarize

ROOT=Path(__file__).resolve().parents[1]
MINT='A'*44


def amm(at,cap=150000,mint=MINT,pool='pool'):
    return {'mint':mint,'symbol':'TEST','observed_at':at,'dex_id':'pumpswap','pair_address':pool,
            'valuation_basis':'fdv','market_cap_usd':cap,'price_usd':cap/1e9,
            'base_reserve':200000000,'liquidity_usd':cap*.4,'source_timestamp_known':False}


class MatrixTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.base=json.loads((ROOT/'long_hold_experiments.json').read_text())['experiments'][0]['config']
        self.entries=catalog(self.base)
        self.risk=json.loads((ROOT/'ownership.config.json').read_text())
        self.shared=SharedResearch(Path(self.tmp.name)/'shared.sqlite');self.addCleanup(self.shared.close)
        self.runner=self.make_runner('persistent')

    def make_runner(self,family):
        config=copy.deepcopy(next(e['config'] for e in self.entries if e['family']==family and e['pilot'] and e['config']['entry_cap_min']==100000))
        config['failed_attempt_probability']=0
        runner=ResearchRunner(Path(self.tmp.name)/(family+'.sqlite'),config,self.risk,self.shared,source='synthetic_test')
        self.addCleanup(runner.close)
        return runner

    def step(self,t,cap=150000,**kwargs):
        batch={'at':t,'quotes':[amm(t,cap,**kwargs)],'evidence':{}}
        self.shared.prepare(batch,self.runner.market);self.runner.step(batch)
        return batch

    def test_catalog_is_distinct_valid_and_pilot_is_balanced(self):
        self.assertEqual(len(self.entries),1000)
        self.assertEqual(len({e['id'] for e in self.entries}),1000)
        self.assertEqual(len({e['config_hash'] for e in self.entries}),1000)
        self.assertEqual(sum(e['pilot'] for e in self.entries),100)
        for family in FAMILIES:self.assertEqual(sum(e['pilot'] and e['family']==family for e in self.entries),10)
        for entry in self.entries:
            validate_config(entry['config']);self.assertLessEqual(len(entry['id']),40)

    def test_pumpswap_entry_exit_delayed_and_accounts_reconcile(self):
        for i in range(11):self.step(1000+30*i,150000+1000*i)
        self.assertIn(MINT,self.runner.state['pending']);self.assertFalse(self.runner.state['positions'])
        self.step(1330,161000)
        p=self.runner.state['positions'][MINT]
        self.assertEqual(p['amm_pool'],'pool');self.assertEqual(self.runner.state['cash_usd'],490)
        self.step(1360,80000);self.assertIn(MINT,self.runner.state['positions'])
        self.step(1390,78000);self.assertFalse(self.runner.state['positions'])
        report=self.runner.report();self.assertEqual(report['total_entries'],1)
        self.assertAlmostEqual(report['pnl_usd'],report['realized_net_pnl_usd']+report['unrealized_pnl_usd'])
        self.assertEqual(report['closed_count'],1)
        self.assertEqual(self.runner.db.execute('SELECT count(*) FROM inputs').fetchone()[0],0)
        self.assertGreater(self.runner.db.execute('SELECT count(*) FROM input_refs').fetchone()[0],0)

    def test_shared_input_replay_and_rollback(self):
        for i in range(11):self.step(1000+i*30,150000+i*1000)
        before=copy.deepcopy(self.runner.state);original=self.runner._step
        def fail(batch):original(batch);raise RuntimeError('injected write failure')
        with patch.object(self.runner,'_step',side_effect=fail):
            with self.assertRaises(RuntimeError):self.step(1330,161000)
        self.assertEqual(before,self.runner.state)
        batch=self.step(1330,161000)
        self.runner.step(batch)
        self.assertEqual(self.runner.report()['total_entries'],1)
        other=ResearchRunner(Path(self.tmp.name)/'replay.sqlite',self.runner.cfg,self.risk,self.shared,'synthetic_test')
        self.addCleanup(other.close)
        for row in self.runner.db.execute('SELECT id,at FROM input_refs ORDER BY at'):
            saved=self.shared.read(row['id'])
            self.shared.features={(f['mint'],f['window']):f for f in saved.pop('features')}
            self.shared.last_input_id=row['id'];self.shared.last_at=row['at']
            other.step(saved)
        self.assertEqual(other.state,self.runner.state)

    def test_pool_change_gap_future_quote_cannot_supply_history(self):
        for i in range(10):self.step(1000+i*30,150000+i*1000)
        self.step(1300,160000,pool='other')
        self.assertNotIn((MINT,300),self.shared.features)
        self.assertFalse(self.runner.state['pending'])
        self.step(1800,161000)
        self.assertNotIn((MINT,300),self.shared.features)
        self.assertIsNone(self.runner.market(amm(1900),1800))

    def test_family_specific_gates_and_relative_peers(self):
        market=self.runner.market(amm(2000),2000)
        base={'growth':.1,'first_half':.05,'second_half':.05,'recent_quarter':.01,'drawdown':.01,
              'higher_lows':True,'squeeze':True,'retest':True,'recovery':True,'liquidity_growth':.1,
              'samples':10,'start_at':1600,'end_at':2000,'route':'amm','pool':'pool','peer_count':5,'percentile':1}
        failed_fields={'persistent':('first_half',-.01),'higher-low':('higher_lows',False),
            'multi-window':('recent_quarter',-.01),'squeeze':('squeeze',False),'retest':('retest',False),
            'post-grad':('drawdown',.2),'liquidity':('liquidity_growth',0),
            'relative':('peer_count',1),'second-wave':('recovery',False),'recovery':('recovery',False)}
        for family in FAMILIES:
            runner=self.runner if family=='persistent' else self.make_runner(family)
            self.shared.features={(MINT,300):dict(base)}
            self.assertIsNone(runner.entry_reason(MINT,market,{'graduated_observed':True},2000),family)
            field,value=failed_fields[family];self.shared.features[(MINT,300)][field]=value
            self.assertIsNotNone(runner.entry_reason(MINT,market,{'graduated_observed':True},2000),family)

    def test_observation_panel_is_balanced_stable_and_expires_missing_quotes(self):
        from paper.brain_service import BrainService
        from paper.experiment_catalog import BANDS
        brain=BrainService.__new__(BrainService);brain.research_watch=[]
        rows=[{'mint':str(band)+'-'+str(i),'last_at':1000,'first_at':i,'last_cap':low+100,
               'quote':{'dex_id':'pumpswap'}} for band,(low,high) in enumerate(BANDS) for i in range(5)]
        first=brain.observation_panel(rows,1000,10).copy()
        self.assertEqual(len(first),10)
        self.assertEqual(first,brain.observation_panel(list(reversed(rows)),1100,10))
        for band in range(5):self.assertEqual(sum(m.startswith(str(band)) for m in first),2)
        self.assertEqual(brain.observation_panel(rows,2000,10),[])

    def test_reentry_requires_cooldown_and_entire_new_window(self):
        runner=self.make_runner('second-wave')
        runner.state['attempted']=[MINT];runner.state['last_exits']={MINT:1000}
        runner.update_histories({}, {},4599);self.assertIn(MINT,runner.state['attempted'])
        runner.update_histories({}, {},4600);self.assertNotIn(MINT,runner.state['attempted'])
        runner.event(4600,MINT,'failed_entry',{})
        self.assertNotIn(MINT,runner.state['last_exits'])

    def test_feature_patterns_use_ordered_observations(self):
        prices=[100,102,101,103,104,103,105,106,105,108,109]
        points=[{'at':1000+i*30,'price':v,'liquidity':100+i,'route':'amm','pool':'p'} for i,v in enumerate(prices)]
        feature=summarize(points,300,1300)
        self.assertTrue(feature['higher_lows']);self.assertGreater(feature['growth'],0)
        self.assertIsNone(summarize(points,300,1600))

    def test_simulation_ownership_is_recorded_not_an_exit_override(self):
        for i in range(12):self.step(1000+i*30,150000+i*1000)
        position=self.runner.state['positions'][MINT]
        market=self.runner.market(amm(1360,162000),1360)
        evidence={'ownership':{'mint':MINT,'status':'blocked'}}
        self.runner.exit_position(MINT,position,market,evidence,1360)
        self.assertNotIn('exit_order',position)
        self.runner.cfg['entry_policy']='strict'
        self.runner.exit_position(MINT,position,market,evidence,1360)
        self.assertEqual(position['exit_order']['reason'],'ownership_flag')

    def test_fill_markers_are_idempotent_with_shared_input_provenance(self):
        for i in range(12):self.step(1000+i*30,150000+i*1000)
        memory=Memory(Path(self.tmp.name)/'events.sqlite',json.loads((ROOT/'brain.config.json').read_text()))
        self.addCleanup(memory.close)
        memory.ingest_strategy_events('pilot',self.runner)
        memory.ingest_strategy_events('pilot',self.runner)
        self.assertEqual(memory.db.execute('SELECT count(*) FROM research_events').fetchone()[0],1)
        self.assertEqual(memory.db.execute('SELECT count(*) FROM research_outcomes').fetchone()[0],7)
        payload=json.loads(memory.db.execute('SELECT data FROM research_events').fetchone()[0])
        self.assertEqual(payload['input_id'],self.shared.last_input_id)
        self.assertEqual(payload['research_family'],'persistent')
        self.assertIsNotNone(payload['signal_features'])


class OutcomeTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.memory=Memory(Path(self.tmp.name)/'memory.sqlite',json.loads((ROOT/'brain.config.json').read_text()))
        self.addCleanup(self.memory.close)
        with self.memory.db:
            for ident,due in ((1,1000),(2,500),(3,1200)):
                self.memory.db.execute('''INSERT INTO research_outcomes
                    (strategy_id,event_id,window_seconds,mint,kind,due_at,reference_price,reference_cap)
                    VALUES (?,?,?,?,?,?,?,?)''',('s',ident,300,MINT,'fill_entry',due,.0001,100000))

    def test_late_quotes_do_not_count_as_on_time_and_future_stays_pending(self):
        with self.memory.db:self.memory.observe(amm(1020),1020,'dex',True)
        rows=list(self.memory.db.execute('SELECT quality FROM research_outcomes ORDER BY event_id'))
        self.assertEqual([r[0] for r in rows],['on_time','late','pending'])
        self.assertEqual(self.memory.research_view()['outcomes_completed'],1)
        out=list(csv.DictReader(io.StringIO(self.memory.export_research_csv())))
        outcomes=[r for r in out if r['record_type']=='forward_outcome']
        self.assertTrue(any(r['quality']=='late' and r['lateness_seconds']=='520.0' for r in outcomes))

    def test_failed_probe_backoff_and_expiry(self):
        self.assertIn(MINT,self.memory.due_outcome_mints(1000))
        self.memory.finish_research_probes([MINT],set(),1000)
        self.assertNotIn(MINT,self.memory.due_outcome_mints(1001))
        self.memory.finish_research_probes([],set(),1100)
        quality=self.memory.db.execute('SELECT quality FROM research_outcomes WHERE event_id=1').fetchone()[0]
        self.assertEqual(quality,'missed')

    def test_curve_price_is_derived_for_outcome(self):
        from test_ladder import curve
        with self.memory.db:self.memory.observe(curve(1020,20000),1020,'pump',True)
        row=self.memory.db.execute('SELECT price_usd,quality FROM research_outcomes WHERE event_id=1').fetchone()
        self.assertGreater(row['price_usd'],0);self.assertEqual(row['quality'],'on_time')


if __name__=='__main__':unittest.main()
