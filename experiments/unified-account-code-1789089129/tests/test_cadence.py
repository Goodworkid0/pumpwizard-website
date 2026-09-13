"""Behavioral checks for the faster paper preset and rolling entry budget."""
import copy
import json
import math
import tempfile
import unittest
from pathlib import Path

from paper.demo import batches
from paper.engine import Engine
from paper.model import load_config, validate_config

ROOT = Path(__file__).resolve().parents[1]


class CadenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(ROOT / 'paper.cadence.config.json')
        self.cfg['failed_attempt_probability'] = 0
        self.path = Path(self.tmp.name) / 'cadence.sqlite'
        self.e = Engine(self.path, self.cfg, 'synthetic_demo')
        self.addCleanup(lambda: self.e.close())
        self.seed = next(iter(batches()))
        self.base = self.seed['at']

    def batch(self, seconds, count=8, factor=None):
        b = copy.deepcopy(self.seed)
        b['at'] = self.base + seconds
        b['snapshots'] = []
        f = factor if factor is not None else 1 + seconds * .001
        for i in range(count):
            s = copy.deepcopy(self.seed['snapshots'][0])
            s.update(mint=f'CadenceTest{i}', symbol=f'TEST{i}',
                     observed_at=b['at'], source_at=b['at'], fx_at=b['at'])
            s['x'], s['y'] = 500e6 / math.sqrt(f), 64.38 * math.sqrt(f)
            s['real_x'], s['real_y'] = s['x'] - 279.9e6, s['y'] - 30
            s['market_cap_usd'] = s['y'] / s['x'] * s['supply'] * s['quote_usd']
            b['snapshots'].append(s)
        return b

    def warm(self):
        for t in range(0, 21, 5):
            self.e.step(self.batch(t))

    def test_pending_signals_reserve_slots_and_rolling_cap_limits_fills(self):
        for t in range(0, 16, 5):
            self.e.step(self.batch(t))
        self.assertEqual(len(self.e.state['pending_entries']), 3)
        self.assertTrue(any(c['reason'] == 'buy_rate_limit' for c in self.e.state['candidates'].values()))
        for t in range(20, 91, 5):
            self.e.step(self.batch(t))
        fills = [r[0] for r in self.e.db.execute("SELECT at FROM decisions WHERE action='fill_entry' ORDER BY at")]
        self.assertEqual(len(fills), 6)
        for t in fills:
            self.assertLessEqual(sum(t-60 < at <= t for at in fills), 3)
        self.assertLessEqual(sum(p['cost_usd'] for p in self.e.state['positions'].values()), 60)

    def test_rate_window_survives_restart_and_expires_at_exact_boundary(self):
        self.warm()
        self.e.close()
        self.e = Engine(self.path, self.cfg, 'synthetic_demo')
        self.assertTrue(self.e._buy_rate_limited(self.base+79.999))
        self.assertFalse(self.e._buy_rate_limited(self.base+80))

    def test_failed_attempts_consume_the_minute_budget(self):
        self.e.config['failed_attempt_probability'] = 1
        self.warm()
        self.assertEqual(self.e.state['failed_attempts'], 3)
        self.assertFalse(self.e.state['positions'])
        for t in range(25, 80, 5):
            self.e.step(self.batch(t))
        self.assertEqual(self.e.state['failed_attempts'], 3)

    def test_execution_rechecks_the_rate_after_signal_delay(self):
        for t in range(0, 16, 5):
            self.e.step(self.batch(t))
        # Exercise delayed orders against a rate budget exhausted before their fill.
        self.e.state['recent_buy_attempts'] = [self.base+19]*3
        self.e.step(self.batch(20))
        self.assertFalse(self.e.state['positions'])
        self.assertEqual(len(self.e.state['pending_entries']), 3)

    def test_rate_cap_does_not_delay_exits_and_loss_halt_still_blocks_entries(self):
        self.warm()
        self.e.state['day_halted'] = True
        self.e.step(self.batch(25, factor=.8))
        self.e.step(self.batch(30, factor=.8))
        self.assertEqual(len(self.e.trades()), 3)
        self.assertFalse(self.e.state['positions'])
        self.assertFalse(self.e.state['pending_entries'])
        self.assertTrue(self.e.state['day_halted'])

    def test_faster_preset_preserves_exposure_but_disables_portfolio_loss_pauses(self):
        old = load_config(ROOT / 'paper.frequent.config.json')
        self.assertLess(self.cfg['position_usd'], old['position_usd'])
        self.assertGreater(self.cfg['max_positions'], old['max_positions'])
        for key in ['max_exposure_usd',
                    'market_cap_min_usd','market_cap_max_usd','protocol_fee_bps','route_fee_bps']:
            self.assertEqual(self.cfg[key], old[key])
        self.assertEqual(self.cfg['daily_loss_halt_fraction'],0)
        self.assertEqual(self.cfg['drawdown_halt_fraction'],0)
        self.assertLess(self.cfg['observation_seconds'],old['observation_seconds'])

    def test_invalid_rate_and_legacy_config_defaults(self):
        for rate in [-1, 1.5, 61, True]:
            with self.assertRaises(ValueError):
                validate_config(dict(self.cfg,max_buy_attempts_per_minute=rate))
        legacy = json.loads((ROOT/'paper.frequent.config.json').read_text())
        self.assertEqual(validate_config(legacy)['max_buy_attempts_per_minute'],0)

    def test_zero_loss_limits_allow_trading_after_a_large_drawdown(self):
        self.e.state['cash_usd'] = 700
        self.e.step(self.batch(0))
        self.assertFalse(self.e.state['day_halted'])
        self.assertFalse(self.e.state['drawdown_halted'])
        for t in range(5,21,5):
            self.e.step(self.batch(t))
        self.assertEqual(len(self.e.state['positions']),3)

    def use_small_profit_target(self):
        self.e.close()
        self.cfg['profit_target_usd'] = 5
        self.path = Path(self.tmp.name) / 'small-target.sqlite'
        self.e = Engine(self.path,self.cfg,'synthetic_demo')

    def test_profit_target_waits_for_delayed_exits_then_finishes_on_cash(self):
        self.use_small_profit_target()
        self.warm()
        self.e.step(self.batch(25,factor=1.5))
        self.assertTrue(self.e.state['profit_exit_pending'])
        self.assertIsNone(self.e.state['run_result'])
        self.assertEqual(len(self.e.state['positions']),3)
        self.e.step(self.batch(30,factor=1.5))
        self.assertEqual(self.e.state['run_result'],'profit_target')
        self.assertFalse(self.e.state['positions'])
        self.assertFalse(self.e.state['pending_entries'])
        self.assertGreaterEqual(self.e.state['cash_usd'],1005)
        result = self.e.report()
        self.e.close()
        self.e = Engine(self.path,self.cfg,'synthetic_demo')
        self.assertEqual(self.e.state['run_result'],'profit_target')
        self.e.step(self.batch(35,factor=1.6))
        self.assertEqual(self.e.state['cash_usd'],result['cash_usd'])
        self.assertFalse(self.e.state['pending_entries'])

    def test_profit_trigger_missed_by_exit_fills_resumes_instead_of_claiming_success(self):
        self.use_small_profit_target()
        self.warm()
        self.e.step(self.batch(25,factor=1.5))
        self.e.step(self.batch(30,factor=1.0))
        self.assertIsNone(self.e.state['run_result'])
        self.assertFalse(self.e.state['profit_exit_pending'])
        self.assertLess(self.e.state['cash_usd'],1005)
        self.assertIsNone(self.e._entry_block_reason())

    def test_unfunded_stops_below_order_size_but_not_while_exits_are_pending(self):
        self.warm()
        self.e.state['cash_usd'] = 1
        b = self.batch(60)
        b['snapshots'] = []
        self.e.step(b)
        self.assertIsNone(self.e.state['run_result'])
        self.assertEqual(len(self.e.state['positions']),3)
        self.assertTrue(all(p['mark_value_usd']==0 for p in self.e.state['positions'].values()))
        self.e.step(self.batch(65,factor=1.0))
        self.assertGreaterEqual(self.e.state['cash_usd'],10)
        self.assertIsNone(self.e.state['run_result'])
        self.e.state['cash_usd'] = 9
        self.e.step(self.batch(70,factor=1.0))
        self.assertEqual(self.e.state['run_result'],'bankroll_depleted')
        self.assertEqual(self.e.state['cash_usd'],9)
        self.assertFalse(self.e.state['positions'])
        self.assertFalse(self.e.state['pending_entries'])

    def test_disabled_profit_target_does_not_stop_a_profitable_run(self):
        self.e.config['profit_target_usd'] = 0
        self.warm()
        self.e.step(self.batch(25,factor=1.5))
        self.e.step(self.batch(30,factor=1.5))
        self.assertGreater(self.e.state['cash_usd'],1000)
        self.assertIsNone(self.e.state['run_result'])
