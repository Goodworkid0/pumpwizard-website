"""Staging-only registry bridge from an existing collector's public API and RO ledgers."""
import argparse
import json
import os
from pathlib import Path
import sqlite3
import sys
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from paper.replay_package import runner_type


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--origin',default='http://pump-paper:8765')
    p.add_argument('--output',type=Path,required=True); args=p.parse_args()
    def request(path):
        req=urllib.request.Request(args.origin+path,headers={'Host':'127.0.0.1:8765'})
        try: response=urllib.request.urlopen(req,timeout=15)
        except urllib.error.HTTPError as exc:
            if exc.code!=503: raise
            response=exc
        with response: return json.load(response)
    entries=[]; page=1
    while True:
        catalog=request('/api/public/catalog?page_size=100&page='+str(page))
        for row in catalog['strategies']:
            path=ROOT/'data'/('long-hold-'+row['id']+'.sqlite')
            con=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)
            try: contract=json.loads(con.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
            finally: con.close()
            entries.append({k:row.get(k) for k in ('id','label','trial','description','family')} | {
                'ledger':path.name,'runner':runner_type(contract).__name__,'contract':contract})
        if page*100>=catalog['total']: break
        page+=1
    # Original registry order is not required for reporting, but preserve the
    # primary non-trial ladder first (rather than a leaderboard-selected trial).
    entries.sort(key=lambda x:(x['runner']!='LadderRunner',x['id']))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    while True:
        health=request('/api/public/health'); now=time.time()
        def observed(key):
            age=health.get(key)
            return now-age if isinstance(age,(int,float)) else 0
        doc={'schema':'pumpwizard.public-registry/v1','at':now,'entries':entries,
             'workers_alive':health.get('checks',{}).get('workers') is True,
             'timing':{'completed_at':observed('research_cycle_age_seconds'),
                       'input_observed_at':observed('market_input_age_seconds')},
             'status':{'market':'Staging read-only view of production collector'},'shared':True,
             'catalog':[{'pilot':False} for _ in range(1000)]}
        temporary=args.output.with_suffix('.tmp'); temporary.write_text(json.dumps(doc)); temporary.replace(args.output)
        time.sleep(5)


if __name__=='__main__': main()
