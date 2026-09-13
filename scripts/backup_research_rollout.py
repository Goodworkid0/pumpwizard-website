"""Online SQLite backup of existing cohorts before the additive research rollout."""
import datetime
import json
from pathlib import Path
import shutil
import sqlite3
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from paper.brain_service import experiment_entries


def main():
    stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    destination=ROOT/'exports'/('before-research-matrix-'+stamp)
    destination.mkdir(parents=True,exist_ok=False)
    entries=experiment_entries(json.loads((ROOT/'long_hold_experiments.json').read_text()))
    sources=[ROOT/'data/coin-memory.sqlite']+[ROOT/'data'/('long-hold-'+e['id']+'.sqlite') for e in entries]
    counts={}
    for path in sources:
        with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as source, sqlite3.connect(destination/path.name) as backup:
            source.backup(backup)
            assert backup.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            if path.name!='coin-memory.sqlite':
                counts[path.stem]={'entries':backup.execute("SELECT count(*) FROM events WHERE action='fill_entry'").fetchone()[0],
                                  'closed':backup.execute('SELECT count(*) FROM trades').fetchone()[0]}
    for name in ('long_hold_experiments.json','brain.config.json','research_experiments.json'):
        shutil.copy2(ROOT/name,destination/name)
    (destination/'ledger-counts.json').write_text(json.dumps(counts,indent=2)+'\n')
    print(json.dumps({'backup':str(destination),'databases':len(sources),'legacy_strategies':len(entries)},indent=2))


if __name__=='__main__':main()
