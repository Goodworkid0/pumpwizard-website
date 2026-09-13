import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from paper.demo import batches
from paper.engine import Engine
from paper.feed import FeedError, PublicFeed, get_json, normalize
from paper.model import buy_quote, load_config, sell_quote, usable

ROOT = Path(__file__).resolve().parents[1]


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = load_config(ROOT / 'paper.config.json')
        self.cfg['failed_attempt_probability'] = 0
        self.path = Path(self.tmp.name) / 'test.sqlite'
        self.e = Engine(self.path, self.cfg, 'synthetic_demo')
        self.addCleanup(lambda: self.e.close())
        self.data = list(batches())

    def batch(self, n, factor=None):
        b = copy.deepcopy(self.data[n])
        b['snapshots'] = b['snapshots'][:1]
        if factor:
            s = b['snapshots'][0]
            s['x'] = 500e6 / math.sqrt(factor)
            s['y'] = 64.38 * math.sqrt(factor)
            s['real_x'], s['real_y'] = s['x'] - 279.9e6, s['y'] - 30
            s['market_cap_usd'] = s['y'] / s['x'] * 1e11
        return b

    def enter(self):
        for n in range(14):
            self.e.step(self.batch(n))
        self.assertEqual(len(self.e.state['positions']), 1)

    def test_round_trip_loses_fees_without_price_change(self):
        s = self.batch(0)['snapshots'][0]
        buy, err = buy_quote(s, 20, self.cfg)
        self.assertIsNone(err)
        sell, err = sell_quote(s, buy, self.cfg)
        self.assertIsNone(err)
        self.assertGreater(sell['proceeds_usd'], 18)
        self.assertLess(sell['proceeds_usd'], 19.2)

    def test_zero_friction_roundtrip_preserves_pool_principal(self):
        cfg = dict(self.cfg, protocol_fee_bps=0, route_fee_bps=0,
                   adverse_fill_bps=0, network_fee_sol=0, entry_rent_sol=0)
        s = self.batch(0)['snapshots'][0]
        buy, _ = buy_quote(s, 20, cfg)
        sell, _ = sell_quote(s, buy, cfg)
        self.assertAlmostEqual(sell['proceeds_usd'], 20, places=7)

    def test_larger_order_has_more_impact_and_hits_depth_limit(self):
        s = self.batch(0)['snapshots'][0]
        _, err = buy_quote(s, 100, self.cfg)
        self.assertEqual(err, 'position_too_large_for_real_liquidity')

    def test_entry_waits_for_later_price(self):
        for n in range(13):
            self.e.step(self.batch(n))
        self.assertEqual(len(self.e.state['positions']), 0)
        self.assertEqual(len(self.e.state['pending_entries']), 1)
        self.e.step(self.batch(13, factor=1.24))
        p = self.e.state['positions']['DemoRise']
        self.assertEqual(p['entry_delay_seconds'], 5)
        self.assertEqual(p['entry_at'], self.batch(13)['at'])
        self.assertAlmostEqual(p['entry_market_cap_usd'], 12876 * 1.24)

    def test_entry_range_rechecked_at_execution(self):
        for n in range(13):
            self.e.step(self.batch(n))
        self.e.step(self.batch(13, factor=4.0))
        self.assertFalse(self.e.state['positions'])
        self.assertFalse(self.e.state['pending_entries'])
        self.assertEqual(self.e.state['cash_usd'], 1000)

    def test_exit_allowed_outside_entry_range_and_delayed(self):
        self.enter()
        b = self.batch(14, factor=2.0)
        b['snapshots'][0]['quote_usd'] *= 2
        b['snapshots'][0]['market_cap_usd'] *= 2
        self.e.step(b)
        self.assertEqual(len(self.e.trades()), 0)
        b = self.batch(15, factor=1.99)
        b['snapshots'][0]['quote_usd'] *= 2
        b['snapshots'][0]['market_cap_usd'] *= 2
        self.e.step(b)
        self.assertEqual(len(self.e.trades()), 1)
        self.assertEqual(self.e.trades()[0]['exit_reason'], 'take_profit')
        self.assertEqual(self.e.trades()[0]['exit_delay_seconds'], 5)

    def test_missing_data_keeps_impaired_position_and_recovers_exit(self):
        self.enter()
        b = self.batch(25)
        b['snapshots'] = []
        self.e.step(b)
        p = self.e.state['positions']['DemoRise']
        self.assertEqual(p['status'], 'exit_pending')
        self.assertEqual(p['mark_value_usd'], 0)
        self.assertFalse(self.e.trades())
        self.assertEqual(self.e.state['cash_usd'], 980)
        self.e.step(self.batch(26))
        self.assertEqual(len(self.e.trades()), 1)
        self.assertFalse(self.e.state['positions'])

    def test_halt_preserves_exits(self):
        self.enter()
        self.e.state['day_halted'] = True
        self.e.step(self.batch(14, factor=1.0))
        self.e.step(self.batch(15, factor=1.0))
        self.assertEqual(len(self.e.trades()), 1)
        self.assertTrue(self.e.state['day_halted'])

    def test_failed_entry_charges_only_attempt_cost(self):
        for n in range(13):
            self.e.step(self.batch(n))
        self.e.config['failed_attempt_probability'] = 1
        self.e.step(self.batch(13))
        self.assertFalse(self.e.state['positions'])
        self.assertEqual(self.e.state['failed_attempts'], 1)
        self.assertAlmostEqual(self.e.state['cash_usd'], 999.985)
        self.assertAlmostEqual(self.e.report()['total_pnl_usd'], -0.015)

    def test_failed_exit_remains_open(self):
        self.enter()
        self.e.step(self.batch(14, factor=1.8))
        self.e.config['failed_attempt_probability'] = 1
        self.e.step(self.batch(15, factor=1.8))
        self.assertEqual(len(self.e.trades()), 0)
        self.assertEqual(self.e.state['positions']['DemoRise']['status'], 'exit_pending')

    def test_cash_equity_and_profit_reconcile(self):
        for b in self.data:
            self.e.step(b)
        r = self.e.report()
        self.assertEqual(r['closed_trades'], 2)
        self.assertEqual(len(r['positions']), 1)
        self.assertAlmostEqual(r['total_pnl_usd'], r['realized_net_pnl_usd'] + r['unrealized_pnl_usd'])
        self.assertAlmostEqual(r['equity_usd'], r['cash_usd'] + sum(p['mark_value_usd'] for p in r['positions']))
        self.assertEqual(r['unrealized_pnl_usd'], -20)

    def test_time_limit_trigger(self):
        self.enter()
        for n in range(14, 136):
            self.e.step(self.batch(n, factor=1.195))
        t = self.e.trades()[0]
        self.assertEqual(t['exit_reason'], 'time_limit')
        self.assertEqual(t['hold_seconds'], 605)

    def test_duplicate_batch_is_idempotent(self):
        self.e.step(self.batch(0))
        original = self.e.report()
        self.assertFalse(self.e.step(self.batch(0)))
        self.assertEqual(self.e.report(), original)

    def test_time_reversal_rejected(self):
        self.e.step(self.batch(1))
        with self.assertRaises(ValueError):
            self.e.step(self.batch(0))

    def test_atomic_rollback(self):
        with patch.object(self.e, '_signals', side_effect=RuntimeError('injected')):
            with self.assertRaises(RuntimeError):
                self.e.step(self.batch(0))
        self.assertIsNone(self.e.state['last_at'])
        self.assertEqual(self.e.db.execute('SELECT COUNT(*) FROM batches').fetchone()[0], 0)

    def test_restart_preserves_positions_and_dedupe(self):
        self.enter()
        original = self.e.report()
        self.e.close()
        self.e = Engine(self.path, self.cfg, 'synthetic_demo')
        self.assertEqual(self.e.report(), original)
        self.assertFalse(self.e.step(self.batch(13)))
        self.e.step(self.batch(14))
        self.assertEqual(len(self.e.state['positions']), 1)

    def test_changed_config_or_source_cannot_mix_runs(self):
        with self.assertRaises(ValueError):
            Engine(self.path, dict(self.cfg, position_usd=10), 'synthetic_demo')
        with self.assertRaises(ValueError):
            Engine(self.path, self.cfg, 'public_index_live')

    def test_replay_identical(self):
        for b in self.data[:30]:
            self.e.step(b)
        target = Path(self.tmp.name) / 'recording.jsonl'
        self.e.export_recording(target)
        other = Engine(Path(self.tmp.name) / 'replay.sqlite', self.cfg, 'synthetic_demo')
        try:
            for line in target.read_text().splitlines()[1:]:
                other.step(json.loads(line))
            self.assertEqual(other.report(), self.e.report())
        finally:
            other.close()

    def test_stale_future_migrated_and_mayhem_rejected(self):
        s = self.batch(0)['snapshots'][0]
        now = s['observed_at']
        for key, value, reason in [('source_at', now-100, 'source_stale'),
                                   ('source_at', now+100, 'future_timestamp'),
                                   ('complete', True, 'curve_completed_or_migrated'),
                                   ('mayhem', True, 'unsupported_mayhem_curve')]:
            self.assertEqual(usable(dict(s, **{key: value}), now, self.cfg), reason)

    def test_gap_restarts_observation_window(self):
        for n in range(8):
            self.e.step(self.batch(n))
        self.e.step(self.batch(20))
        self.assertFalse(self.e.state['pending_entries'])
        self.assertEqual(len(self.e.state['histories']['DemoRise']), 1)

    def test_price_valuation_mismatch_rejected(self):
        s = self.batch(0)['snapshots'][0]
        s['market_cap_usd'] *= 2
        self.assertEqual(usable(s, s['observed_at'], self.cfg), 'valuation_reserve_mismatch')

    def test_only_read_only_paths_allowed(self):
        for path in ['/api/trade', '/sendTransaction', '/coins/../trade', '/coins/bad']:
            with self.assertRaises(ValueError):
                get_json(path)

    def test_invalid_configuration(self):
        for key, value in [('mode', 'live'), ('position_usd', -1), ('poll_seconds', 0),
                           ('network_fee_sol', float('nan')), ('max_positions', 2.5)]:
            f = Path(self.tmp.name) / 'config.json'
            f.write_text(json.dumps(dict(self.cfg, **{key: value})))
            with self.assertRaises(ValueError):
                load_config(f)

    def test_reentry_waits_after_close_then_requires_a_new_signal(self):
        self.e.config['max_entry_attempts_per_mint'] = 3
        self.enter()
        self.e.step(self.batch(14, factor=1.6))
        self.e.step(self.batch(15, factor=1.6))
        self.assertEqual(len(self.e.trades()), 1)
        closed_at = self.e.trades()[0]['exit_at']
        self.assertEqual(self.e._reentry_reason('DemoRise', closed_at + 119), 'reentry_cooldown')
        for n in range(16, 39):
            self.e.step(self.batch(n, factor=1.6 + (n*5-75)*0.0015))
            self.assertFalse(self.e.state['positions'])
            self.assertFalse(self.e.state['pending_entries'])
        self.e.step(self.batch(39, factor=1.78))  # exactly two minutes after close
        self.assertEqual(len(self.e.state['pending_entries']), 1)
        self.e.step(self.batch(40, factor=1.7875))
        self.assertEqual(self.e.state['entry_attempts_by_mint']['DemoRise'], 2)
        self.assertEqual(len(self.e.state['positions']), 1)

    def test_attempt_limit_survives_restart(self):
        self.enter()
        self.assertEqual(self.e.watch_blocks()['DemoRise'], None)
        self.e.close()
        self.e = Engine(self.path, self.cfg, 'synthetic_demo')
        self.assertEqual(self.e._reentry_reason('DemoRise', self.batch(100)['at']), 'entry_attempt_limit')

    def test_failed_entry_consumes_attempt_and_starts_cooldown(self):
        self.e.config['max_entry_attempts_per_mint'] = 3
        for n in range(13):
            self.e.step(self.batch(n))
        self.e.config['failed_attempt_probability'] = 1
        self.e.step(self.batch(13))
        now = self.batch(13)['at']
        self.assertEqual(self.e.state['entry_attempts_by_mint']['DemoRise'], 1)
        self.assertEqual(self.e._reentry_reason('DemoRise', now + 119), 'reentry_cooldown')
        self.assertIsNone(self.e._reentry_reason('DemoRise', now + 120))

    def test_frequent_preset_signals_earlier_without_larger_exposure(self):
        cfg = load_config(ROOT / 'paper.frequent.config.json')
        cfg['failed_attempt_probability'] = 0
        other = Engine(Path(self.tmp.name) / 'frequent.sqlite', cfg, 'synthetic_demo')
        try:
            for n in range(8):
                b = self.batch(n)
                self.e.step(b)
                other.step(b)
            self.assertFalse(self.e.state['positions'])
            self.assertEqual(len(other.state['positions']), 1)
            self.assertEqual(other.config['position_usd'], self.cfg['position_usd'])
            self.assertEqual(other.config['max_exposure_usd'], self.cfg['max_exposure_usd'])
            features = other.state['positions']['DemoRise']['signal']
            self.assertEqual(features['long_window_seconds'], 30)
        finally:
            other.close()


class WatchRotationTests(unittest.TestCase):
    def setUp(self):
        self.cfg = load_config(ROOT / 'paper.frequent.config.json')
        self.cfg['max_watch_tokens'] = 2
        self.feed = PublicFeed(self.cfg)
        self.addCleanup(self.feed.close)

    def coin(self, mint, cap=15000, updated=1000):
        return {'mint': mint, 'market_cap_usd': cap, 'updated_at': updated,
                'last_trade_timestamp': updated*1000, 'complete': False,
                'quote_mint': '11111111111111111111111111111111'}

    def stale(self):
        return {'source_at': 900, 'observed_at': 990, 'complete': False, 'mayhem': False,
                'route': 'pump_bonding_curve_sol', 'market_cap_usd': 15000}

    def test_exhausted_and_stale_tokens_release_slots(self):
        self.feed.watch = {'exhausted': 900, 'stale': 900}
        self.feed.last_snapshots['stale'] = self.stale()
        self.feed.refresh_watch({'new': self.coin('new')}, 1000, blocks={'exhausted': None})
        self.assertEqual(set(self.feed.watch), {'new'})

    def test_open_positions_are_never_evicted(self):
        self.feed.watch = {'held': 900, 'cooldown': 950}
        self.feed.last_snapshots['held'] = self.stale()
        self.feed.refresh_watch({'new': self.coin('new')}, 1000, ['held'],
                                {'held': None, 'cooldown': 1050})
        self.assertEqual(set(self.feed.watch), {'held', 'new'})

    def test_discovery_prioritizes_entry_range(self):
        coins = {m: self.coin(m, cap) for m, cap in [('small', 5500), ('eligible', 12000), ('eligible2', 25000)]}
        self.feed.refresh_watch(coins, 1000)
        self.assertEqual(set(self.feed.watch), {'eligible', 'eligible2'})

    def test_stale_and_nonfinite_discovery_rejected(self):
        coins = {'stale': self.coin('stale', updated=900), 'bad': self.coin('bad', float('nan'))}
        self.feed.refresh_watch(coins, 1000)
        self.assertFalse(self.feed.watch)

    def test_cooldown_token_can_be_rediscovered_when_ready(self):
        coin = {'ready': self.coin('ready')}
        self.feed.refresh_watch(coin, 1000, blocks={'ready': 1001})
        self.assertFalse(self.feed.watch)
        self.feed.refresh_watch(coin, 1001, blocks={'ready': 1001})
        self.assertIn('ready', self.feed.watch)

    def test_long_out_of_range_watch_does_not_reinsert_immediately(self):
        self.feed.watch = {'small': 900}
        s = self.stale()
        s.update(source_at=1000, observed_at=1000, market_cap_usd=6000)
        self.feed.last_snapshots['small'] = s
        self.feed.refresh_watch({'small': self.coin('small', 6000)}, 1000)
        self.assertNotIn('small', self.feed.watch)


if __name__ == '__main__':
    unittest.main()
