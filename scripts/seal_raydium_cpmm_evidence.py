#!/usr/bin/env python3
"""Seal a saved Raydium CPMM evidence corpus without contacting RPC or a database."""
import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from paper.raydium_cpmm_evidence import RaydiumEvidenceError, seal


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
    parser.add_argument('--input', required=True, type=Path, help='Saved evidence corpus JSON; no network request')
    parser.add_argument('--report', required=True, type=Path, help='New sealed corpus report')
    args = parser.parse_args()
    if args.report.exists():
        parser.error('report destination must be new')
    try:
        if not args.input.is_file():
            parser.error('input must be an existing regular file')
        report = seal(json.loads(args.input.read_text()))
        write_new(args.report, report)
        print(json.dumps({'mode': report['mode'], **report['validation'],
                          'review_ready': report['review']['ready_for_human_review'], 'sha256': report['sha256']},
                         sort_keys=True))
    except (RaydiumEvidenceError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(1, 'Raydium CPMM evidence sealing failed: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
