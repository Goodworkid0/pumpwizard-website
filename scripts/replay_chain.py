"""Verify checkpoint-linked implementation eras using explicitly trusted local releases.

Historical code is never loaded from the research artifact. The operator must
provide separate reviewed installations and exact code-era checkpoints.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def local_file(root,name):
    path=root/name
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError('Chain member must be a regular file inside the artifact')
    return path


def verify_chain(manifest, releases, *, trusted=False):
    if not trusted: raise ValueError('Explicit trust of separate local source releases is required')
    manifest=Path(manifest); root=manifest.parent
    if manifest.stat().st_size>1_000_000: raise ValueError('Oversized chain manifest')
    data=json.loads(manifest.read_text())
    if data.get('schema')!='pumpwizard.replay-chain/v1' or not 1<=len(data['segments'])<=20:
        raise ValueError('Invalid chain schema or segment count')
    previous=None; proofs=[]; contract=None
    with tempfile.TemporaryDirectory(prefix='pump-replay-chain-') as temporary:
        for index,segment in enumerate(data['segments']):
            release=Path(releases[segment['release']]).resolve()
            if release.is_relative_to(root.resolve()): raise ValueError('Never execute a release bundled in the artifact')
            files={k:str(local_file(root,segment[k])) for k in ('checkpoint','inputs','expected')}
            output=Path(temporary)/(str(index)+'.json')
            result=subprocess.run([sys.executable,'-I',str(ROOT/'scripts/replay_segment.py'),
                '--release',str(release),'--checkpoint',files['checkpoint'],'--inputs',files['inputs'],
                '--expected',files['expected'],'--output',str(output)],capture_output=True,timeout=660)
            if result.returncode: raise ValueError('Segment '+str(index)+' did not verify; missing source, boundary or evidence')
            proof=json.loads(output.read_text())
            if previous is not None and proof['initial']!=previous:
                raise ValueError('Checkpoint continuity mismatch; do not splice unrelated histories')
            rules={k:v for k,v in proof['contract'].items() if k!='code_hash'}
            if contract is not None and rules!=contract: raise ValueError('Rule changes require a separate instance, not a code-era splice')
            contract=rules; previous=proof['final']; proofs.append(proof)
    full=proofs[0]['initial']['state'].get('started_at') is None and all(v['rows']==0 for v in proofs[0]['initial']['tables'].values())
    return {'status':'verified','segments':len(proofs),'inputs':sum(p['inputs'] for p in proofs),
            'scope':'complete_from_initial_state' if full else 'checkpoint_forward_only',
            'proofs':proofs,'limitations':'Trusted code-era checkpoints are required. This cannot reconstruct missing historical source or infer unknown boundaries.'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--releases',type=Path,required=True,help='JSON map of release IDs to separate trusted source roots')
    p.add_argument('--trust-local-releases',action='store_true'); p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.output.exists(): p.error('Output must be new')
    result=verify_chain(args.manifest,json.loads(args.releases.read_text()),trusted=args.trust_local_releases)
    with args.output.open('x') as stream: json.dump(result,stream,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='proofs'},indent=2))
