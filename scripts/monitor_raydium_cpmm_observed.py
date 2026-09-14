#!/usr/bin/env python3
"""Run a bounded observed-only Raydium CPMM capture schedule; no trading occurs."""
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
from paper.raydium_cpmm_observed_monitor import RaydiumObservedMonitorError, capture_once


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
    parser.add_argument('--archive', required=True, type=Path, help='Existing catalog archive; opened read-only')
    parser.add_argument('--store', required=True, type=Path, help='Observed-only SQLite log; created if needed')
    parser.add_argument('--pool-id', required=True, help='Archived Raydium CPMM pool identity')
    parser.add_argument('--commitment', choices=('confirmed', 'finalized'), default='finalized')
    parser.add_argument('--interval-seconds', type=cadence, default=60.0,
                        help='Spacing between captures (minimum 15; default 60)')
    parser.add_argument('--max-captures', type=positive_count, default=1,
                        help='Finite number of captures, 1-1000 (default 1)')
    args = parser.parse_args()
    try:
        archive, log = MarketArchive(args.archive, view_only=True), ObservedMarketLog(args.store)
        try:
            for index in range(args.max_captures):
                report = capture_once(args.pool_id, archive, log, args.commitment, args.interval_seconds)
                print(json.dumps(report, sort_keys=True), flush=True)
                if index + 1 < args.max_captures:
                    time.sleep(args.interval_seconds)
        finally:
            archive.close(); log.close()
    except KeyboardInterrupt:
        parser.exit(130, 'Raydium CPMM observed monitor interrupted; completed snapshots remain immutable.\n')
    except (ArchiveError, ObservedLogError, RaydiumObservedMonitorError, OSError, ValueError) as exc:
        parser.exit(1, 'Raydium CPMM observed monitor failed: ' + str(exc) + '\n')


if __name__ == '__main__': main()
