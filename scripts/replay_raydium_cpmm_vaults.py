#!/usr/bin/env python3
"""Replay a saved Raydium CPMM vault capture; this command never contacts RPC."""
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
from paper.raydium_cpmm_vaults import RaydiumVaultReplayError, ingest_vault_recording


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
    parser.add_argument('--input', required=True, type=Path, help='Saved vault recording JSON; no network request')
    parser.add_argument('--archive', required=True, type=Path, help='Existing market archive')
    parser.add_argument('--report', required=True, type=Path, help='New proof report')
    args = parser.parse_args()
    if args.report.exists():
        parser.error('report destination must be new')
    try:
        if not args.input.is_file():
            parser.error('input must be an existing regular file')
        recording = json.loads(args.input.read_text())
        archive = MarketArchive(args.archive)
        try:
            report = ingest_vault_recording(recording, archive)
            write_new(args.report, report)
        finally:
            archive.close()
        print(json.dumps({key: report[key] for key in ('mode', 'appended_pool_states', 'state_ids', 'sha256')},
                         sort_keys=True))
    except (ArchiveError, RaydiumVaultReplayError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, 'Raydium CPMM vault replay failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
