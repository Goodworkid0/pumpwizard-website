#!/usr/bin/env python3
"""Verify an observed-only market log without contacting a network or changing it."""
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
    parser.add_argument('--store', required=True, type=Path)
    args = parser.parse_args()
    try:
        log = ObservedMarketLog(args.store, view_only=True)
        try: print(json.dumps(log.verify(), sort_keys=True))
        finally: log.close()
    except (ObservedLogError, OSError) as exc:
        parser.exit(1, 'Observed market log verification failed: ' + str(exc) + '\n')


if __name__ == '__main__': main()
