"""Read-only accounting and shared-input reconciliation of a verified backup.

Does not replay strategies, start a collector or contact providers. A historical
implementation change is not silently treated as an exact historical replay.
"""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
import sys
import time

ROOT=Path(__file__).resolve().parents[1]; sys.path.insert(0,str(ROOT))
from scripts.backup_lab import verify


def reconcile(directory):
    directory=Path(directory).resolve(); manifest=verify(directory)
    shared=directory/'data/shared-research.sqlite'; rows=[]
    for path in sorted((directory/'data').glob('long-hold-*.sqlite')):
        if path.is_symlink(): raise ValueError('Symlink ledger refused')
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro&immutable=1',uri=True)) as con:
            deadline=time.monotonic()+30
            con.set_progress_handler(lambda:int(time.monotonic()>deadline),10000)
            meta=dict(con.execute('SELECT key,value FROM meta'))
            state=json.loads(meta['state']); contract=json.loads(meta['contract'])
            budget=contract['config']['budget_usd']; positions=list(state['positions'].values())
            equity=state['cash_usd']+sum(p['mark_value_usd'] for p in positions)
            realized=state['realized_pnl_usd']-state['failed_fees_usd']
            unrealized=sum(p['mark_value_usd']-p['remaining_cost_usd'] for p in positions)
            if not all(math.isfinite(n) for n in (budget,equity,realized,unrealized)):
                raise ValueError('Nonfinite accounting in '+path.name)
            if abs(equity-budget-realized-unrealized)>1e-5:
                raise ValueError('Accounting reconciliation differs in '+path.name)
            latest=con.execute('SELECT at,value,cash FROM equity ORDER BY id DESC LIMIT 1').fetchone()
            if latest and (latest[0]!=state['last_at'] or abs(latest[1]-equity)>1e-5 or abs(latest[2]-state['cash_usd'])>1e-5):
                raise ValueError('Latest equity/state differs in '+path.name)
            tables={r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if 'input_refs' in tables:
                if not shared.is_file() or shared.is_symlink(): raise ValueError('Shared archive missing')
                con.execute('ATTACH DATABASE ? AS archive',(shared.as_uri()+'?mode=ro&immutable=1',))
                missing=con.execute('SELECT count(*) FROM input_refs r LEFT JOIN archive.inputs i ON i.id=r.id WHERE i.id IS NULL').fetchone()[0]
                if missing: raise ValueError('Missing shared references in '+path.name)
            rows.append({'instance':path.stem.removeprefix('long-hold-'),'equity_usd':equity,
                         'open_positions':len(positions),'closed':con.execute('SELECT count(*) FROM trades').fetchone()[0]})
    if not rows: raise ValueError('No instance ledgers to reconcile')
    return {'status':'verified','verified_at':time.time(),'snapshot_completed_at':manifest['completed_at'],
            'instances':len(rows),'closed':sum(r['closed'] for r in rows),
            'open_positions':sum(r['open_positions'] for r in rows),
            'equity_usd':sum(r['equity_usd'] for r in rows),
            'scope':'Backup integrity, accounting, latest equity and shared-input coverage; not an exact historical replay.'}


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True); parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    if args.output and args.output.exists(): parser.error('Output must be new')
    result=reconcile(args.source)
    if args.output:
        with args.output.open('x') as stream: json.dump(result,stream,indent=2)
    print(json.dumps(result,indent=2))
