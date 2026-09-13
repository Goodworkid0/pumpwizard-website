import copy
import json
import tempfile
import unittest
from pathlib import Path

from paper.demo import batches
from paper.engine import Engine
from paper.guarded import GuardedEngine
from paper.memory import Memory
from paper.model import load_config
from paper.ownership import TOKEN,TOKEN22,PUMP,analyze,check,curve_address
from paper.runner import Runner
from paper.ladder import validate_config
from paper.brain_service import experiment_entries

ROOT=Path(__file__).resolve().parents[1]
MINT='RgxTWibe59SwhpYnvZwRjRtXC19toUkzN2DQQ9fpump'


class ForwardCohortTests(unittest.TestCase):
    def test_long_hold_forward_manifest_is_valid_and_distinct(self):
        manifest=json.loads((ROOT/'long_hold_experiments.json').read_text())
        self.assertEqual(manifest['version'],'long-hold-forward-cohorts-v1')
        cohorts=manifest['experiments']
        self.assertEqual([c['id'] for c in cohorts],[
            'early-frequency','core-frequency','mid-cap-patient','momentum-conviction','upper-cap-swing',
            'pullback-reclaim','breakout-continuation','slow-burn-survivor','exit-study-twin',
            'reserve-surge','tight-range-breakout','recovery-patient','steady-trend'])
        self.assertEqual(len({c['id'] for c in cohorts}),len(cohorts))
        configs=[validate_config(c['config']) for c in cohorts]
        self.assertEqual([(c['entry_cap_min'],c['entry_cap_max']) for c in configs],
                         [(8000,15000),(10000,25000),(25000,50000),(50000,250000),(250000,1000000),
                          (15000,50000),(20000,75000),(25000,100000),(10000,25000),
                          (15000,35000),(30000,100000),(20000,75000),(75000,250000)])
        self.assertEqual([c['observation_seconds'] for c in configs],[30,30,45,110,90,180,120,900,30,60,300,300,600])
        self.assertEqual([c['entry_style'] for c in configs],[
            'momentum','momentum','momentum','momentum','momentum',
            'pullback_reclaim','breakout','slow_burn','momentum',
            'momentum','breakout','pullback_reclaim','slow_burn'])
        self.assertEqual(configs[7]['targets'][-1]['market_cap_usd'],10_000_000)
        expanded=experiment_entries(manifest)
        self.assertEqual(len(expanded),39)
        self.assertEqual(expanded[-1]['id'],'steady-trend-obs-12000s')
        self.assertEqual(expanded[-1]['config']['observation_seconds'],12000)
        self.assertEqual(expanded[-1]['config']['max_hold_seconds'],2592000)


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg=load_config(ROOT/'paper.config.json');self.cfg['failed_attempt_probability']=0
        self.risk=json.loads((ROOT/'ownership.config.json').read_text())
        self.data=list(batches());self.path=Path(self.tmp.name)/'test.sqlite'

    def guarded(self):
        return GuardedEngine(self.path,self.cfg,'synthetic_demo',self.risk)

    def tag(self,b,engine,status='clear',age=0,mint=None):
        b=copy.deepcopy(b)
        for s in b['snapshots']:
            s['entry_guard']={'mint':mint or s['mint'],'checked_at':b['at']-age,
                'policy_hash':engine.entry_policy['policy_hash'],'status':status}
        return b

    def test_missing_stale_wrong_mint_and_flagged_checks_block_entries(self):
        for status,age,mint in [('unknown',0,None),('blocked',0,None),('clear',121,None),('clear',0,'wrong'),('clear',-1,None)]:
            with self.subTest(status=status,age=age,mint=mint):
                p=Path(self.tmp.name)/f'{status}-{age}-{mint}.sqlite'
                e=GuardedEngine(p,self.cfg,'synthetic_demo',self.risk)
                for b in self.data[:16]:e.step(self.tag(b,e,status,age,mint))
                self.assertFalse(e.state['positions']);self.assertFalse(e.state['pending_entries']);e.close()

    def test_clear_check_is_rechecked_at_fill(self):
        e=self.guarded()
        try:
            for b in self.data[:13]:e.step(self.tag(b,e))
            self.assertTrue(e.state['pending_entries'])
            e.step(self.tag(self.data[13],e,'blocked'))
            self.assertFalse(e.state['positions'])
        finally:e.close()

    def test_gate_preserves_existing_positions_and_does_not_prevent_exits(self):
        old=Engine(self.path,self.cfg,'synthetic_demo')
        for b in self.data[:14]:old.step(b)
        cash=old.state['cash_usd'];held=list(old.state['positions']);old.close()
        e=self.guarded()
        try:
            self.assertEqual(e.state['cash_usd'],cash);self.assertEqual(list(e.state['positions']),held)
            for b in self.data[14:31]:e.step(self.tag(b,e,'blocked'))
            self.assertGreaterEqual(len(e.trades()),1)
        finally:e.close()

    def test_policy_cutover_and_guard_evidence_survive_replay(self):
        old=Engine(self.path,self.cfg,'synthetic_demo')
        for b in self.data[:10]:old.step(b)
        old.close();e=self.guarded()
        try:
            for b in self.data[10:35]:e.step(self.tag(b,e))
            out=Path(self.tmp.name)/'replay.jsonl';e.export_recording(out)
            lines=out.read_text().splitlines();header=json.loads(lines[0])
            other=GuardedEngine(Path(self.tmp.name)/'replay.sqlite',self.cfg,'synthetic_demo',self.risk,policy=header['entry_policy'])
            try:
                for line in lines[1:]:other.step(json.loads(line))
                self.assertEqual(e.report(),other.report())
            finally:other.close()
        finally:e.close()


class OwnershipTests(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads((ROOT/'ownership.config.json').read_text());self.now=100000
        self.mint={'owner':TOKEN,'data':{'parsed':{'type':'mint','info':{'supply':'10000','mintAuthority':None,'freezeAuthority':None}}}}
        self.holders=[self.account(f'a{i}',f'o{i}',100) for i in range(100)]

    def account(self,address,owner,amount):
        return {'pubkey':address,'account':{'owner':TOKEN,'data':{'parsed':{'type':'account','info':{
            'mint':MINT,'owner':owner,'state':'initialized','tokenAmount':{'amount':str(amount)}}}}}}

    def tx(self,n,source=None,destination=None,amount=0):
        instructions=[]
        if source:instructions=[{'programId':TOKEN,'parsed':{'type':'transfer','info':{'source':source,'destination':destination,'amount':str(amount)}}}]
        return {'blockTime':self.now-5,'meta':{'err':None,'preTokenBalances':[{'mint':MINT,'owner':'o0','accountIndex':0}],'postTokenBalances':[],'innerInstructions':[]},
            'transaction':{'signatures':[str(n)],'message':{'accountKeys':[],'instructions':instructions}}}

    def run_check(self,transactions=None,owners=None):
        return analyze(MINT,self.mint,self.holders,owners or {},transactions or [self.tx(n) for n in range(5)],self.cfg,self.now)

    def test_no_observed_flags_requires_complete_holder_and_recent_transaction_evidence(self):
        r=self.run_check();self.assertEqual(r['status'],'clear');self.assertAlmostEqual(r['top10_fraction'],.1)
        r=self.run_check([self.tx(1)]);self.assertEqual(r['status'],'unknown')
        self.holders.pop();self.assertEqual(self.run_check()['status'],'unknown')

    def test_aggregate_multiple_accounts_owned_by_one_wallet(self):
        for h in self.holders[:11]:h['account']['data']['parsed']['info']['owner']='same-whale'
        r=self.run_check();self.assertEqual(r['status'],'blocked');self.assertAlmostEqual(r['top_owner_fraction'],.11)

    def test_curve_vault_excluded_only_when_derived_and_program_verified(self):
        owner=curve_address(MINT)
        self.holders=[self.account('vault',owner,4000)]+[self.account(f'a{i}',f'o{i}',100) for i in range(60)]
        self.assertEqual(self.run_check()['status'],'blocked')
        r=self.run_check(owners={owner:{'owner':PUMP}})
        self.assertEqual(r['status'],'clear');self.assertEqual(r['excluded_vault_accounts'],1)
        self.assertAlmostEqual(r['top_owner_fraction'],100/6000)

    def test_restrictive_authorities_and_unknown_extensions_block(self):
        self.mint['data']['parsed']['info']['freezeAuthority']='controller'
        self.assertEqual(self.run_check()['status'],'blocked')
        self.mint['data']['parsed']['info']['freezeAuthority']=None
        self.mint['owner']=TOKEN22
        for h in self.holders:h['account']['owner']=TOKEN22
        self.mint['data']['parsed']['info']['extensions']=[{'extension':'permanentDelegate'}]
        self.assertEqual(self.run_check()['status'],'blocked')

    def test_large_fanout_and_circular_transfers_flag(self):
        for txs in [[self.tx(0,'a0','a1',600)],
                    [self.tx(i,'a0',f'a{i+1}',40) for i in range(3)],
                    [self.tx(0,'a0','a1',1),self.tx(1,'a1','a0',1)]]:
            r=self.run_check(txs+[self.tx(n+20) for n in range(5)])
            self.assertEqual(r['status'],'blocked')

    def test_rpc_failure_is_unknown_not_clear(self):
        from paper.ownership import EvidenceUnavailable
        def rpc(*args):raise EvidenceUnavailable('Ownership RPC rate limited')
        r=check(MINT,self.cfg,rpc,self.now)
        self.assertEqual(r['status'],'unknown');self.assertIn('rate limited',r['reasons'][0])

    def test_initial_mint_and_authority_revocation_are_not_suspicious_transfers(self):
        tx=self.tx(99)
        tx['transaction']['message']['instructions']=[{'programId':TOKEN,'parsed':{'type':kind,'info':info}}
            for kind,info in [('initializeMint2',{'mint':MINT}),('mintTo',{'mint':MINT,'amount':'10000'}),
                             ('setAuthority',{'account':'a0','newAuthority':None})]]
        self.assertEqual(self.run_check([tx]+[self.tx(n) for n in range(5)])['status'],'clear')
        tx['transaction']['message']['instructions'].pop(0)
        self.assertEqual(self.run_check([tx]+[self.tx(n) for n in range(5)])['status'],'blocked')


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.cfg=json.loads((ROOT/'brain.config.json').read_text())
        self.m=Memory(Path(self.tmp.name)/'memory.sqlite',self.cfg);self.addCleanup(self.m.close)

    def quote(self,t,cap=20000):
        return {'mint':MINT,'symbol':'TEST','market_cap_usd':cap,'price_usd':cap/1e9,'observed_at':t,
            'dex_id':'pumpswap','pair_address':'pool','liquidity_usd':20000,'base_reserve':1e9,'txns_m5':{'buys':15,'sells':12}}

    def test_twenty_to_fifty_thousand_milestone_and_repeat_poll_dedup(self):
        with self.m.db:
            for t in range(100,200):self.m.observe(self.quote(t),t,'dex')
            self.m.observe(self.quote(250,50000),250,'dex')
        c=self.m.evaluate(self.m.get(MINT),250)
        self.assertEqual(c['changes'],1);self.assertEqual(c['first_cap'],20000)
        self.assertEqual(c['growth_fraction'],1.5)
        self.assertIn(50000,[x['threshold'] for x in c['milestones']])
        self.assertNotEqual(c['stage'],'runner_candidate')

    def test_source_switch_is_not_activity_and_stale_quote_does_not_change_memory(self):
        q=self.quote(100)
        with self.m.db:self.m.observe(q,100,'dex')
        old=self.m.get(MINT)
        q.update(source_at=1)
        with self.m.db:self.m.observe(q,120,'pump')
        self.assertEqual(old,self.m.get(MINT))

    def test_ownership_flag_survives_expiry_and_unavailable_recheck(self):
        with self.m.db:self.m.store_ownership({'mint':MINT,'status':'blocked','checked_at':100,'reasons':['Large external transfer']})
        self.assertEqual(self.m.ownership(MINT,300)['status'],'blocked')
        with self.m.db:self.m.store_ownership({'mint':MINT,'status':'unknown','checked_at':301,'reasons':['RPC unavailable']})
        self.assertEqual(self.m.ownership(MINT,302)['status'],'blocked')
        with self.m.db:self.m.store_ownership({'mint':MINT,'status':'clear','checked_at':310,'reasons':[]})
        self.assertEqual(self.m.ownership(MINT,311)['status'],'clear')

    def test_memory_search_finds_coins_outside_default_visible_rows(self):
        with self.m.db:
            for n in range(105):
                q=self.quote(100+n);q['mint']='test'+str(n);q['symbol']='oldest' if n==0 else 'NEW'
                self.m.observe(q,100+n,'dex')
        self.assertEqual(len(self.m.view(205)['coins']),100)
        self.assertEqual(self.m.search('OLDEST',205)[0]['symbol'],'oldest')

    def test_longer_hold_needs_history_activity_and_ownership(self):
        with self.m.db:
            for n in range(75):self.m.observe(self.quote(100+n*300,20000+n*400),100+n*300,'dex')
        now=100+74*300
        self.assertNotEqual(self.m.evaluate(self.m.get(MINT),now)['stage'],'runner_candidate')
        with self.m.db:self.m.store_ownership({'mint':MINT,'status':'clear','checked_at':now,'reasons':[]})
        self.assertEqual(self.m.evaluate(self.m.get(MINT),now)['stage'],'runner_candidate')
        self.assertNotEqual(self.m.evaluate(self.m.get(MINT),now+181)['stage'],'runner_candidate')

    def test_runner_is_separate_and_cannot_backfill_historical_buys(self):
        runner=Runner(self.m,self.cfg)
        self.assertEqual(runner.report()['equity_usd'],100)
        with self.m.db:
            for n in range(75):self.m.observe(self.quote(100+n*300,20000+n*400),100+n*300,'dex')
        self.assertFalse(runner.state['positions']);self.assertFalse(runner.state['pending'])
        now=100+74*300
        with self.m.db:
            self.m.store_ownership({'mint':MINT,'status':'clear','checked_at':now,'reasons':[]})
            runner.step([self.quote(now,50000)],now)
        self.assertTrue(runner.state['pending']);self.assertFalse(runner.state['positions'])
        with self.m.db:runner.step([self.quote(now+30,50000)],now+30)
        self.assertEqual(len(runner.state['positions']),1)
        self.assertEqual(runner.state['cash_usd'],90)
        # An ownership failure forces a delayed exit even inside the intended one-day hold.
        with self.m.db:
            self.m.store_ownership({'mint':MINT,'status':'blocked','checked_at':now+60,'reasons':['transfer flag']})
            runner.step([self.quote(now+60,50000)],now+60)
        self.assertEqual(runner.state['positions'][MINT]['status'],'exit_pending')
        with self.m.db:runner.step([self.quote(now+90,50000)],now+90)
        self.assertFalse(runner.state['positions']);self.assertEqual(runner.report()['closed_count'],1)
        self.assertLess(runner.report()['equity_usd'],100)
