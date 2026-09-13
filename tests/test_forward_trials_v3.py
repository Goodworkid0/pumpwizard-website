"""Synthetic checks for the additive V3 matched forward cohort."""
import copy
import json
from pathlib import Path
import tempfile
import unittest

from paper.brain_service import experiment_entries
from paper.experiment_catalog import catalog
from paper.research_runner import ResearchRunner
from paper.shared_research import SharedResearch
from paper.forward_trials_v3 import V3ResearchRunner, trial_entries
from test_research_matrix import amm


ROOT = Path(__file__).resolve().parents[1]
MINT = 'A' * 44


class ForwardTrialsV3Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        base = json.loads((ROOT / 'long_hold_experiments.json').read_text())['experiments'][0]['config']
        self.config = copy.deepcopy(next(entry['config'] for entry in catalog(base)
            if entry['id'] == 'r1-persistent-b3-300s-7d'))
        self.config['failed_attempt_probability'] = 0
        self.risk = json.loads((ROOT / 'ownership.config.json').read_text())
        self.shared = SharedResearch(self.root / 'shared.sqlite'); self.addCleanup(self.shared.close)

    def definition(self, arm):
        return {
            'trial_id': 'synthetic-v3', 'generation': 'V3', 'comparison_title': 'V3 entry & exit comparison',
            'group': 'persistent', 'arm': arm, 'label': 'V3 ' + arm, 'parent_id': 'original',
            'control_id': 'synthetic-v3-control',
            'confirmation_enabled': arm in ('confirmation', 'combined'),
            'protection_enabled': arm in ('protection', 'combined'),
            'confirmation_delay_seconds': 60, 'break_even_activation_return': .10,
        }

    def runner(self, arm):
        runner = V3ResearchRunner(self.root / (arm + '.sqlite'), self.config, self.risk, self.shared,
                                  definition=self.definition(arm), source='synthetic_test')
        self.addCleanup(runner.close)
        return runner

    def batch(self, at, cap=161000):
        return {'at': at, 'quotes': [amm(at, cap)], 'evidence': {MINT: {'graduated_observed': True}}}

    def step(self, runners, at, cap=161000):
        batch = self.batch(at, cap)
        self.shared.prepare(batch, runners[0].market)
        for runner in runners:
            runner.step(batch)

    def warm_to_first_signal(self, runners):
        for index in range(11):
            self.step(runners, 1000 + index * 30, 150000 + index * 1000)

    def enter(self, runners):
        self.warm_to_first_signal(runners)
        self.step(runners, 1330)
        for runner in runners:
            self.assertIn(MINT, runner.state['positions'])

    def test_manifest_creates_16_fresh_v3_instances(self):
        original = experiment_entries(json.loads((ROOT / 'long_hold_experiments.json').read_text()))
        parents = [dict(entry, runner=type('Runner', (), {'cfg': entry['config']})()) for entry in original]
        parents += [dict(entry, runner=type('Runner', (), {'cfg': entry['config']})())
                    for entry in catalog(parents[0]['runner'].cfg) if entry['pilot']]
        entries = trial_entries(json.loads((ROOT / 'v3_trial_experiments.json').read_text()), parents)
        self.assertEqual(len(entries), 16)
        self.assertEqual(len({entry['id'] for entry in entries}), 16)
        self.assertEqual(sum(entry['config']['budget_usd'] for entry in entries), 8000)
        self.assertTrue(all(entry['trial']['generation'] == 'V3' for entry in entries))

    def test_control_matches_the_unchanged_parent(self):
        original = ResearchRunner(self.root / 'original.sqlite', self.config, self.risk, self.shared,
                                  source='synthetic_test')
        self.addCleanup(original.close)
        control = self.runner('control')
        self.enter([original, control])
        for at, cap in ((1360, 230000), (1390, 150000), (1420, 145000)):
            self.step([original, control], at, cap)
            for field in ('positions', 'cash_usd', 'pending', 'attempts', 'realized_pnl_usd', 'fees_usd'):
                self.assertEqual(original.state[field], control.state[field], field)

    def test_confirmation_requires_a_second_fresh_valid_signal(self):
        runner = self.runner('confirmation')
        self.warm_to_first_signal([runner])
        self.assertNotIn(MINT, runner.state['pending'])
        self.assertEqual(runner.state['candidates'][MINT]['reason'], 'Awaiting V3 confirmation')
        self.step([runner], 1330)
        self.assertNotIn(MINT, runner.state['pending'])
        self.step([runner], 1360)
        self.assertIn(MINT, runner.state['pending'])
        self.step([runner], 1390)
        self.assertIn(MINT, runner.state['positions'])
        self.assertEqual(runner.report()['trial_metrics']['confirmations_passed'], 1)

    def test_break_even_protection_arms_then_requests_an_exit(self):
        runner = self.runner('protection')
        self.enter([runner])
        self.step([runner], 1360, 230000)
        position = runner.state['positions'][MINT]
        self.assertTrue(position['break_even_protection_armed'])
        self.step([runner], 1390, 145000)
        self.assertEqual(runner.state['positions'][MINT]['exit_order']['reason'], 'break_even_protection')
        self.step([runner], 1420, 140000)
        self.assertFalse(runner.state['positions'])
        self.assertEqual(runner.report()['trades'][0]['exit_reason'], 'break_even_protection')

    def test_combined_arm_records_both_policy_changes(self):
        runner = self.runner('combined')
        self.warm_to_first_signal([runner]); self.step([runner], 1330); self.step([runner], 1360); self.step([runner], 1390)
        self.assertIn(MINT, runner.state['positions'])
        self.step([runner], 1420, 230000)
        self.assertTrue(runner.state['positions'][MINT]['break_even_protection_armed'])
        events = runner.report()['events']
        self.assertTrue(any(event['action'] == 'confirmation_passed' for event in events))
        self.assertTrue(any(event['action'] == 'break_even_protection_armed' for event in events))


if __name__ == '__main__':
    unittest.main()
