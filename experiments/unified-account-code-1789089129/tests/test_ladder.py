import copy
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zlib

from paper.ladder import LadderRunner, validate_config

ROOT = Path(__file__).resolve().parents[1]
MINT = 'A'*44


def curve(t, cap, mint=MINT, fx=100):
    product = 39_600_000_000
    x = math.sqrt(product/(cap/1e9/fx))
    y = product/x
    return {'mint': mint, 'symbol': 'LADDER TEST', 'route': 'pump_bonding_curve_sol',
            'observed_at': t, 'source_at': t, 'fx_at': t, 'quote_usd': fx,
            'x': x, 'y': y, 'real_x': x-200_000_000, 'real_y': y-30,
            'supply': 1e9, 'market_cap_usd': cap, 'decimals': 6, 'complete': False,
            'curve_address': 'curve-'+mint, 'mayhem': False}


class LadderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cfg = json.loads((ROOT/'ladder.config.json').read_text())
        self.cfg['failed_attempt_probability'] = 0
        self.risk = json.loads((ROOT/'ownership.config.json').read_text())
        self.path = Path(self.tmp.name)/'ladder.sqlite'
        self.runner = self.open(self.path)

    def open(self, path):
        r = LadderRunner(path, self.cfg, self.risk, source='synthetic_test')
        self.addCleanup(r.close)
        return r

    def batch(self, t, cap=11000, quotes=None, status='clear'):
        if quotes is None:
            # The synthetic curve completes before $100k; later stages use an AMM.
            q = curve(t, cap) if cap < 95000 else {
                'mint': MINT, 'symbol': 'LADDER TEST', 'observed_at': t,
                'dex_id': 'pumpswap', 'pair_address': 'pair', 'valuation_basis': 'fdv',
                'market_cap_usd': cap, 'price_usd': cap/1e9,
                'base_reserve': 150_000_000, 'liquidity_usd': cap*.3}
            quotes = [q]
        mints = {q['mint'] for q in quotes} | {MINT}
        evidence = {m: {'ownership': {'mint': m, 'status': status, 'checked_at': t,
            'policy_hash': self.runner.policy_hash}, 'peak_cap': max(11000, cap),
            'curve_complete': any(q['mint']==m and q.get('dex_id')=='pumpswap' for q in quotes)} for m in mints}
        return {'at': t, 'quotes': quotes, 'evidence': evidence}

    def enter(self):
        for i in range(7):
            self.runner.step(self.batch(1000+i*10, 10500+i*60))
        self.assertIn(MINT, self.runner.state['pending'])
        self.assertFalse(self.runner.state['positions'])
        self.runner.step(self.batch(1065, 10900))
        self.assertIn(MINT, self.runner.state['positions'])
        return self.runner.state['positions'][MINT]

    def partial(self, t=1070, cap=31000):
        self.runner.step(self.batch(t, cap))
        self.runner.step(self.batch(t+5, cap))

    def reconcile(self):
        r = self.runner.report()
        self.assertAlmostEqual(r['pnl_usd'], r['realized_net_pnl_usd']+r['unrealized_pnl_usd'])
        self.assertGreaterEqual(r['cash_usd'], 0)

    def test_delayed_partial_sales_cost_basis_and_restart(self):
        original = copy.deepcopy(self.enter())
        self.runner.step(self.batch(1070, 31000))
        self.assertEqual(self.runner.report()['sale_count'], 0)
        self.runner.step(self.batch(1071, 31000))
        self.assertEqual(self.runner.report()['sale_count'], 0)
        self.runner.step(self.batch(1075, 32000))
        p = self.runner.state['positions'][MINT]
        self.assertAlmostEqual(p['quantity'], original['quantity']*.6)
        self.assertAlmostEqual(p['remaining_cost_usd'], 6)
        sale = self.runner.report()['trades'][0]
        self.assertEqual(sale['exit_cap'], 32000)  # actual later quote, not the target
        self.assertAlmostEqual(sale['cost_usd'], 4)
        self.assertGreater(sale['exit_fees_usd'], .10)
        self.assertEqual(self.runner.report()['closed_count'], 0)
        self.reconcile()
        self.runner.close(); self.runner = self.open(self.path)
        self.assertEqual(self.runner.state['positions'][MINT]['sold_stages'], [0])
        self.partial(1080, 62000)
        self.assertAlmostEqual(self.runner.state['positions'][MINT]['remaining_cost_usd'], 3)
        self.partial(1090, 102000)
        p = self.runner.state['positions'][MINT]
        self.assertAlmostEqual(p['quantity']/p['initial_quantity'], .1)
        self.assertAlmostEqual(p['remaining_cost_usd'], 1)
        self.assertEqual(self.runner.report()['sale_count'], 3)
        self.assertEqual(self.runner.report()['closed_count'], 0)
        # Partial reductions themselves never trip trailing loss; price has not fallen.
        self.assertNotIn('exit_order', p)
        self.reconcile()

    def test_gap_over_multiple_targets_sells_original_fractions_once(self):
        self.enter(); self.partial(cap=105000)
        p = self.runner.state['positions'][MINT]
        self.assertEqual(p['sold_stages'], [0, 1, 2])
        self.assertAlmostEqual(p['quantity']/p['initial_quantity'], .1)
        self.assertEqual(self.runner.report()['sale_count'], 1)
        self.runner.step(self.batch(1080, 106000))
        self.assertEqual(self.runner.report()['sale_count'], 1)
        self.reconcile()

    def test_retrace_cancels_unfilled_target_and_can_rearm(self):
        self.enter(); self.runner.step(self.batch(1070, 31000))
        self.runner.step(self.batch(1075, 29000))
        p = self.runner.state['positions'][MINT]
        self.assertEqual(p['sold_stages'], [])
        self.assertNotIn('exit_order', p)
        self.partial(1080, 31000)
        self.assertEqual(self.runner.report()['sale_count'], 1)

    def test_gap_then_retrace_only_fills_targets_still_reached(self):
        self.enter(); self.runner.step(self.batch(1070, 65000))
        self.runner.step(self.batch(1075, 35000))
        self.assertEqual(self.runner.state['positions'][MINT]['sold_stages'], [0])
        self.assertAlmostEqual(self.runner.report()['trades'][0]['initial_fraction'], .4)

    def test_same_observation_cannot_fill_a_sale(self):
        self.enter(); self.runner.step(self.batch(1070, 31000))
        self.runner.step(self.batch(1075, quotes=[curve(1070, 31000)]))
        self.assertEqual(self.runner.report()['sale_count'], 0)
        self.runner.step(self.batch(1080, 31000))
        self.assertEqual(self.runner.report()['sale_count'], 1)

    def test_stop_loss_preempts_target_and_unknown_ownership_allows_exit(self):
        self.enter(); self.runner.step(self.batch(1070, 31000))
        self.runner.step(self.batch(1075, 7000, status='unknown'))
        self.assertEqual(self.runner.state['positions'][MINT]['exit_order']['reason'], 'stop_loss')
        self.assertEqual(self.runner.report()['sale_count'], 0)
        self.runner.step(self.batch(1080, 6900, status='unknown'))
        self.assertFalse(self.runner.state['positions'])
        self.assertEqual(self.runner.report()['closed_count'], 1)
        self.reconcile()

    def test_trailing_exit_uses_price_after_partial_and_ownership_flag_exits(self):
        self.enter(); self.partial()
        self.runner.step(self.batch(1080, 19500))
        self.assertEqual(self.runner.state['positions'][MINT]['exit_order']['reason'], 'trailing_exit')
        self.runner.step(self.batch(1085, 19000, status='blocked'))
        self.assertFalse(self.runner.state['positions'])
        self.assertEqual(self.runner.report()['sale_count'], 2)
        self.reconcile()

    def test_missing_quote_never_invents_a_sale_and_gap_survives_restart(self):
        self.enter()
        self.runner.step(self.batch(1100, quotes=[]))
        p = self.runner.state['positions'][MINT]
        self.assertEqual(p['mark_value_usd'], 0)
        self.assertNotIn('exit_order', p)
        self.runner.step(self.batch(1700, quotes=[]))
        self.assertEqual(p['exit_order']['reason'], 'quote_gap')
        self.assertEqual(self.runner.report()['closed_count'], 0)
        self.reconcile()
        self.runner.close(); self.runner = self.open(self.path)
        self.runner.step(self.batch(1705, 20000))
        self.assertEqual(self.runner.report()['closed_count'], 1)
        self.reconcile()

    def test_ownership_gate_checks_policy_age_and_fill_again(self):
        for i in range(7):
            self.runner.step(self.batch(1000+i*10, 10500+i*60))
        b = self.batch(1065, 10900, status='blocked')
        self.runner.step(b)
        self.assertFalse(self.runner.state['positions'])
        for change in ({'checked_at': 0}, {'checked_at': 99999}, {'mint': 'wrong'}, {'policy_hash': 'wrong'}):
            b = self.batch(1070+len(self.runner.state['candidates']), 11000)
            b['evidence'][MINT]['ownership'].update(change)
            self.assertFalse(self.runner.ownership_clear(MINT, b['evidence'][MINT], b['at']))

    def test_flat_market_and_fx_only_moves_cannot_create_entries(self):
        for i in range(9):
            q = curve(1000+i*10, 10500)
            q['quote_usd'] = 100+i
            q['market_cap_usd'] *= q['quote_usd']/100
            self.runner.step(self.batch(q['observed_at'], quotes=[q]))
        self.assertFalse(self.runner.held_mints())

    def test_stale_window_and_peak_drawdown_block_entry(self):
        for i in range(6):
            self.runner.step(self.batch(1000+i*10, 10500+i*60))
        self.runner.step(self.batch(1100, 11000))
        self.assertFalse(self.runner.held_mints())
        for i in range(1, 8):
            b = self.batch(1100+i*10, 11000+i*60)
            b['evidence'][MINT]['peak_cap'] = 50000
            self.runner.step(b)
        self.assertFalse(self.runner.held_mints())

    def test_curve_to_amm_transition_and_wrong_pool_cannot_exit(self):
        self.enter()
        def amm(t, pool='pair'):
            return {'mint': MINT, 'symbol': 'LADDER TEST', 'observed_at': t,
                    'dex_id': 'pumpswap', 'pair_address': pool, 'valuation_basis': 'fdv',
                    'market_cap_usd': 105000, 'price_usd': .000105,
                    'base_reserve': 150_000_000, 'liquidity_usd': 31500}
        self.runner.step(self.batch(1070, quotes=[amm(1070)]))
        self.assertEqual(self.runner.state['positions'][MINT]['amm_pool'], 'pair')
        self.runner.step(self.batch(1075, quotes=[amm(1075, 'wrong')]))
        self.assertEqual(self.runner.report()['sale_count'], 0)
        self.runner.step(self.batch(1080, quotes=[amm(1080)]))
        self.assertEqual(self.runner.report()['sale_count'], 1)
        self.assertAlmostEqual(self.runner.report()['positions'][0]['remaining_fraction'], .1)
        self.reconcile()

    def test_invalid_amm_values_cannot_mark_or_fill(self):
        self.enter()
        for field, value in [('base_reserve', float('nan')), ('base_reserve', -1),
                             ('valuation_basis', 'market_cap'), ('observed_at', 99999)]:
            q = {'mint': MINT, 'symbol': 'TEST', 'observed_at': 1070, 'dex_id': 'pumpswap',
                 'pair_address': 'pair', 'valuation_basis': 'fdv', 'market_cap_usd': 100000,
                 'price_usd': .0001, 'base_reserve': 100_000_000, 'liquidity_usd': 20000}
            q[field] = value
            self.assertIsNone(self.runner.market(q, 1070))

    def test_amm_cannot_replace_a_curve_without_observed_completion(self):
        self.enter()
        b = self.batch(1070, 105000)
        b['evidence'][MINT]['curve_complete'] = False
        self.runner.step(b)
        p = self.runner.state['positions'][MINT]
        self.assertNotIn('amm_pool', p)
        self.assertEqual(p['mark_value_usd'], 0)
        self.assertEqual(self.runner.report()['sale_count'], 0)

    def test_inconsistent_amm_valuation_cannot_trigger_a_sale(self):
        self.enter()
        b = self.batch(1070, 105000)
        b['quotes'][0]['price_usd'] /= 10
        self.runner.step(b)
        p = self.runner.state['positions'][MINT]
        self.assertNotIn('amm_pool', p)
        self.assertEqual(p['mark_value_usd'], 0)
        self.assertEqual(self.runner.report()['sale_count'], 0)

    def test_atomic_rollback_deduplication_and_recorded_input_replay(self):
        self.enter(); self.runner.step(self.batch(1070, 31000))
        before = copy.deepcopy(self.runner.state)
        count = self.runner.db.execute('SELECT count(*) FROM inputs').fetchone()[0]
        original = self.runner._step
        def fail_after_mutation(batch):
            original(batch)
            raise RuntimeError('Injected disk/write transaction failure')
        with patch.object(self.runner, '_step', side_effect=fail_after_mutation):
            with self.assertRaises(RuntimeError):
                self.runner.step(self.batch(1075, 31000))
        self.assertEqual(self.runner.state, before)
        self.assertEqual(self.runner.db.execute('SELECT count(*) FROM inputs').fetchone()[0], count)
        self.assertEqual(self.runner.report()['sale_count'], 0)
        self.runner.step(self.batch(1075, 31000))
        self.runner.step(self.batch(1075, 31000))
        self.assertEqual(self.runner.report()['sale_count'], 1)
        replay = self.open(Path(self.tmp.name)/'replay.sqlite')
        for row in self.runner.db.execute('SELECT payload FROM inputs ORDER BY at'):
            replay.step(json.loads(zlib.decompress(row[0])))
        self.assertEqual(replay.state, self.runner.state)
        self.assertEqual(replay.export_csv(), self.runner.export_csv())
        self.reconcile()

    def test_failed_partial_exit_retains_quantity_and_cost(self):
        self.enter(); self.runner.step(self.batch(1070, 31000))
        original = copy.deepcopy(self.runner.state['positions'][MINT])
        self.runner.cfg['failed_attempt_probability'] = .999999
        self.runner.step(self.batch(1075, 31000))
        p = self.runner.state['positions'][MINT]
        self.assertEqual(p['quantity'], original['quantity'])
        self.assertEqual(p['remaining_cost_usd'], 10)
        self.assertEqual(p['sold_stages'], [])
        self.assertEqual(self.runner.report()['sale_count'], 0)
        self.assertAlmostEqual(self.runner.state['failed_fees_usd'], .10)
        self.runner.cfg['failed_attempt_probability'] = 0
        self.runner.step(self.batch(1080, 31000))
        self.assertEqual(self.runner.report()['sale_count'], 1)
        self.reconcile()

    def test_cash_reservations_and_max_positions_include_pending(self):
        for i in range(7):
            t = 1000+i*10
            quotes = [curve(t, 10500+i*60, chr(65+j)*44) for j in range(8)]
            self.runner.step(self.batch(t, quotes=quotes))
        self.assertEqual(len(self.runner.state['pending']), 3)
        self.runner.step(self.batch(1065, quotes=[curve(1065, 10900, chr(65+j)*44) for j in range(8)]))
        self.assertEqual(len(self.runner.state['positions']), 3)
        self.assertEqual(self.runner.state['cash_usd'], 70)
        self.reconcile()

    def test_ten_day_exit_and_contract_or_duplicate_writer_refusal(self):
        self.enter()
        with self.assertRaisesRegex(ValueError, 'writer'):
            LadderRunner(self.path, self.cfg, self.risk, source='synthetic_test')
        t = 1065+self.cfg['max_hold_seconds']
        self.runner.step(self.batch(t, 20000))
        self.assertEqual(self.runner.state['positions'][MINT]['exit_order']['reason'], 'ten_day_limit')
        self.runner.step(self.batch(t+5, 20000))
        self.assertFalse(self.runner.state['positions'])
        self.reconcile()
        self.runner.close()
        changed = copy.deepcopy(self.cfg); changed['targets'][0]['market_cap_usd'] = 35000
        with self.assertRaisesRegex(ValueError, 'rules changed'):
            LadderRunner(self.path, changed, self.risk, source='synthetic_test')

    def test_target_configuration_cannot_oversell(self):
        bad = copy.deepcopy(self.cfg); bad['targets'][0]['initial_fraction'] = .9
        with self.assertRaises(ValueError): validate_config(bad)
        bad = copy.deepcopy(self.cfg); bad['targets'][1]['market_cap_usd'] = 20000
        with self.assertRaises(ValueError): validate_config(bad)

    def test_sale_export_keeps_public_symbols_as_text(self):
        import csv
        import io
        p=self.enter();p['symbol']='=HYPERLINK("https://example.invalid")'
        self.partial()
        rows=list(csv.DictReader(io.StringIO(self.runner.export_csv())))
        self.assertTrue(rows[0]['symbol'].startswith("'="))


if __name__ == '__main__':
    unittest.main()
