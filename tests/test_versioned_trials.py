"""Isolated synthetic tests; never open or change the live ledgers."""
import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from paper.brain_service import experiment_entries
from paper.engine import canonical
from paper.experiment_catalog import catalog
from paper.ladder import LadderRunner
from paper.research_runner import ResearchRunner
from paper.shared_research import SharedResearch
from paper.versioned_trials import trial_entries, TrialLadderRunner, TrialResearchRunner
from test_ladder import curve
from test_research_matrix import amm

ROOT = Path(__file__).resolve().parents[1]
MINT = 'A'*44


class VersionedTrialTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = json.loads((ROOT/'long_hold_experiments.json').read_text())['experiments'][0]['config']
        self.risk = json.loads((ROOT/'ownership.config.json').read_text())
        self.manifest = json.loads((ROOT/'trial_experiments.json').read_text())
        self.shared = SharedResearch(self.root/'shared.sqlite'); self.addCleanup(self.shared.close)
        self.config = copy.deepcopy(next(e['config'] for e in catalog(self.base)
            if e['id'] == 'r1-persistent-b3-300s-7d'))
        self.config['failed_attempt_probability'] = 0

    def definition(self, arm):
        return {'trial_id': 'synthetic', 'group': 'persistent', 'arm': arm,
            'label': arm, 'parent_id': 'original', 'control_id': 'synthetic-control',
            'risk_enabled': False, 'ownership_required': False,
            'ownership_telemetry': arm in ('risk', 'combined'), 'exits_enabled': arm in ('exits', 'combined'),
            'profit_activation_return': .20, 'profit_trailing_drawdown': .10}

    def runner(self, arm, name=None, config=None, legacy=False):
        cfg = copy.deepcopy(config or self.config)
        definition = self.definition(arm)
        args = [self.root/((name or arm)+'.sqlite'), cfg, self.risk]
        if not legacy: args.append(self.shared)
        cls = TrialLadderRunner if legacy else TrialResearchRunner
        runner = cls(*args, definition=definition, source='synthetic_test')
        self.addCleanup(lambda: runner.close() if not runner.file_lock.closed else None)
        return runner

    def batch(self, t, cap=161000, status='clear', quotes=None):
        gate = {'mint': MINT, 'status': status, 'checked_at': t}
        import hashlib
        gate['policy_hash'] = hashlib.sha256(canonical(self.risk).encode()).hexdigest()
        return {'at': t, 'quotes': [amm(t, cap)] if quotes is None else quotes,
                'evidence': {MINT: {'ownership': gate, 'graduated_observed': True}}}

    def step(self, runners, t, cap=161000, status='clear', batch=None):
        batch = batch or self.batch(t, cap, status)
        self.shared.prepare(batch, runners[0].market)
        for runner in runners: runner.step(batch)
        return batch

    def enter(self, runners):
        for i in range(11): self.step(runners, 1000+i*30, 150000+i*1000)
        for runner in runners: self.assertIn(MINT, runner.state['pending'])
        self.step(runners, 1330)
        for runner in runners: self.assertIn(MINT, runner.state['positions'])

    def reconcile(self, runner):
        report = runner.report()
        self.assertAlmostEqual(report['pnl_usd'], report['realized_net_pnl_usd']+report['unrealized_pnl_usd'])
        self.assertGreaterEqual(report['cash_usd'], 0)

    def test_manifest_selects_14_families_and_56_isolated_matched_arms(self):
        original = experiment_entries(json.loads((ROOT/'long_hold_experiments.json').read_text()))
        parents = [dict(e, runner=SimpleNamespace(cfg=dict(e['config'], entry_style=e['config'].get('entry_style', 'momentum'))))
                   for e in original]
        parents += [dict(e, runner=SimpleNamespace(cfg=e['config'])) for e in catalog(self.base) if e['pilot']]
        untouched = copy.deepcopy([p['runner'].cfg for p in parents])
        entries = trial_entries(self.manifest, parents)
        self.assertEqual(len(entries), 56)
        self.assertEqual(len({e['id'] for e in entries}), 56)
        self.assertEqual(len({e['family'] for e in entries}), 14)
        self.assertEqual(sum(e['config']['budget_usd'] for e in entries), 28000)
        for entry in entries:
            cfg = copy.deepcopy(entry['config'])
            parent = next(p for p in parents if p['id'] == entry['trial']['parent_id'])
            self.assertEqual(cfg, parent['runner'].cfg)
            self.assertLessEqual(len(entry['id']), 40)
        self.assertEqual(untouched, [p['runner'].cfg for p in parents])
        self.manifest['parents'].append(self.manifest['parents'][0])
        with self.assertRaises(ValueError): trial_entries(self.manifest, parents)

    def test_research_control_matches_original_execution(self):
        original = ResearchRunner(self.root/'original.sqlite', self.config, self.risk, self.shared, 'synthetic_test')
        self.addCleanup(original.close)
        control = self.runner('control')
        self.enter([original, control])
        for t, cap in ((1360, 230000), (1390, 200000), (1420, 199000), (1450, 80000), (1480, 78000)):
            self.step([original, control], t, cap)
            for key in ('positions', 'cash_usd', 'pending', 'attempts', 'realized_pnl_usd', 'fees_usd'):
                self.assertEqual(original.state[key], control.state[key], key)
        self.assertEqual(original.report()['closed_count'], control.report()['closed_count'])

    def test_legacy_control_matches_original_execution(self):
        cfg = copy.deepcopy(self.base); cfg['failed_attempt_probability'] = 0
        original = LadderRunner(self.root/'legacy.sqlite', cfg, self.risk, 'synthetic_test')
        self.addCleanup(original.close)
        control = self.runner('control', config=cfg, legacy=True)
        for i in range(15):
            t = 1000+i*10; cap = 10500+i*60 if i < 10 else 16000
            batch = self.batch(t, quotes=[curve(t, cap)])
            for runner in (original, control): runner.step(batch)
            for key in ('positions', 'cash_usd', 'pending', 'attempts', 'realized_pnl_usd', 'fees_usd'):
                self.assertEqual(original.state[key], control.state[key], key)
        self.assertGreater(original.report()['total_entries'], 0)

    def test_ownership_telemetry_never_blocks_an_unknown_entry(self):
        runner = self.runner('risk')
        for i in range(11): self.step([runner], 1000+i*30, 150000+i*1000)
        self.assertIn(MINT, runner.state['pending'])
        self.step([runner], 1330, status='unknown')
        self.assertIn(MINT, runner.state['positions'])
        self.assertLess(runner.state['cash_usd'], 500)
        self.assertGreater(runner.report()['trial_metrics']['ownership_observations'], 0)
        for field, value in (('mint', 'wrong'), ('policy_hash', 'wrong'), ('checked_at', 1), ('checked_at', 9999)):
            batch = self.batch(1360)
            batch['evidence'][MINT]['ownership'][field] = value
            self.assertIsNone(runner.entry_reason(MINT, runner.market(batch['quotes'][0], 1360), batch['evidence'][MINT], 1360))

    def test_blocked_ownership_evidence_never_forces_an_exit(self):
        runner = self.runner('risk'); self.enter([runner])
        for t, changed in ((1360, {'checked_at': 1}), (1390, {'mint': 'wrong'}),
                           (1420, {'policy_hash': 'wrong'}), (1450, {'checked_at': 9999})):
            batch = self.batch(t, status='blocked'); batch['evidence'][MINT]['ownership'].update(changed)
            self.step([runner], t, batch=batch)
            self.assertIn(MINT, runner.state['positions'])
        self.step([runner], 1480, status='blocked')
        self.assertNotIn('exit_order', runner.state['positions'][MINT])

    def test_profit_trail_before_absolute_target_with_delayed_actual_fill(self):
        control = self.runner('control'); improved = self.runner('exits')
        self.enter([control, improved])
        self.step([control, improved], 1360, 230000)
        self.assertTrue(improved.state['positions'][MINT]['profit_protection_armed'])
        self.assertNotIn('exit_order', improved.state['positions'][MINT])
        self.step([control, improved], 1390, 200000)
        self.assertEqual(improved.state['positions'][MINT]['exit_order']['reason'], 'net_profit_trail')
        self.assertEqual(improved.report()['closed_count'], 0)
        self.step([control, improved], 1391, 199000)
        self.assertEqual(improved.report()['closed_count'], 0)
        self.step([control, improved], 1420, 198000)
        self.assertIn(MINT, control.state['positions'])
        self.assertFalse(improved.state['positions'])
        sale = improved.report()['trades'][0]
        self.assertEqual(sale['exit_cap'], 198000)
        self.assertEqual(sale['exit_reason'], 'net_profit_trail')
        self.assertGreater(sale['exit_fees_usd'], 0)
        self.reconcile(improved)

    def test_no_fictional_exit_on_missing_stale_or_wrong_pool_quotes(self):
        runner = self.runner('combined'); self.enter([runner])
        self.step([runner], 1360, 230000); self.step([runner], 1390, 200000)
        for t, quotes in ((1420, []), (1450, [amm(1300, 210000)]), (1480, [amm(1480, 210000, pool='wrong')])):
            self.step([runner], t, batch=self.batch(t, quotes=quotes))
            self.assertEqual(runner.report()['closed_count'], 0)
            self.assertEqual(runner.state['positions'][MINT]['mark_value_usd'], 0)
            self.reconcile(runner)
        self.step([runner], 1510, 190000)
        self.assertEqual(runner.report()['closed_count'], 1)

    def test_failed_profit_exit_retains_position_and_accounts_for_fee(self):
        runner = self.runner('exits'); self.enter([runner])
        self.step([runner], 1360, 230000); self.step([runner], 1390, 200000)
        # Force the existing deterministic failure path without changing live costs.
        from paper import ladder
        real_hash = ladder.hashlib.sha256
        def fail_attempt(payload):
            return SimpleNamespace(hexdigest=lambda: '0'*64) if payload.startswith(b'ladder:') else real_hash(payload)
        runner.cfg['failed_attempt_probability'] = .05
        with patch.object(ladder.hashlib, 'sha256', side_effect=fail_attempt):
            self.step([runner], 1420, 198000)
        self.assertIn(MINT, runner.state['positions'])
        self.assertAlmostEqual(runner.state['failed_fees_usd'], runner.cfg['fixed_fee_usd'])
        self.assertEqual(runner.report()['closed_count'], 0)
        runner.cfg['failed_attempt_probability'] = 0
        self.step([runner], 1450, 197000)
        self.assertEqual(runner.report()['closed_count'], 1); self.reconcile(runner)

    def test_partial_target_and_profit_trail_do_not_double_sell(self):
        runner = self.runner('exits'); self.enter([runner])
        self.step([runner], 1360, 510000); self.step([runner], 1390, 515000)
        p = runner.state['positions'][MINT]
        self.assertAlmostEqual(p['quantity']/p['initial_quantity'], .6)
        self.assertEqual(p['sold_stages'], [0])
        self.step([runner], 1420, 350000); self.step([runner], 1450, 340000)
        self.assertFalse(runner.state['positions'])
        self.assertAlmostEqual(sum(s['initial_fraction'] for s in runner.report()['trades']), 1)
        self.assertEqual(runner.report()['closed_count'], 1); self.reconcile(runner)

    def test_stop_loss_priority_and_pending_target_replaced_by_profit_trail(self):
        runner = self.runner('exits'); self.enter([runner])
        self.step([runner], 1360, 510000)
        self.assertTrue(runner.state['positions'][MINT]['exit_order']['stages'])
        self.step([runner], 1390, 350000)
        self.assertEqual(runner.state['positions'][MINT]['exit_order']['reason'], 'net_profit_trail')
        self.assertEqual(runner.report()['sale_count'], 0)
        # A price gap can still lose money; the simulator must not promise the trail price.
        self.step([runner], 1420, 60000)
        self.assertLess(runner.report()['trades'][0]['pnl_usd'], 0)
        self.reconcile(runner)

    def test_stop_loss_takes_priority_over_new_profit_trail(self):
        runner = self.runner('exits'); self.enter([runner])
        self.step([runner], 1360, 230000); self.step([runner], 1390, 60000)
        self.assertEqual(runner.state['positions'][MINT]['exit_order']['reason'], 'stop_loss')

    def test_rollback_duplicate_restart_and_immutable_definition(self):
        runner = self.runner('combined'); self.enter([runner])
        before = copy.deepcopy(runner.state); original = runner._step
        def fail(batch): original(batch); raise RuntimeError('injected')
        with patch.object(runner, '_step', side_effect=fail):
            with self.assertRaises(RuntimeError): self.step([runner], 1360, 230000)
        self.assertEqual(before, runner.state)
        self.assertFalse(any(e['action'] == 'profit_protection_armed' for e in runner.report()['events']))
        batch = self.batch(1360, 230000); runner.step(batch)
        state = copy.deepcopy(runner.state); runner.step(batch); self.assertEqual(state, runner.state)
        runner.close(); reopened = self.runner('combined')
        self.assertEqual(state, reopened.state)
        reopened.close()
        changed = self.definition('combined'); changed['profit_activation_return'] = .25
        cfg = dict(self.config)
        with self.assertRaisesRegex(ValueError, 'new trial ID'):
            TrialResearchRunner(self.root/'combined.sqlite', cfg, self.risk, self.shared,
                                definition=changed, source='synthetic_test')
        # A code-only change also refuses to reinterpret the old experiment.
        with patch.object(TrialResearchRunner, 'extra_code_files', (*TrialResearchRunner.extra_code_files, 'brain_service.py')):
            with self.assertRaisesRegex(ValueError, 'new trial ID'): self.runner('combined')
        self.assertEqual(self.runner('combined').state, state)

    def test_exact_recorded_features_replay_profit_trail_and_closed_outcome(self):
        runner = self.runner('combined'); self.enter([runner])
        self.step([runner], 1360, 230000); self.step([runner], 1390, 200000)
        self.step([runner], 1420, 198000)
        replay_shared = SharedResearch(self.root/'replay-shared.sqlite'); self.addCleanup(replay_shared.close)
        replay = TrialResearchRunner(self.root/'replay.sqlite', dict(self.config),
            self.risk, replay_shared, definition=self.definition('combined'), source='synthetic_test')
        self.addCleanup(replay.close)
        for row in runner.db.execute('SELECT id FROM input_refs ORDER BY at'):
            recorded = self.shared.read(row['id'])
            replay_shared.features = {(f['mint'], f['window']): {k: v for k, v in f.items() if k not in ('mint', 'window')}
                                      for f in recorded.pop('features')}
            replay_shared.last_at = recorded['at']; replay_shared.last_input_id = row['id']
            replay.step(recorded)
        self.assertEqual(runner.state, replay.state)
        self.assertEqual(runner.report(), replay.report())
