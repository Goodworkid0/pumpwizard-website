#!/usr/bin/env python3
"""Describe recorded paths near $10k; this is not a backtest of executable trades."""
import argparse
import csv
import json
import sqlite3
import statistics
from collections import defaultdict
from pathlib import Path
import zlib

from paper.model import usable


def analyze(directory):
    directory = Path(directory)
    points, experiments, seen = defaultdict(list), [], set()
    for name in ('live.sqlite', 'frequent.sqlite', 'cadence.sqlite'):
        db = sqlite3.connect(f'file:{directory / name}?mode=ro', uri=True)
        meta = dict(db.execute('SELECT key,value FROM meta'))
        contract, state = json.loads(meta['contract']), json.loads(meta['state'])
        if contract['source'] != 'public_index_live':
            raise ValueError('Research requires actual observed ledgers')
        trades = [json.loads(r[0]) for r in db.execute('SELECT data FROM trades')]
        equity = db.execute('SELECT value FROM equity ORDER BY id DESC LIMIT 1').fetchone()[0]
        experiments.append({'ledger': name, 'closed': len(trades), 'open': len(state['positions']),
            'net_pnl_usd': equity - contract['config']['virtual_balance_usd'],
            'first_at': state['started_at'], 'last_at': state['last_at']})
        for ident, at, payload in db.execute('SELECT id,at,payload FROM batches ORDER BY at'):
            if ident in seen:
                continue
            seen.add(ident)
            for q in json.loads(zlib.decompress(payload))['snapshots']:
                if not usable(q, at, contract['config']):
                    points[q['mint']].append((at, q['market_cap_usd'], q['symbol']))
        db.close()
    memory = sqlite3.connect(f'file:{directory / "coin-memory.sqlite"}?mode=ro', uri=True)
    coins = {r[0]: json.loads(r[1]) for r in memory.execute('SELECT mint,data FROM coins')}
    bars = defaultdict(list)
    for mint, bucket, low, high in memory.execute('SELECT mint,bucket,low,high FROM bars ORDER BY bucket'):
        bars[mint].append((bucket, low, high))
    statuses = dict(memory.execute("SELECT json_extract(data,'$.status'),count(*) FROM ownership GROUP BY 1"))
    rows = []
    for mint, series in points.items():
        series.sort()
        entry = next((p for p in series if 10000 <= p[1] <= 12500), None)
        if not entry:
            continue
        after = [p for p in series if p[0] > entry[0]]
        later_bars = [b for b in bars[mint] if b[0] > int(entry[0] // 300)]
        observed_peak = max([entry[1]] + [p[1] for p in after] + [b[2] for b in later_bars])
        stop = next((p[0] for p in after if p[1] <= entry[1] * .7), None)
        row = {'mint': mint, 'symbol': entry[2], 'entry_reference_at': entry[0],
            'entry_reference_cap': entry[1], 'observed_peak_after_reference': observed_peak,
            'later_curve_samples': len(after), 'later_curve_span_minutes': (after[-1][0] - entry[0]) / 60 if after else 0,
            'observed_30pct_cap_drop': stop is not None,
            'latest_ownership_status': 'not_checked'}
        ownership = memory.execute('SELECT data FROM ownership WHERE mint=?', (mint,)).fetchone()
        if ownership:
            row['latest_ownership_status'] = json.loads(ownership[0])['status']
        for target in (30000, 60000, 100000):
            exact = next((p[0] for p in after if p[1] >= target), None)
            # Same-bucket highs may predate the reference, so never count them.
            bar_hit = next((b[0] * 300 for b in later_bars if b[2] >= target), None)
            row[f'reached_{target}'] = exact is not None or bar_hit is not None
            row[f'curve_minutes_to_{target}'] = (exact - entry[0]) / 60 if exact else None
            row[f'curve_{target}_before_drop'] = exact is not None and (stop is None or exact < stop)
        rows.append(row)
    memory.close()
    rows.sort(key=lambda r: r['observed_peak_after_reference'], reverse=True)
    first = min(e['first_at'] for e in experiments)
    last = max(e['last_at'] for e in experiments)
    summary = {'snapshot_first_at': first, 'snapshot_last_at': last, 'recorded_hours': (last-first)/3600,
        'remembered_coins': len(coins), 'near_10k_cohort': len(rows), 'experiments': experiments,
        'latest_ownership_counts': statuses,
        'observed_30pct_cap_drops': sum(r['observed_30pct_cap_drop'] for r in rows),
        'fewer_than_5_minutes_of_later_curve_data': sum(r['later_curve_span_minutes'] < 5 for r in rows),
        'targets': {},
        'limitations': 'First usable $10k–$12.5k observation is a reference, not an entry. Threshold touches use later usable Pump snapshots and strictly later five-minute memory bars. Bar highs have uncertain within-bar timing and may include DEX valuation changes. Missing observations are not failures to reach a target. The cohort is a bounded discovery sample, not the whole market. Historical ownership was not available for all references. No strategy P&L or success probability is inferred from these touches.'}
    for target in (30000, 60000, 100000):
        minutes = [r[f'curve_minutes_to_{target}'] for r in rows if r[f'curve_minutes_to_{target}'] is not None]
        summary['targets'][str(target)] = {'observed_touches': sum(r[f'reached_{target}'] for r in rows),
            'timed_curve_touches': len(minutes), 'median_curve_minutes': statistics.median(minutes) if minutes else None,
            'curve_touch_before_observed_30pct_cap_drop': sum(r[f'curve_{target}_before_drop'] for r in rows)}
    return summary, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    args = parser.parse_args()
    summary, rows = analyze(args.directory)
    (args.directory/'analysis.json').write_text(json.dumps(summary, indent=2)+'\n')
    if rows:
        with (args.directory/'coin-paths.csv').open('w', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=list(rows[0]))
            writer.writeheader()
            for row in rows:
                writer.writerow({k: "'"+v if isinstance(v,str) and v.lstrip().startswith(('=','+','-','@')) else v
                                 for k,v in row.items()})
    print(json.dumps(summary, indent=2))
    print('Largest observed subsequent valuations:', json.dumps(rows[:8], indent=2))


if __name__ == '__main__':
    main()
