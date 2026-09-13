#!/usr/bin/env python3
"""Read-only snapshot of an existing recording, then isolated paper-rule replays."""
import argparse
import json
import sqlite3
import zlib
from pathlib import Path

from paper.engine import Engine, canonical
from paper.model import load_config, validate_config

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    con = sqlite3.connect(f'file:{args.db.resolve()}?mode=ro', uri=True)
    con.execute('BEGIN')
    contract = json.loads(con.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
    batches = [json.loads(zlib.decompress(r[0])) for r in con.execute('SELECT payload FROM batches ORDER BY at')]
    con.close()
    with (args.out/'input.recording.jsonl').open('w') as f:
        f.write(canonical({'type':'header', **contract})+'\n')
        for b in batches:
            f.write(canonical(b)+'\n')
    results = []
    for name, cfg in [('previous',validate_config(contract['config'])),
                      ('cadence',load_config(ROOT/'paper.cadence.config.json'))]:
        engine = Engine(args.out/(name+'.sqlite'),cfg,contract['source'])
        try:
            for b in batches:
                engine.step(b)
            report = engine.report()
            fills = [r[0] for r in engine.db.execute("SELECT at FROM decisions WHERE action='fill_entry' ORDER BY at")]
            attempts = [r[0] for r in engine.db.execute("SELECT at FROM decisions WHERE action IN ('fill_entry','failed_entry') ORDER BY at")]
            result = {'preset':name,'buys':len(fills),'closed_trades':report['closed_trades'],
                      'open_positions':len(report['positions']), 'total_pnl_usd':report['total_pnl_usd'],
                      'max_buys_in_60s':max((sum(t-60<x<=t for x in fills) for t in fills),default=0),
                      'max_attempts_in_60s':max((sum(t-60<x<=t for x in attempts) for t in attempts),default=0),
                      'run_result':report['run_result'],'halt_reason':report['halt_reason'],
                      'minutes_observed':(batches[-1]['at']-batches[0]['at'])/60,
                      'observations':report['observations']}
            results.append(result)
            (args.out/(name+'.report.json')).write_text(json.dumps(report,indent=2))
            (args.out/(name+'.trades.csv')).write_text(engine.export_csv())
        finally:
            engine.close()
    (args.out/'comparison.json').write_text(json.dumps(results,indent=2)+'\n')
    print(json.dumps(results,indent=2))


if __name__ == '__main__':
    main()
