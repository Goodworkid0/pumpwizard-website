"""Create or verify an offline replay package from explicitly frozen backups."""
import argparse
import json
import os
import copy
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from paper.replay_package import create, verify


def demo(destination):
    """A small reproducible example with invented quotes, one entry and one exit."""
    from paper.experiment_catalog import catalog
    from paper.research_runner import ResearchRunner
    from paper.shared_research import SharedResearch
    root = Path(__file__).resolve().parents[1]
    base = json.loads((root/'long_hold_experiments.json').read_text())['experiments'][0]['config']
    config = copy.deepcopy(next(e['config'] for e in catalog(base) if e['id']=='r1-persistent-b3-300s-7d'))
    config['failed_attempt_probability'] = 0
    risk = json.loads((root/'ownership.config.json').read_text())
    with tempfile.TemporaryDirectory(prefix='pumpwizard-example-') as temp:
        folder = Path(temp); shared = SharedResearch(folder/'shared.sqlite')
        runner = ResearchRunner(folder/'ledger.sqlite',config,risk,shared,'synthetic_example')
        try:
            for index in range(18):
                at=1000+index*30; cap=150000+index*1000 if index<15 else 80000
                quote={'mint':'SYNTHETIC-NOT-A-TOKEN','symbol':'EXAMPLE','observed_at':at,
                       'dex_id':'pumpswap','pair_address':'synthetic-pool','valuation_basis':'fdv',
                       'market_cap_usd':cap,'price_usd':cap/1e9,'base_reserve':200000000,
                       'liquidity_usd':cap*.4,'source_timestamp_known':False}
                batch={'at':at,'quotes':[quote],'evidence':{}}
                shared.prepare(batch,runner.market); runner.step(batch)
        finally: runner.close(); shared.close()
        return create(folder/'ledger.sqlite',folder/'shared.sqlite',destination,frozen_source=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    export = sub.add_parser('create')
    export.add_argument('--ledger', type=Path, required=True)
    export.add_argument('--shared', type=Path)
    export.add_argument('--destination', type=Path, required=True)
    export.add_argument('--frozen-source', action='store_true')
    export.add_argument('--max-mib', type=int, default=64, help='Operator budget, at most 1024 MiB')
    export.add_argument('--max-inputs', type=int, default=10000, help='At most 50,000 batches')
    export.add_argument('--max-seconds', type=int, default=180, help='Replay time budget, at most 600 seconds')
    check = sub.add_parser('verify')
    check.add_argument('--package', type=Path, required=True)
    example = sub.add_parser('demo')
    example.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        result = (demo(args.destination) if args.command == 'demo' else verify(args.package) if args.command == 'verify' else
                  create(args.ledger, args.shared, args.destination, frozen_source=args.frozen_source,
                         max_bytes=args.max_mib*1024*1024, max_inputs=args.max_inputs, max_seconds=args.max_seconds))
    except (ValueError, OSError) as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__': main()
