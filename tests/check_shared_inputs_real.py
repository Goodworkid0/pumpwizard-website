"""Disposable real-ledger conversion and complete installed-code replay."""
import argparse
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from paper.shared_inputs import Archive
from paper.replay_package import create,verify
from scripts.compact_shared_inputs import Importer,convert

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--source',type=Path,required=True)
    args=p.parse_args();started=time.time()
    with tempfile.TemporaryDirectory(prefix='pump-shared-real-') as tmp:
        root=Path(tmp); source=root/'source.sqlite'
        a=sqlite3.connect(args.source.resolve().as_uri()+'?mode=ro',uri=True);b=sqlite3.connect(source)
        a.backup(b);a.close();b.close()
        archive=Archive(root/'shared-research.sqlite')
        try: result=convert(source,root/'compact.sqlite',Importer(archive))
        finally:archive.close()
        replay=create(root/'compact.sqlite',root/'shared-research.sqlite',root/'package',
                      frozen_source=True,max_inputs=50000,max_bytes=1024**3,max_seconds=600)
        if replay!=verify(root/'package'): raise ValueError('Independent replay verification differs')
        print(json.dumps({'status':'verified','source':args.source.name,'seconds':round(time.time()-started,2),
                          'conversion':result,'replay':replay},indent=2))
