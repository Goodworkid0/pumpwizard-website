#!/usr/bin/env python3
"""Append one verified free-RPC Raydium snapshot report to an observed log."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.observed_market_log import ObservedLogError, ObservedMarketLog


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--store', required=True, type=Path, help='Observed-only SQLite log; created if needed')
    parser.add_argument('--report', required=True, type=Path, help='Existing observed snapshot JSON report')
    args = parser.parse_args()
    try:
        with args.report.open() as stream:
            report = json.load(stream)
        log = ObservedMarketLog(args.store)
        try:
            added = log.append(report)
            proof = log.verify()
            expected = log.next_sequence(report['pool_id'])
        finally:
            log.close()
        print(json.dumps({'added': added, 'pool_id': report['pool_id'], 'sequence': report['sequence'],
                          'next_sequence': expected, 'manifest_sha256': proof['sha256']}, sort_keys=True))
    except (ObservedLogError, OSError, ValueError, json.JSONDecodeError, KeyError) as exc:
        parser.exit(1, 'Raydium CPMM observed record failed: ' + str(exc) + '\n')


if __name__ == '__main__': main()
