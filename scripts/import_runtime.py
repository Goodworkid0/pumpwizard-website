"""Import frozen long-hold ledgers into a NEW PostgreSQL runtime namespace."""
import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from paper.postgres_runtime import Backend


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--source',type=Path,required=True)
    parser.add_argument('--namespace',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--frozen-source',action='store_true')
    args=parser.parse_args()
    paths=([args.source/'shared-research.sqlite'] if (args.source/'shared-research.sqlite').exists() else [])+sorted(args.source.glob('long-hold-*.sqlite'))
    if not args.apply:
        print(json.dumps({'namespace':args.namespace,'ledgers':[p.name for p in paths],'mode':'plan_only'})); return
    if not args.frozen_source or not paths: parser.error('A complete frozen source and --frozen-source are required')
    backend=Backend(os.environ['PUMP_POSTGRES_DSN'],args.namespace)
    try:
        for path in paths:
            backend.import_ledger(path,frozen=True,shared_name='shared-research.sqlite' if paths[0].name=='shared-research.sqlite' else None)
            print('Imported and reconciled: '+path.name,flush=True)
    finally: backend.close()


if __name__=='__main__': main()
