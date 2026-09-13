"""Verify and publish one reviewed package locally. No remote writes or live queries."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from paper.replay_package import verify, file_digest
from paper.package_registry import Packages, ID


def publish(source, destination, instance, *, reviewed=False):
    if not reviewed: raise ValueError('Explicit public-input and source review is required')
    if not ID.fullmatch(instance): raise ValueError('Invalid instance')
    verify(source)
    source, destination = Path(source), Path(destination)
    manifest_raw=(source/'manifest.json').read_bytes()
    expected=json.loads(manifest_raw)['files']
    contract = json.loads((source/'contract.json').read_text())
    state = json.loads((source/'expected.json').read_text())['state']
    # Trial IDs are intrinsic; originals must be associated by the reviewing operator.
    trial = contract.get('experiment')
    if trial and instance != '-'.join(trial[k] for k in ('trial_id','group','arm')):
        raise ValueError('Instance differs from trial contract')
    destination.mkdir(mode=0o755,parents=True,exist_ok=True)
    if destination.is_symlink(): raise ValueError('Symlink destination refused')
    with (destination/'.publish.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='.publish-',dir=destination) as temp:
            archive=Path(temp)/'archive.zip'
            with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_STORED) as bundle:
                for path in sorted(source.iterdir()):
                    if path.is_symlink() or not path.is_file(): raise ValueError('Invalid package member')
                    h=hashlib.sha256(); count=0
                    with path.open('rb') as incoming, bundle.open(path.name,'w',force_zip64=True) as outgoing:
                        while block:=incoming.read(1024*1024):
                            outgoing.write(block); h.update(block); count+=len(block)
                    record=expected.get(path.name)
                    if path.name=='manifest.json':
                        if h.hexdigest()!=hashlib.sha256(manifest_raw).hexdigest(): raise ValueError('Manifest changed during publication')
                    elif record is None or (h.hexdigest(),count)!=(record['sha256'],record['bytes']):
                        raise ValueError('Package changed during publication')
                if {p.name for p in source.iterdir()} != set(expected)|{'manifest.json'}:
                    raise ValueError('Package inventory changed')
            digest=file_digest(archive); target=destination/(digest+'.zip')
            if target.exists():
                if target.is_symlink() or file_digest(target)!=digest: raise ValueError('Publication collision')
            else:
                archive.chmod(0o644); os.link(archive,target)
            row={'instance':instance,'sha256':digest,'bytes':target.stat().st_size,
                 'published_at':time.time(),'observed_through':state['last_at'],
                 'scope':'Complete frozen instance input history; replay-derived screens; separate future price labels.',
                 'schema':'pumpwizard.replay-package/v1'}
            rows=[r for r in Packages(destination).records() if r['sha256']!=digest]
            rows.append(row)
            registry=Path(temp)/'registry.json'
            with registry.open('w') as stream:
                json.dump({'schema':'pumpwizard.public-packages/v1','packages':rows},stream)
                stream.flush(); os.fsync(stream.fileno())
            registry.chmod(0o644); registry.replace(destination/'registry.json')
            return row


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--package',type=Path,required=True)
    parser.add_argument('--destination',type=Path,required=True)
    parser.add_argument('--instance',required=True)
    parser.add_argument('--reviewed-public-inputs',action='store_true')
    args=parser.parse_args()
    print(json.dumps(publish(args.package,args.destination,args.instance,reviewed=args.reviewed_public_inputs),indent=2))
