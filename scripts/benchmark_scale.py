"""Offline capacity probe using real runners and read models on disposable synthetic ledgers."""
import argparse
import copy
from concurrent.futures import ThreadPoolExecutor
import json
import math
import os
from pathlib import Path
import platform
import resource
import sqlite3
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paper.brain_service import BrainService
from paper.experiment_catalog import catalog
from paper.memory import Memory
from paper.public_read import PublicReadService
from paper.research_runner import ResearchRunner
from paper.shared_research import SharedResearch


def stats(values):
    rows = sorted(values)
    return {**{name: round(rows[min(len(rows)-1, math.ceil(len(rows)*q)-1)]*1000, 3)
               for name,q in (('p50_ms',.5),('p95_ms',.95),('p99_ms',.99))}, 'samples':len(rows)} if rows else None


def size(root):
    return sum(p.stat().st_size for p in root.rglob('*') if p.is_file())


def run(count, steps, coins, max_seconds=300):
    soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft != resource.RLIM_INFINITY and soft < count*4+64:
        raise ValueError('Insufficient file-descriptor allowance for this scenario')
    deadline = time.monotonic()+max_seconds
    def budget():
        if time.monotonic()>deadline: raise ValueError('Benchmark time budget exceeded; reduce scenario size')
    with tempfile.TemporaryDirectory(prefix='pumpwizard-capacity-') as temp:
        root=Path(temp); (root/'data').mkdir()
        base=json.loads((ROOT/'long_hold_experiments.json').read_text())['experiments'][0]['config']
        configs=[e for e in catalog(base) if e['pilot'] and e['config']['entry_cap_min']==100000
                 and e['config']['observation_seconds']==300]
        risk=json.loads((ROOT/'ownership.config.json').read_text())
        shared=SharedResearch(root/'data/shared.sqlite')
        memory=Memory(root/'data/coin-memory.sqlite',json.loads((ROOT/'brain.config.json').read_text()))
        items=[]; service=None
        try:
            start=time.perf_counter()
            for index in range(count):
                budget(); entry=configs[index%len(configs)]; config=copy.deepcopy(entry['config'])
                runner=ResearchRunner(root/'data'/f'long-hold-bench-{index}.sqlite',config,risk,shared,'synthetic_benchmark')
                items.append({'id':f'bench-{index}','label':f'Benchmark {index}','family':entry['family'],
                              'description':entry['description'],'runner':runner})
            initialize=time.perf_counter()-start
            brain=BrainService.__new__(BrainService)
            brain.root=root; brain.runner=items[0]['runner']; brain.primary_strategy=items[0]
            brain.experiment_runners=items[1:]; brain.memory=memory; brain.sampler=None
            brain.ladder_enabled=True; brain.shared=shared; brain.strategy_lock=threading.RLock()
            brain.research_catalog=[]; brain.status={}; brain.research_timing={}
            brain.community_path=root/'community.config.json'
            service=PublicReadService(brain,autostart=False)
            cycles=[]; features=[]; publications=[]; reads=[]; failures=0
            before=size(root); epoch=time.time()-steps*30
            def read(index):
                start=time.perf_counter()
                if index%2: service.history(f'bench-{index%count}',{'limit':['25']})
                else: service.response('/api/public/catalog',{'page_size':['24']})
                return time.perf_counter()-start
            with ThreadPoolExecutor(max_workers=4) as pool:
                for step in range(steps):
                    budget(); at=epoch+step*30; cap=150000+(step%14)*2000 if step%14<12 else 80000
                    quotes=[{'mint':f'BENCH-{n}', 'symbol':'SYNTH', 'observed_at':at,
                             'dex_id':'pumpswap','pair_address':f'pool-{n}','valuation_basis':'fdv',
                             'market_cap_usd':cap,'price_usd':cap/1e9,'base_reserve':200000000,
                             'liquidity_usd':cap*.4,'source_timestamp_known':False} for n in range(coins)]
                    futures=[pool.submit(read,index) for index in range(8)] if service.generation else []
                    start=time.perf_counter(); shared.prepare({'at':at,'quotes':quotes,'evidence':{}},items[0]['runner'].market)
                    features.append(time.perf_counter()-start)
                    batch={'at':at,'quotes':quotes,'evidence':{}}
                    for item in items: item['runner'].step(batch)
                    cycles.append(time.perf_counter()-start)
                    for future in futures:
                        try: reads.append(future.result())
                        except Exception: failures+=1
                    if step%5==0 or step==steps-1:
                        start=time.perf_counter()
                        if not service.refresh_once(): raise ValueError('Public snapshot failed in benchmark')
                        publications.append(time.perf_counter()-start)
            after=size(root)
            closed=sum(i['runner'].report()['closed_count'] for i in items)
            # Measure a representative SQLite backup/restore; this is not fleet recovery.
            start=time.perf_counter(); sample=root/'sample-backup.sqlite'
            with sqlite3.connect(sample) as target: items[0]['runner'].db.backup(target)
            target.close()
            with sqlite3.connect(sample) as restored:
                assert restored.execute('PRAGMA quick_check').fetchone()[0]=='ok'
                assert json.loads(restored.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0])==items[0]['runner'].state
            restored.close(); recovery=time.perf_counter()-start
            service.close()
            saved=[copy.deepcopy(i['runner'].state) for i in items]
            for item in items: item['runner'].close()
            start=time.perf_counter()
            for item,state in zip(items,saved):
                previous=item['runner']
                item['runner']=ResearchRunner(previous.path,previous.cfg,risk,shared,'synthetic_benchmark')
                assert item['runner'].state==state
            restart=time.perf_counter()-start
            return {'instances':count,'steps':steps,'coins_per_cycle':coins,'closed_positions':closed,
                'initialize_seconds':round(initialize,3),'cycle':stats(cycles),'shared_features':stats(features),
                'publication':stats(publications),'concurrent_reads':stats(reads),'read_errors':failures,
                'restart_seconds':round(restart,3),'representative_backup_restore_seconds':round(recovery,3),
                'working_set_bytes_before':before,'working_set_bytes_after':after,
                'working_set_growth_bytes':after-before,
                'process_peak_rss_kib':resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                'gates':{'cycle_p95_under_30s':stats(cycles)['p95_ms']<30000,
                         'publication_max_under_30s':max(publications)<30,'no_read_errors':failures==0},
                'limitations':['Synthetic short histories and synthetic indexed quotes; not production capacity certification.',
                    'Read timings exclude HTTP/TLS/proxy; no provider, memory ingestion or Telegram transfer load.',
                    'Copies of ten signal families are not independent strategies or independent trade evidence.',
                    'Working-set growth includes SQLite/WAL allocation, not a daily storage forecast.',
                    'RSS is the process high-water mark, including preceding scenarios.',
                    'Recovery timing covers one ledger, not a complete off-site/application restore.']}
        finally:
            if service: service.close()
            for item in items: item['runner'].close()
            memory.close(); shared.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instances',default='10,100')
    parser.add_argument('--steps',type=int,default=15)
    parser.add_argument('--coins',type=int,default=4)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(); counts=[int(n) for n in args.instances.split(',')]
    if not counts or len(counts)>5 or any(n<1 or n>1000 for n in counts) or not 2<=args.steps<=100 or not 1<=args.coins<=50:
        parser.error('Bounds: 1–1000 instances, up to 5 scenarios, 2–100 steps and 1–50 coins')
    if args.output.exists(): parser.error('Output must be a new file')
    result={'schema':'pumpwizard.capacity-benchmark/v1','python':platform.python_version(),
            'sqlite':sqlite3.sqlite_version,'host':platform.system(),'cpu_count':os.cpu_count(),'scenarios':[]}
    failed=False
    with args.output.open('x') as stream:
        for count in counts:
            print(f'Benchmarking {count} instances…',flush=True)
            try: result['scenarios'].append(run(count,args.steps,args.coins))
            except Exception as exc:
                failed=True
                result['scenarios'].append({'instances':count,'status':'failed','error':str(exc)})
            stream.seek(0); json.dump(result,stream,indent=2); stream.truncate(); stream.flush()
    print(json.dumps(result,indent=2))
    if failed: raise SystemExit(1)


if __name__=='__main__': main()
