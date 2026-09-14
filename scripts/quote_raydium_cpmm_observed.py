#!/usr/bin/env python3
"""Read one observed Raydium CPMM CPMM estimate; no network or mutation occurs."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.market_archive import ArchiveError, MarketArchive
from paper.observed_market_log import ObservedLogError, ObservedMarketLog
from paper.raydium_cpmm_observed_quote import RaydiumObservedQuoteError, quote


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path, help='Catalog archive; opened read-only')
    parser.add_argument('--store', required=True, type=Path, help='Observed log; opened read-only')
    parser.add_argument('--pool-id', required=True)
    parser.add_argument('--sequence', required=True, type=int)
    parser.add_argument('--input-mint', required=True)
    parser.add_argument('--amount-in', required=True, type=int, help='Raw token base units')
    args = parser.parse_args()
    try:
        archive, log = MarketArchive(args.archive, view_only=True), ObservedMarketLog(args.store, view_only=True)
        try: print(json.dumps(quote(args.pool_id, args.sequence, args.input_mint, args.amount_in, archive, log), sort_keys=True))
        finally: archive.close(); log.close()
    except (ArchiveError, ObservedLogError, RaydiumObservedQuoteError, OSError, ValueError) as exc:
        parser.exit(1, 'Raydium CPMM observed quote failed: ' + str(exc) + '\n')


if __name__ == '__main__': main()
