"""Exercise the additive rollout on synthetic inputs in disposable databases."""
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper.brain_service import experiment_entries
from paper.experiment_catalog import catalog, BANDS
from paper.ladder import LadderRunner
from paper.research_runner import ResearchRunner
from paper.shared_research import SharedResearch
from paper.versioned_trials import open_trials


def main():
    risk = json.loads((ROOT/'ownership.config.json').read_text())
    originals = experiment_entries(json.loads((ROOT/'long_hold_experiments.json').read_text()))
    manifest = json.loads((ROOT/'trial_experiments.json').read_text())
    with tempfile.TemporaryDirectory(prefix='pump-trials-preflight-') as folder:
        root = Path(folder); shared = SharedResearch(root/'shared.sqlite'); parents = []; trials = []
        try:
            for entry in originals:
                runner = LadderRunner(root/'data'/(entry['id']+'.sqlite'), entry['config'], risk, 'synthetic_test')
                parents.append(dict(entry, runner=runner))
            for entry in catalog(parents[0]['runner'].cfg):
                if entry['pilot']:
                    runner = ResearchRunner(root/'data'/(entry['id']+'.sqlite'), entry['config'], risk, shared, 'synthetic_test')
                    parents.append(dict(entry, runner=runner))
            trials = open_trials(root, manifest, parents, risk, shared)
            assert len(parents) == 139 and len(trials) == 56
            assert sum(e['runner'].report()['equity_usd'] for e in trials) == 28000
            runners = [e['runner'] for e in parents+trials]; durations = []
            for i in range(65):
                at = 1000+i*30; quotes = []
                for band, (low, high) in enumerate(BANDS):
                    for peer in range(4):
                        cap = low*1.25*(1+i*(.004+peer*.0003))*(.55 if i >= 63 else 1)
                        mint = chr(65+band*4+peer)*44
                        quotes.append({'mint': mint, 'symbol': 'SYNTHETIC', 'observed_at': at,
                            'dex_id': 'pumpswap', 'pair_address': mint, 'valuation_basis': 'fdv',
                            'market_cap_usd': cap, 'price_usd': cap/1e9, 'base_reserve': 200000000,
                            'liquidity_usd': cap*.4, 'source_timestamp_known': False})
                batch = {'at': at, 'quotes': quotes, 'evidence': {q['mint']: {
                    'graduated_observed': True, 'ownership': {'mint': q['mint'], 'status': 'clear',
                        'checked_at': at, 'policy_hash': runners[0].policy_hash}} for q in quotes}}
                tick = time.monotonic(); shared.prepare(batch, next(r for r in runners if isinstance(r, ResearchRunner)).market)
                for runner in runners: runner.step(batch)
                durations.append(time.monotonic()-tick)
            assert all(e['runner'].state['started_at'] == 1000 for e in trials)
            assert all(e['runner'].state['last_at'] == 2920 for e in trials)
            trial_reports = [e['runner'].report() for e in trials]
            assert sum(r['total_entries'] for r in trial_reports) > 0
            assert sum(r['closed_count'] for r in trial_reports) > 0
            for runner in runners:
                report = runner.report()
                assert abs(report['pnl_usd']-report['realized_net_pnl_usd']-report['unrealized_pnl_usd']) < 1e-6
            print(json.dumps({'source': 'synthetic_capacity_check_not_live_performance',
                'originals': len(parents), 'trial_instances': len(trials), 'batches': len(durations),
                'trial_entries': sum(r['total_entries'] for r in trial_reports),
                'trial_closed': sum(r['closed_count'] for r in trial_reports),
                'median_seconds': statistics.median(durations), 'max_seconds': max(durations)}, indent=2))
        finally:
            for entry in parents+trials: entry['runner'].close()
            shared.close()


if __name__ == '__main__':
    main()
