#!/usr/bin/env python3
"""Run a finite schedule of explicit Raydium observed-paper pilot decisions.

It reads an existing observed snapshot log and writes only to the separately
contracted Raydium paper ledger.  It performs no RPC call, wallet operation, or
public API write.  A separate snapshot monitor must already be running.
"""
import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.market_archive import ArchiveError, MarketArchive
from paper.observed_market_log import ObservedLogError, ObservedMarketLog
from paper.raydium_cpmm_observed_ledger import ObservedRaydiumLedger, RaydiumObservedLedgerError
from paper.raydium_cpmm_pilot import ObservedRaydiumPilot, RaydiumPilotError


def positive_count(value):
    value = int(value)
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError('must be between 1 and 1000')
    return value


def cadence(value):
    value = float(value)
    if value < 15:
        raise argparse.ArgumentTypeError('must be at least 15 seconds')
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--store', required=True, type=Path)
    parser.add_argument('--ledger', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path)
    parser.add_argument('--interval-seconds', type=cadence, default=60)
    parser.add_argument('--max-decisions', type=positive_count, default=1)
    args = parser.parse_args()
    try:
        pilot = ObservedRaydiumPilot(json.loads(args.config.read_text()))
        archive, log = MarketArchive(args.archive, view_only=True), ObservedMarketLog(args.store, view_only=True)
        source = {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only'}
        ledger = ObservedRaydiumLedger(args.ledger, {
            'pool_id': pilot.config['pool_id'], 'initial_quote_amount': pilot.config['initial_quote_amount'],
            'max_positions': 1, 'max_snapshot_age_seconds': 120,
        }, source)
        try:
            for index in range(args.max_decisions):
                print(json.dumps(pilot.step(archive, log, ledger), sort_keys=True), flush=True)
                if index + 1 < args.max_decisions:
                    time.sleep(args.interval_seconds)
        finally:
            ledger.close(); archive.close(); log.close()
    except KeyboardInterrupt:
        parser.exit(130, 'Raydium CPMM pilot monitor interrupted; existing paper events remain immutable.\n')
    except (ArchiveError, ObservedLogError, RaydiumObservedLedgerError, RaydiumPilotError,
            OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, 'Raydium CPMM pilot monitor failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
