#!/usr/bin/env python3
"""Run one explicit observed Raydium paper-ledger action; no order is sent."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.market_archive import ArchiveError, MarketArchive
from paper.observed_market_log import ObservedLogError, ObservedMarketLog
from paper.raydium_cpmm_observed_ledger import ObservedRaydiumLedger, RaydiumObservedLedgerError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path, help='Catalog archive; opened read-only')
    parser.add_argument('--store', required=True, type=Path, help='Observed log; opened read-only')
    parser.add_argument('--ledger', required=True, type=Path, help='Separate Raydium observed ledger')
    parser.add_argument('--pool-id', required=True)
    parser.add_argument('--initial-quote-amount', required=True, type=int, help='Raw quote-token paper balance')
    parser.add_argument('--max-positions', type=int, default=1)
    parser.add_argument('--max-snapshot-age-seconds', type=float, default=120)
    action = parser.add_subparsers(dest='action', required=True)
    opened = action.add_parser('open', help='Simulate quote-token entry')
    opened.add_argument('--position-id', required=True); opened.add_argument('--sequence', required=True, type=int)
    opened.add_argument('--quote-amount', required=True, type=int)
    closed = action.add_parser('close', help='Simulate base-token exit')
    closed.add_argument('--position-id', required=True); closed.add_argument('--sequence', required=True, type=int)
    summary = action.add_parser('summary', help='Read P/L and marks without mutation')
    summary.add_argument('--mark-sequence', type=int)
    args = parser.parse_args()
    config = {'pool_id': args.pool_id, 'initial_quote_amount': args.initial_quote_amount,
              'max_positions': args.max_positions, 'max_snapshot_age_seconds': args.max_snapshot_age_seconds}
    source = {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only'}
    try:
        archive, log = MarketArchive(args.archive, view_only=True), ObservedMarketLog(args.store, view_only=True)
        ledger = ObservedRaydiumLedger(args.ledger, config, source)
        try:
            if args.action == 'open': result = ledger.open_position(args.position_id, args.sequence, args.quote_amount, archive, log)
            elif args.action == 'close': result = ledger.close_position(args.position_id, args.sequence, archive, log)
            else: result = ledger.summary(archive, log, args.mark_sequence)
            print(json.dumps(result, sort_keys=True))
        finally:
            ledger.close(); archive.close(); log.close()
    except (ArchiveError, ObservedLogError, RaydiumObservedLedgerError, OSError, ValueError) as exc:
        parser.exit(1, 'Raydium CPMM observed ledger failed: ' + str(exc) + '\n')


if __name__ == '__main__': main()
