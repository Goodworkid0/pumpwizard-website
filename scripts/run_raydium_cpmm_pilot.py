#!/usr/bin/env python3
"""Run one explicit observed-only Raydium pilot decision; no order is sent."""
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
from paper.raydium_cpmm_pilot import ObservedRaydiumPilot, RaydiumPilotError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path)
    parser.add_argument('--store', required=True, type=Path)
    parser.add_argument('--ledger', required=True, type=Path)
    parser.add_argument('--config', required=True, type=Path, help='Immutable pilot JSON configuration')
    parser.add_argument('--inspect', action='store_true', help='Read evidence without creating a paper-ledger event')
    args = parser.parse_args()
    try:
        config = json.loads(args.config.read_text())
        pilot = ObservedRaydiumPilot(config)
        archive, log = MarketArchive(args.archive, view_only=True), ObservedMarketLog(args.store, view_only=True)
        try:
            if args.inspect:
                result = pilot.inspect(archive, log)
            else:
                source = {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only'}
                ledger = ObservedRaydiumLedger(args.ledger, {
                    'pool_id': pilot.config['pool_id'],
                    'initial_quote_amount': pilot.config['initial_quote_amount'],
                    'max_positions': 1,
                    'max_snapshot_age_seconds': 120,
                }, source)
                try:
                    result = pilot.step(archive, log, ledger)
                finally:
                    ledger.close()
            print(json.dumps(result, sort_keys=True))
        finally:
            archive.close(); log.close()
    except (ArchiveError, ObservedLogError, RaydiumObservedLedgerError, RaydiumPilotError,
            OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, 'Raydium CPMM pilot failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
