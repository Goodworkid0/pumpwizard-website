"""Funding and unified account tests use isolated synthetic observations only."""
import copy
import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
import zlib

from migrate_account import migrate
from paper.account_view import Account
from paper.demo import batches
from paper.engine import Engine
from paper.funding import FundedScalp, FundedLadder
from paper.guarded import GuardedEngine
from test_ladder import curve, MINT

ROOT=Path(__file__).resolve().parents[1]


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);(self.root/'data').mkdir()
        self.scfg=json.loads((ROOT/'paper.cadence.config.json').read_text())
        self.scfg['failed_attempt_probability']=0
        self.lcfg=json.loads((ROOT/'ladder.config.json').read_text());self.lcfg['failed_attempt_probability']=0
        self.risk=json.loads((ROOT/'ownership.config.json').read_text())
        self.sp=self.root/'data/cadence.sqlite';self.lp=self.root/'data/ladder.sqlite'
        # Their source labels exercise the migration allowlist; fixtures never
        # leave this temporary directory or enter the real memory collector.
        e=FundedScalp(self.sp,self.scfg,'public_index_live',self.risk)
        e.step({'at':1000,'snapshots':[]});e.close()
        r=FundedLadder(self.lp,self.lcfg,self.risk)
        r.step({'at':1100,'quotes':[],'evidence':{}});r.close()
        self.scalp=None;self.ladder=None

    def open(self):
        self.scalp=FundedScalp(self.sp,self.scfg,'public_index_live',self.risk)
        self.ladder=FundedLadder(self.lp,self.lcfg,self.risk)
        self.addCleanup(self.scalp.close);self.addCleanup(self.ladder.close)
        return Account(self.scalp,self.ladder,self.root/'data/account.sqlite')

    def batch(self,t,cap=11000,status='clear'):
        q=curve(t,cap)
        return {'at':t,'quotes':[q],'evidence':{MINT:{'ownership':{'mint':MINT,'checked_at':t,
            'policy_hash':self.ladder.policy_hash,'status':status},'peak_cap':max(cap,11000)}}}

    def enter_ladder(self):
        for i in range(7):self.ladder.step(self.batch(1300+i*10,10500+i*60))
        self.ladder.step(self.batch(1365,10900))
        self.assertIn(MINT,self.ladder.state['positions'])

    def test_even_allocation_and_idempotence_preserve_account_capital(self):
        plan=migrate(self.root,at=1200)
        self.assertFalse(plan['applied']);self.assertEqual(plan['combined_equity_usd'],1100)
        self.assertFalse((self.root/'data/account.sqlite').exists())
        result=migrate(self.root,at=1200,apply=True)
        self.assertEqual(result['allocations']['scalp']['cash_usd'],550)
        self.assertEqual(result['allocations']['ladder']['cash_usd'],550)
        again=migrate(self.root,at=1300,apply=True)
        self.assertTrue(again['already_applied'])
        a=self.open();d=a.build(1201)
        self.assertEqual(d['portfolio']['equity_usd'],1100)
        self.assertEqual(d['performance']['net_pnl_usd'],0)
        self.assertEqual(self.scalp.config['virtual_balance_usd'],550)
        self.assertEqual(self.ladder.cfg['budget_usd'],550)
        self.assertEqual(d['account']['statement']['total'],3)
        transfer=d['account']['statement']['rows'][0]
        self.assertEqual(transfer['action'],'allocation_transfer')
        self.assertEqual(transfer['cash_delta_usd'],0)
        self.assertEqual(transfer['cash_after_usd'],1100)

    def test_partial_sales_reconcile_once_and_do_not_count_as_closed(self):
        migrate(self.root,at=1200,apply=True);a=self.open();self.enter_ladder()
        self.ladder.step(self.batch(1370,31000));self.ladder.step(self.batch(1375,32000))
        d=a.build(1376);sheet=d['account']['balance_sheet']
        self.assertEqual(d['performance']['buys'],1);self.assertEqual(d['performance']['sells'],1)
        self.assertEqual(d['performance']['closed_count'],0)
        self.assertEqual(d['portfolio']['open_count'],1)
        self.assertAlmostEqual(d['positions'][0]['cost_usd'],6)
        self.assertEqual(d['positions'][0]['strategy'],'ladder')
        self.assertAlmostEqual(sheet['equity_usd'],1100+sheet['realized_net_pnl_usd']+sheet['unrealized_pnl_usd'])
        self.assertAlmostEqual(sheet['reconciliation_difference_usd'],0)
        ledger=list(csv.DictReader(io.StringIO(a.export_statement())))
        cash=sum(float(r['cash_delta_usd']) for r in ledger)
        self.assertAlmostEqual(cash,sheet['cash_usd'])
        self.assertAlmostEqual(float(ledger[-1]['cash_after_usd']),cash)
        sales=[r for r in ledger if r['action']=='fill_partial_exit']
        self.assertEqual(len(sales),1)
        self.assertAlmostEqual(float(sales[0]['cost_usd']),4)
        self.assertAlmostEqual(d['performance']['fees_usd'],self.ladder.state['fees_usd'])
        self.assertEqual(a.build(1376,ledger_action='sales')['account']['statement']['total'],1)
        self.assertEqual(a.build(1376,ledger_strategy='scalp')['account']['statement']['total'],1)

    def test_final_exit_aggregates_previous_partials_for_win_statistics(self):
        migrate(self.root,at=1200,apply=True);a=self.open();self.enter_ladder()
        for t,cap in [(1370,31000),(1375,32000),(1380,19000),(1385,18500)]:self.ladder.step(self.batch(t,cap))
        d=a.build(1386)
        self.assertEqual(d['performance']['sells'],2)
        self.assertEqual(d['performance']['closed_count'],1)
        self.assertEqual(len(d['positions']),0)
        trade=d['journal']['rows'][0]
        self.assertEqual(trade['strategy'],'ladder')
        self.assertGreater(trade['quantity'],0)
        self.assertAlmostEqual(trade['pnl_usd'],d['portfolio']['total_pnl_usd'])
        self.assertEqual(len(list(csv.DictReader(io.StringIO(a.export_trades())))),1)

    def test_external_funding_and_internal_transfer_never_become_profit(self):
        migrate(self.root,at=1200,apply=True);a=self.open();d=a.build(1201)
        self.assertEqual([p['value'] for p in d['equity_curve']],[1000,1100,1100])
        self.assertEqual([p['period_pnl'] for p in d['equity_curve']],[0,0,0])
        self.assertEqual(d['performance']['max_drawdown_fraction'],0)
        # At this artificial 7-day boundary, only the second funding and transfer are inside.
        d=a.build(1050+7*86400,period='7d')
        self.assertEqual(d['performance']['capital_added_usd'],100)
        self.assertEqual(d['performance']['net_pnl_usd'],0)
        self.assertEqual(d['performance']['return_fraction'],0)

    def test_migration_preserves_existing_partial_position_and_cost(self):
        self.ladder=FundedLadder(self.lp,self.lcfg,self.risk)
        self.enter_ladder()
        self.ladder.step(self.batch(1370,31000));self.ladder.step(self.batch(1375,32000))
        before=copy.deepcopy(self.ladder.state['positions'][MINT]);self.ladder.close()
        result=migrate(self.root,at=1400,apply=True);a=self.open()
        self.assertEqual(self.ladder.state['positions'][MINT],before)
        self.assertAlmostEqual(result['allocations']['scalp']['equity_usd'],result['allocations']['ladder']['equity_usd'])
        d=a.build(1401)
        self.assertEqual(d['performance']['sells'],1)
        self.assertAlmostEqual(d['account']['balance_sheet']['reconciliation_difference_usd'],0)

    def test_funded_restart_and_recording_replay_preserve_state(self):
        migrate(self.root,at=1200,apply=True);a=self.open();self.enter_ladder()
        self.ladder.step(self.batch(1370,31000));self.ladder.step(self.batch(1375,32000))
        expected=copy.deepcopy(self.ladder.state)
        path=self.root/'ladder.jsonl';self.ladder.export_recording(path)
        self.ladder.close()
        self.ladder=FundedLadder(self.lp,self.lcfg,self.risk);self.addCleanup(self.ladder.close)
        self.assertEqual(self.ladder.state,expected);self.assertEqual(self.ladder.cfg['budget_usd'],550)
        replay=FundedLadder(self.root/'replay.sqlite',self.lcfg,self.risk);self.addCleanup(replay.close)
        lines=path.read_text().splitlines();header=json.loads(lines[0]);replay.load_replay_funding(header['funding'])
        self.assertEqual(header['config']['budget_usd'],100)
        for line in lines[1:]:replay.step(json.loads(line))
        self.assertEqual(replay.state,expected)
        self.assertEqual(replay.export_csv(),self.ladder.export_csv())

    def test_scap_funding_replay_preserves_own_result_and_capital(self):
        migrate(self.root,at=1200,apply=True);self.open()
        self.scalp.step({'at':1300,'snapshots':[]})
        path=self.root/'scalp.jsonl';self.scalp.export_recording(path)
        lines=path.read_text().splitlines();header=json.loads(lines[0])
        replay=FundedScalp(self.root/'replay-scalp.sqlite',self.scfg,'public_index_live',self.risk,policy=header['entry_policy'])
        self.addCleanup(replay.close);replay.load_replay_funding(header['funding'])
        for line in lines[1:]:replay.step(json.loads(line))
        self.assertEqual(replay.state,self.scalp.state)
        self.assertAlmostEqual(replay.report()['total_pnl_usd'],0)

    def test_allocation_writer_lock_blocks_migration(self):
        r=FundedLadder(self.lp,self.lcfg,self.risk);self.addCleanup(r.close)
        with self.assertRaisesRegex(ValueError,'Stop the collector'):migrate(self.root,at=1200,apply=True)
        self.assertFalse((self.root/'data/account.sqlite').exists())

    def test_failed_transfer_rolls_back_both_ledgers(self):
        from unittest.mock import patch
        from paper.funding import adjusted_state
        calls=[]
        def failing(state,amount,strategy):
            calls.append(strategy)
            if strategy=='ladder':raise RuntimeError('Injected failure between allocation writes')
            return adjusted_state(state,amount,strategy)
        with patch('migrate_account.adjusted_state',side_effect=failing):
            with self.assertRaises(RuntimeError):migrate(self.root,at=1200,apply=True)
        self.assertEqual(calls,['scalp','ladder'])
        for path,capital in [(self.sp,1000),(self.lp,100)]:
            with sqlite3.connect(path) as con:
                meta=dict(con.execute('SELECT key,value FROM meta'))
                self.assertNotIn('account_funding',meta)
                self.assertEqual(json.loads(meta['state'])['cash_usd'],capital)
        with sqlite3.connect(self.root/'data/account.sqlite') as con:
            self.assertEqual(con.execute('SELECT count(*) FROM transfers').fetchone()[0],0)

    def test_unexplained_cash_change_is_detected(self):
        migrate(self.root,at=1200,apply=True);a=self.open()
        self.scalp.state['cash_usd']+=5
        with self.assertRaisesRegex(ValueError,'cash'):a.build(1300)

    def test_reserved_cash_and_period_statement_filters(self):
        migrate(self.root,at=1200,apply=True);a=self.open()
        for i in range(7):self.ladder.step(self.batch(1300+i*10,10500+i*60))
        d=a.build(1361)
        self.assertEqual(d['account']['balance_sheet']['reserved_cash_usd'],10)
        self.assertEqual(d['account']['balance_sheet']['available_cash_usd'],1090)
        self.assertEqual(a.build(1361,ledger_action='orders')['account']['statement']['total'],1)
        self.assertEqual(a.build(1361,ledger_search='no-such-action')['account']['statement']['total'],0)
        with self.assertRaises(ValueError):a.build(1361,ledger_strategy='bogus')


if __name__=='__main__':unittest.main()
