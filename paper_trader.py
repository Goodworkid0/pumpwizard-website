#!/usr/bin/env python3
"""Off-chain paper trading CLI. Never constructs, signs or submits a transaction."""
import argparse
import copy
import fcntl
import json
import signal
import sys
import threading
import time
from pathlib import Path

from paper.demo import batches
from paper.engine import Engine
from paper.guarded import GuardedEngine
from paper.funding import FundedScalp
from paper.feed import PublicFeed
from paper.model import load_config, validate_config
from paper.server import serve

ROOT = Path(__file__).resolve().parent


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['live', 'demo', 'replay', 'report', 'export', 'dashboard'])
    parser.add_argument('--config', type=Path)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--bind', default='127.0.0.1',
                        help='Dashboard bind address; keep the default for local-only access')
    parser.add_argument('--seconds', type=float, default=0, help='Live duration; 0 keeps running')
    parser.add_argument('--input', type=Path, help='Recorded JSONL for replay')
    parser.add_argument('--output', type=Path, default=ROOT / 'exports')
    parser.add_argument('--no-dashboard', action='store_true')
    parser.add_argument('--publish-registry', action='store_true', help='Publish collector metadata for the separate read-only public worker')
    args = parser.parse_args()
    if args.seconds < 0:
        parser.error('--seconds must be nonnegative')
    if args.command == 'replay' and not args.input:
        parser.error('replay requires --input')
    default_config = ROOT / ('paper.cadence.config.json' if args.command == 'live' else 'paper.config.json')
    cfg = load_config(args.config or default_config)
    source = 'synthetic_demo' if args.command == 'demo' else 'public_index_live'
    entry_policy = None
    if args.command == 'replay':
        with args.input.open() as f:
            header = json.loads(f.readline())
        if header.get('type') != 'header':
            parser.error('Recording header missing')
        source = header['source']  # replay source remains explicitly demo or observed
        entry_policy = header.get('entry_policy')
        if args.config is None:
            cfg = validate_config(header['config'])
    db = args.db or ROOT / 'data' / ('demo.sqlite' if args.command == 'demo' else
                                    'replay.sqlite' if args.command == 'replay' else
                                    'observer.sqlite')
    # Read commands use the stored contract and do not require matching current defaults.
    if args.command in ('report', 'export', 'dashboard'):
        if not db.exists():
            parser.error(f'Database does not exist: {db}')
        import sqlite3
        con = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        contract = json.loads(con.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
        stored_policy = con.execute("SELECT value FROM meta WHERE key='entry_policy'").fetchone()
        entry_policy = json.loads(stored_policy[0]) if stored_policy else None
        con.close()
        cfg, source = contract['config'], contract['source']
    db.parent.mkdir(parents=True, exist_ok=True)
    lock = open(str(db) + '.lock', 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        parser.error('This database is already open. Use its dashboard or stop its process first.')
    if args.command == 'live' or entry_policy:
        ownership_cfg = entry_policy['config'] if entry_policy else json.loads((ROOT/'ownership.config.json').read_text())
        engine = FundedScalp(db,cfg,source,ownership_cfg,
            view_only=args.command in ('report','export','dashboard'),policy=entry_policy)
    else:
        engine = Engine(db, cfg, source, view_only=args.command in ('report', 'export', 'dashboard'))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    server = None
    feed = None
    brain = None
    account = None
    registry = None
    try:
        if args.command == 'live':
            from paper.brain_service import BrainService
            brain = BrainService(ROOT,engine)
            if args.publish_registry:
                from paper.public_worker import RegistryPublisher
                registry = RegistryPublisher(brain)
            if (ROOT/'data/account.sqlite').exists() and db.resolve()==ROOT/'data/cadence.sqlite':
                from paper.account_view import Account
                account=Account(engine,brain.runner,ROOT/'data/account.sqlite')
        if args.command in ('live', 'dashboard') and not args.no_dashboard:
            server = serve(engine, args.port, brain, account, args.bind)
            print(f'Paper dashboard: http://{args.bind}:{args.port}', flush=True)
        if args.command == 'live':
            # The retired scalp adapter keeps its recorded contract, while discovery
            # follows the active long-hold cohorts' full market-cap universe.
            collector_cfg=copy.deepcopy(cfg)
            collector_cfg['market_cap_min_usd'],collector_cfg['market_cap_max_usd']=brain.long_hold_cap_bounds()
            # Healthy unheld coins must outlive the longest funded observation
            # window. Stale, unsupported and out-of-range quotes still rotate.
            collector_cfg['watch_retention_seconds']=max(collector_cfg['watch_retention_seconds'],
                max(r.cfg['observation_seconds']+r.cfg['max_sample_gap_seconds'] for r in brain.all_long_hold_runners()))
            feed = PublicFeed(collector_cfg)
            started = time.monotonic()
            if account:
                overview=account.build(time.time())
                print(f'PAPER ONLY | account value ${overview["portfolio"]["equity_usd"]:,.2f} | two strategy allocations',flush=True)
            else:
                print(f'SIMULATION | public strategy collector | {len(brain.public_strategies())} independent ledgers', flush=True)
            while not stop.is_set() and (not args.seconds or time.monotonic() - started < args.seconds):
                tick = time.monotonic()
                # Engine remains a read-only feed adapter during the public strategy
                # experiment. The retired scalp rules never receive a batch, so they
                # cannot create entries, exits, or a visible performance history.
                batch = feed.collect(brain.watch_priority())
                batch = brain.annotate(batch)
                brain.step_ladder(batch)
                print(json.dumps({'scope':'public_strategy_collector','at':batch['at'],
                    'source_status':batch.get('status'),'observations':len(batch['snapshots']),
                    'strategies':len(brain.all_long_hold_runners())}), flush=True)
                stop.wait(max(0.1, cfg['poll_seconds'] - (time.monotonic() - tick)))
        elif args.command == 'demo':
            for batch in batches():
                engine.step(batch)
            print('Synthetic demonstration only; these results are not market evidence.')
        elif args.command == 'replay':
            if header.get('funding'):
                engine.load_replay_funding(header['funding'])
            with args.input.open() as f:
                next(f)
                for line in f:
                    if line.strip():
                        engine.step(json.loads(line))
            print('Replay finished. Source and configuration are recorded in the report.')
        elif args.command == 'dashboard':
            if args.no_dashboard:
                parser.error('dashboard cannot be used with --no-dashboard')
            while not stop.wait(1):
                pass
        if args.command != 'dashboard':
            args.output.mkdir(parents=True, exist_ok=True)
            label = db.stem
            report = engine.report()
            (args.output / (label + '.report.json')).write_text(json.dumps(report, indent=2))
            (args.output / (label + '.trades.csv')).write_text(engine.export_csv())
            if args.command == 'export':
                engine.export_recording(args.output / (label + '.recording.jsonl'))
            print(json.dumps({k: report[k] for k in ('source', 'closed_trades', 'total_pnl_usd',
                  'realized_net_pnl_usd', 'unrealized_pnl_usd', 'observations')}, indent=2))
            print(f'Report and CSV: {args.output}')
    finally:
        if registry:
            registry.close()
        if brain:
            brain.close()
        if feed:
            feed.close()
        if server:
            server.shutdown()
            server.server_close()
        engine.close()
        lock.close()


if __name__ == '__main__':
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f'Paper simulator stopped: {exc}', file=sys.stderr)
        sys.exit(1)
