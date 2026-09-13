"""Verify staged SQLite bytes and tables against a frozen snapshot manifest."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from scripts.backup_lab import digest, database_check


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--manifest',type=Path,required=True)
    p.add_argument('--data',type=Path,required=True)
    args=p.parse_args()
    manifest=json.loads(args.manifest.read_text())
    if manifest.get('schema')!='pumpwizard.backup/v1' or not manifest.get('completed_at'):
        raise ValueError('Completed snapshot required')
    expected=[]
    for item in manifest['files']:
        if not item['path'].startswith('data/'): continue
        name=Path(item['path']).name
        if item['path']!='data/'+name or not name.endswith('.sqlite'): raise ValueError('Invalid manifest path')
        file=args.data/name
        if file.is_symlink() or file.stat().st_size!=item['bytes'] or digest(file)!=item['sha256']:
            raise ValueError('Staged file differs: '+name)
        if database_check(file)!=item['table_counts']: raise ValueError('Staged table counts differ: '+name)
        expected.append(name)
    if not expected or set(expected)!={p.name for p in args.data.glob('*.sqlite')}:
        raise ValueError('Staged database inventory differs')
    print(json.dumps({'status':'verified','databases':len(expected),
                      'snapshot_completed_at':manifest['completed_at'],'consistency':manifest['consistency']}))


if __name__=='__main__': main()
