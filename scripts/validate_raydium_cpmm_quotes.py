#!/usr/bin/env python3
"""Validate saved Raydium CPMM historical quote cases without network access."""
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
from paper.raydium_cpmm_validation import RaydiumShadowValidationError, validate


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
    parser.add_argument('--input', required=True, type=Path, help='Saved validation corpus JSON; no network request')
    parser.add_argument('--archive', required=True, type=Path, help='Existing market archive; read-only')
    parser.add_argument('--report', required=True, type=Path, help='New validation report')
    args = parser.parse_args()
    if args.report.exists():
        parser.error('report destination must be new')
    try:
        if not args.input.is_file():
            parser.error('input must be an existing regular file')
        recording = json.loads(args.input.read_text())
        archive = MarketArchive(args.archive, view_only=True)
        try:
            report = validate(recording, archive)
            write_new(args.report, report)
        finally:
            archive.close()
        print(json.dumps({'mode': report['mode'], **report['summary'], 'sha256': report['sha256']}, sort_keys=True))
    except (ArchiveError, RaydiumShadowValidationError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, 'Raydium CPMM validation failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
