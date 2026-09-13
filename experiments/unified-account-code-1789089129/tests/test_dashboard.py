import copy
import io
import json
import tempfile
import time
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from paper.brief import BriefService, facts_for, render_brief
from paper.dashboard import build_view, compact_curve, period_start
from paper.engine import Engine
from paper.model import load_config

ROOT = Path(__file__).resolve().parents[1]


def timestamp(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc).timestamp()


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(ROOT / 'paper.config.json')
        self.engine = Engine(Path(self.tmp.name)/'test.sqlite', self.cfg, 'synthetic_demo')
        self.addCleanup(self.engine.close)
        self.now = timestamp('2026-09-11T00:10:00')
        self.start = self.now - 10800
        self.engine.state.update(started_at=self.start, last_at=self.now,
                                 cash_usd=1015, last_source_status='ok')
        with self.engine.db:
            for t, v in [(self.start, 1000), (self.now-7200, 1005), (self.now, 1015)]:
                self.engine.db.execute('INSERT INTO equity(at,value,cash,impaired) VALUES (?,?,?,0)', (t,v,v))

    def trade(self, pnl, exit_at, mint='A'*44, symbol='TEST'):
        data = {'mint': mint, 'symbol': symbol, 'quantity': 10, 'entry_at': exit_at-60,
                'exit_at': exit_at, 'entry_price_usd': 2, 'exit_price_usd': (20+pnl)/10,
                'entry_market_cap_usd': 20000, 'cost_usd': 20, 'proceeds_usd': 20+pnl,
                'pnl_usd': pnl, 'return_fraction': pnl/20, 'fees_usd': 0.5,
                'hold_seconds': 60, 'exit_reason': 'time_limit'}
        with self.engine.db:
            self.engine.db.execute('INSERT INTO trades(mint,data) VALUES (?,?)', (mint,json.dumps(data)))

    def view(self, **kw):
        return build_view(self.engine,self.now,**kw)

    def test_today_uses_belgrade_midnight(self):
        self.assertEqual(period_start('today',self.now,self.start), timestamp('2026-09-10T22:00:00'))
        winter = timestamp('2026-12-11T12:00:00')
        self.assertEqual(period_start('today',winter,self.start), timestamp('2026-12-10T23:00:00'))

    def test_dst_change_uses_actual_midnight_offset(self):
        t = timestamp('2026-10-25T12:00:00')
        self.assertEqual(period_start('today',t,None), timestamp('2026-10-24T22:00:00'))

    def test_period_pnl_uses_prior_equity_not_whole_closed_trade_profit(self):
        self.trade(2,self.now-20)
        self.trade(100,self.start+20)
        view=self.view(period='today')
        self.assertEqual(view['performance']['net_pnl_usd'],15)
        self.assertEqual(view['performance']['closed_trade_pnl_usd'],2)
        self.assertEqual(view['performance']['closed_count'],1)

    def test_no_data_is_not_zero_return(self):
        self.engine.state['last_at']=self.start
        view=self.view(period='today')
        self.assertFalse(view['range']['has_data'])
        self.assertIsNone(view['performance']['net_pnl_usd'])
        self.assertFalse(view['equity_curve'])

    def test_longer_period_marks_partial_history_without_padding(self):
        view=self.view(period='30d')
        self.assertTrue(view['range']['partial_history'])
        self.assertEqual(view['equity_curve'][0]['at'],self.start)
        self.assertEqual(len(view['equity_curve']),3)

    def test_filters_sort_and_token_totals(self):
        self.trade(5,self.now-30)
        self.trade(-8,self.now-20)
        self.trade(3,self.now-10,mint='B'*44,symbol='Other')
        self.assertEqual([t['pnl_usd'] for t in self.view()['journal']['rows']],[3,-8,5])
        self.assertEqual([t['pnl_usd'] for t in self.view(outcome='winners',sort='best')['journal']['rows']],[5,3])
        self.assertEqual([t['pnl_usd'] for t in self.view(outcome='losers')['journal']['rows']],[-8])
        self.assertEqual(self.view()['losers'][0]['pnl_usd'],-3)
        self.assertEqual(self.view(search='oThEr')['journal']['total'],1)

    def test_pagination_reads_beyond_legacy_500_trade_limit(self):
        for n in range(515):
            self.trade(1,self.now-600+n,mint='B'*44 if n==0 else 'A'*44)
        view=self.view(search='B'*44)
        self.assertEqual(view['journal']['total'],1)
        self.assertEqual(self.view()['performance']['closed_count'],515)
        self.assertEqual(self.view(page=999)['journal']['page'],43)

    def test_open_positions_remain_visible_in_all_periods_and_newest_first(self):
        for mint,t in [('old',self.start),('new',self.now-30)]:
            self.engine.state['positions'][mint]={'mint':mint,'symbol':mint,'quantity':10,
                'entry_at':t,'entry_price_usd':2,'cost_usd':20,'mark_value_usd':18,
                'unrealized_pnl_usd':-2,'mark_reason':None,'status':'open'}
        view=self.view(period='today')
        self.assertEqual([p['mint'] for p in view['positions']],['new','old'])
        self.assertEqual(view['portfolio']['exposure_usd'],40)

    def test_activity_uses_real_events_not_skips(self):
        with self.engine.db:
            self.engine.event(self.now-5,'A'*44,'fill_entry','momentum',{'cost_usd':20,'entry_fees_usd':0.7,'symbol':'ONE'})
            self.engine.event(self.now-2,'A'*44,'skip','warming_up')
            self.engine.event(self.now-1,'A'*44,'failed_exit','modeled_execution_failure',{'fee_usd':0.02})
        view=self.view()
        self.assertEqual(len(view['activity']),2)
        self.assertEqual(view['performance']['buys'],1)
        self.assertEqual(view['performance']['failed_attempts'],1)
        self.assertAlmostEqual(view['performance']['fees_usd'],0.72)
        self.assertEqual(view['activity'][0]['action'],'failed_exit')

    def test_compact_curve_preserves_extrema_and_only_observed_points(self):
        points=[{'at':i,'value':1000+i%23} for i in range(10000)]
        points[4200]['value']=500
        points[5300]['value']=2000
        compact=compact_curve(points)
        self.assertLessEqual(len(compact),480)
        self.assertIn(points[4200],compact)
        self.assertIn(points[5300],compact)
        self.assertEqual(compact[0],points[0])
        self.assertEqual(compact[-1],points[-1])

    def test_invalid_query_rejected(self):
        for kw in [{'period':'all; DROP TABLE trades'}, {'outcome':'anything'}, {'sort':'drop'}]:
            with self.assertRaises(ValueError):
                self.view(**kw)

    def test_brief_must_report_halt_and_impaired_positions(self):
        view=self.view()
        view['portfolio'].update(halted=True,halt_reason='daily_loss',impaired_count=1)
        brief=render_brief(view,{'headline_id':'positive','fact_ids':['winner','costs']},provider='openai')
        self.assertIn('paused',brief['headline'])
        self.assertEqual(brief['fact_ids'][:2],['status','impaired'])

    def test_ai_cannot_add_unverified_numbers_or_instructions(self):
        view=self.view()
        brief=render_brief(view,{'headline_id':'Fake $99999 profit','fact_ids':['Transfer funds','costs']},provider='openai')
        self.assertNotIn('99999',json.dumps(brief))
        self.assertNotIn('Transfer funds',json.dumps(brief))

    def test_ai_usage_persists_and_is_rate_limited(self):
        cfg=json.loads((ROOT/'dashboard.config.json').read_text())
        cache=Path(self.tmp.name)/'briefs.json'
        requester=lambda packet: ({'headline_id':'active','fact_ids':['performance','exposure']},0.001)
        service=BriefService(cfg,cache,requester)
        with patch.dict('os.environ',{'OPENAI_API_KEY':'unit-test'}):
            service.get(self.view(),now=self.now)
            for _ in range(100):
                if not service.inflight: break
                time.sleep(0.01)
            result=service.get(self.view(),now=self.now+1)
            self.assertEqual(result['provider'],'openai')
            self.assertEqual(service.usage['requests'],1)
            again=BriefService(cfg,cache,requester)
            again.get(self.view(period='today'),now=self.now+2)
            self.assertEqual(again.usage['requests'],1)

    def test_ai_missing_key_and_budget_limit_use_labelled_fallback(self):
        cfg=json.loads((ROOT/'dashboard.config.json').read_text())
        cfg['summary_daily_budget_usd']=0
        service=BriefService(cfg,Path(self.tmp.name)/'briefs.json',lambda packet: self.fail('Unexpected paid call'))
        with patch.dict('os.environ',{'OPENAI_API_KEY':'unit-test'}):
            result=service.get(self.view(),now=self.now)
        self.assertEqual(result['provider'],'automatic')
        self.assertEqual(service.usage['requests'],0)

    def test_ai_quota_error_is_sanitized_and_stops_retries(self):
        cfg=json.loads((ROOT/'dashboard.config.json').read_text())
        def requester(packet):
            body=json.dumps({'error':{'code':'insufficient_quota','message':'secret-key-fragment'}}).encode()
            raise urllib.error.HTTPError('https://api.openai.com/v1/responses',429,'Too many requests',{},io.BytesIO(body))
        service=BriefService(cfg,Path(self.tmp.name)/'briefs.json',requester)
        with patch.dict('os.environ',{'OPENAI_API_KEY':'unit-test'}):
            service.get(self.view(),now=self.now)
            for _ in range(100):
                if not service.inflight: break
                time.sleep(0.01)
            result=service.get(self.view(),now=self.now+600)
        self.assertEqual(result['provider'],'automatic')
        self.assertIn('quota',result['status'])
        self.assertNotIn('secret-key-fragment',json.dumps(result))
        self.assertEqual(service.usage['requests'],1)

    def test_brief_describes_completed_and_settling_runs_without_a_loss_pause(self):
        for result, settling, expected in [('profit_target',False,'complete'),
                                           ('bankroll_depleted',False,'cannot fund'),
                                           (None,True,'Closing positions')]:
            self.engine.state.update(run_result=result,profit_exit_pending=settling)
            view=self.view()
            brief=render_brief(view)
            self.assertIn(expected,brief['headline'])
            self.assertNotIn('drawdown',brief['paragraphs'][0])
            self.assertEqual(view['portfolio']['halted'],True)

    def test_recent_buy_pace_is_live_even_when_history_filter_changes(self):
        with self.engine.db:
            self.engine.event(self.now-61,'A'*44,'fill_entry','momentum',{'cost_usd':10})
            self.engine.event(self.now-59,'A'*44,'fill_entry','momentum',{'cost_usd':10})
            self.engine.event(self.now-2,'A'*44,'failed_entry','modeled_execution_failure',{'fee_usd':0.01})
        for period in ['today','7d']:
            view=self.view(period=period)
            self.assertEqual(view['portfolio']['buys_last_minute'],1)
            self.assertEqual(view['portfolio']['attempts_last_minute'],2)
