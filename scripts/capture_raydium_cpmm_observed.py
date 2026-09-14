#!/usr/bin/env python3
"""Capture one free-RPC Raydium CPMM observed snapshot; never writes a simulation."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.market_archive import ArchiveError, MarketArchive
from paper.raydium_cpmm_observed import RaydiumObservedError, collect


def write_new(path, value):
    if path.exists(): raise ValueError('report destination already exists')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(value, sort_keys=True, indent=2) + '\n'); stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try: os.unlink(temporary)
        except FileNotFoundError: pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path, help='Existing catalog archive; opened read-only')
    parser.add_argument('--pool-id', required=True, help='Archived Raydium CPMM pool identity')
    parser.add_argument('--sequence', required=True, type=int, help='Operator-assigned observed capture sequence')
    parser.add_argument('--commitment', choices=('confirmed', 'finalized'), default='finalized')
    parser.add_argument('--report', required=True, type=Path, help='New snapshot report')
    args = parser.parse_args()
    if args.report.exists(): parser.error('report destination must be new')
    try:
        archive = MarketArchive(args.archive, view_only=True)
        try: report = collect(args.pool_id, archive, args.sequence, args.commitment)
        finally: archive.close()
        write_new(args.report, report)
        print(json.dumps({key: report[key] for key in ('mode', 'pool_id', 'context_slot', 'commitment', 'sha256')}, sort_keys=True))
    except (ArchiveError, RaydiumObservedError, OSError, ValueError) as exc:
        parser.exit(1, 'Raydium CPMM observed capture failed: ' + str(exc) + '\n')


if __name__ == '__main__': main()
