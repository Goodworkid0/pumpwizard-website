"""Synthetic capacity/accounting check. Never imports test quotes into live data."""
import copy
import json
from pathlib import Path
import statistics
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from paper.experiment_catalog import catalog, BANDS
from paper.research_runner import ResearchRunner
from paper.shared_research import SharedResearch


def main():
    base=json.loads((ROOT/'long_hold_experiments.json').read_text())['experiments'][0]['config']
    risk=json.loads((ROOT/'ownership.config.json').read_text())
    with tempfile.TemporaryDirectory(prefix='pump-research-benchmark-') as folder:
        shared=SharedResearch(Path(folder)/'shared.sqlite');runners=[]
        try:
            for entry in catalog(base):
                if not entry['pilot']:continue
                config=copy.deepcopy(entry['config']);config['failed_attempt_probability']=0
                runners.append(ResearchRunner(Path(folder)/(entry['id']+'.sqlite'),config,risk,shared,'synthetic_benchmark'))
            assert len(runners)==100 and sum(r.cfg['budget_usd'] for r in runners)==50000
            durations=[]
            for i in range(65):
                at=1000+i*30;quotes=[]
                for band,(low,high) in enumerate(BANDS):
                    for peer in range(4):
                        cap=low*1.25*(1+i*(.002+peer*.0003))*(.4 if i>=63 else 1)
                        mint=chr(65+band*4+peer)*44
                        quotes.append({'mint':mint,'symbol':'SYNTHETIC','observed_at':at,
                            'dex_id':'pumpswap','pair_address':mint,'valuation_basis':'fdv',
                            'market_cap_usd':cap,'price_usd':cap/1e9,'base_reserve':200000000,
                            'liquidity_usd':cap*.4,'source_timestamp_known':False})
                batch={'at':at,'quotes':quotes,'evidence':{q['mint']:{'graduated_observed':True} for q in quotes}}
                tick=time.monotonic();shared.prepare(batch,runners[0].market)
                for runner in runners:runner.step(batch)
                durations.append(time.monotonic()-tick)
            reports=[r.report() for r in runners]
            assert sum(r['total_entries'] for r in reports)>0
            assert sum(r['closed_count'] for r in reports)>0
            assert shared.db.execute('SELECT count(*) FROM inputs').fetchone()[0]==65
            for runner in runners:
                assert runner.db.execute('SELECT count(*) FROM inputs').fetchone()[0]==0
                assert runner.db.execute('SELECT count(*) FROM input_refs').fetchone()[0]==65
            result={'source':'synthetic_benchmark_not_live_performance','runners':len(runners),
                'shared_batches':65,'independent_input_references':6500,
                'total_entries':sum(r['total_entries'] for r in reports),
                'closed_trades':sum(r['closed_count'] for r in reports),
                'median_cycle_seconds':statistics.median(durations),'max_cycle_seconds':max(durations),
                'p95_cycle_seconds':sorted(durations)[int(len(durations)*.95)]}
        finally:
            for runner in runners:runner.close()
            shared.close()
    print(json.dumps(result,indent=2))


if __name__=='__main__':main()
