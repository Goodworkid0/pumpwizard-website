#!/usr/bin/env python3
"""Verify an existing immutable market archive without modifying it."""
import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.market_archive import ArchiveError, MarketArchive


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', required=True, type=Path,
                        help='Existing SQLite market archive; it is opened read-only')
    args = parser.parse_args()
    if not args.archive.is_file():
        parser.error('archive must be an existing regular file')
    try:
        archive = MarketArchive(args.archive, view_only=True)
        try:
            print(json.dumps(archive.verify(), sort_keys=True, indent=2))
        finally:
            archive.close()
    except (ArchiveError, OSError, sqlite3.Error) as exc:
        parser.exit(1, 'Market archive verification failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
