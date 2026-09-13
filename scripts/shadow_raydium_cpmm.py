#!/usr/bin/env python3
"""Opt-in Raydium CPMM catalog discovery; never starts a strategy or collector."""
import argparse
import json
import os
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.market_archive import ArchiveError, MarketArchive
from paper.raydium_cpmm import RaydiumCatalogError, fetch_catalog, ingest_catalog


def write_new(path, value):
    if path.exists():
        raise ValueError('report destination already exists')
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + '.', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(json.dumps(value, sort_keys=True, indent=2) + '\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument('--input', type=Path, help='Recorded Raydium catalog JSON; no network request')
    source.add_argument('--fetch', action='store_true', help='Fetch one fixed public Raydium catalog page')
    parser.add_argument('--archive', required=True, type=Path, help='Market archive created/updated only by this command')
    parser.add_argument('--report', required=True, type=Path, help='New JSON report containing raw provider input and proof')
    parser.add_argument('--size', type=int, default=100, help='Catalog page size used only with --fetch (1-100)')
    args = parser.parse_args()
    if args.report.exists():
        parser.error('report destination must be new')
    try:
        if args.fetch:
            page, source_url = fetch_catalog(args.size)
        else:
            if not args.input.is_file():
                parser.error('input must be an existing regular file')
            page, source_url = json.loads(args.input.read_text()), args.input.resolve().as_uri()
        archive = MarketArchive(args.archive)
        try:
            report = ingest_catalog(page, archive, time.time(), source_url)
            write_new(args.report, report)
        finally:
            archive.close()
        print(json.dumps({key: report[key] for key in ('mode', 'appended', 'accepted_pool_ids', 'rejected', 'sha256')},
                         sort_keys=True))
    except (ArchiveError, RaydiumCatalogError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, 'Raydium CPMM shadow collection failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
