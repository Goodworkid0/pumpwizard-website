"""Internal isolated-interpreter segment verifier; invoked by replay_chain.py."""
import argparse
import base64
import copy
import gzip
import hashlib
import json
from pathlib import Path
import socket
import sqlite3
import sys
import tempfile
import time
from types import SimpleNamespace
import zlib


def main():
    p=argparse.ArgumentParser();
    for name in ('release','checkpoint','inputs','expected','output'): p.add_argument('--'+name,type=Path,required=True)
    args=p.parse_args(); deadline=time.monotonic()+600
    # This is an execution guard for reviewed source, not a sandbox for malicious code.
    def no_network(*_,**__): raise RuntimeError('Network unavailable during historical replay')
    socket.socket=no_network; socket.create_connection=no_network
    sys.path.insert(0,str(args.release))
    from paper.engine import canonical
    from paper.ladder import LadderRunner
    from paper.research_runner import ResearchRunner
    classes=[LadderRunner,ResearchRunner]
    for module,names in (('versioned_trials',('TrialLadderRunner','TrialResearchRunner')),
                         ('forward_trials_v3',('V3LadderRunner','V3ResearchRunner'))):
        if (args.release/'paper'/(module+'.py')).exists():
            import importlib
            loaded=importlib.import_module('paper.'+module)
            classes.extend(getattr(loaded,name) for name in names)
    if args.checkpoint.is_symlink() or any(Path(str(args.checkpoint)+suffix).exists() and Path(str(args.checkpoint)+suffix).stat().st_size for suffix in ('-wal','-journal')):
        raise ValueError('Use a frozen checkpoint')
    con=sqlite3.connect(args.checkpoint.resolve().as_uri()+'?mode=ro&immutable=1',uri=True)
    con.row_factory=sqlite3.Row
    contract=json.loads(con.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
    cls=None
    for candidate in classes:
        files=('ladder.py','model.py',*getattr(candidate,'extra_code_files',()))
        code=b''.join((args.release/'paper'/name).read_bytes() for name in files)
        if hashlib.sha256(code).hexdigest()==contract['code_hash']: cls=candidate; break
    if cls is None: raise ValueError('Exact historical source is missing')
    def proof(db,state):
        tables={}
        for name in ('events','sales','trades','equity'):
            h=hashlib.sha256(); count=0
            for row in db.execute('SELECT * FROM '+name+' ORDER BY id'):
                h.update((canonical(dict(row))+'\n').encode()); count+=1
            tables[name]={'rows':count,'sha256':h.hexdigest()}
        return {'state':copy.deepcopy(state),'tables':tables}
    expected=json.loads(args.expected.read_text())
    shared=SimpleNamespace(features={},last_input_id=None,last_at=None)
    with tempfile.TemporaryDirectory(prefix='pump-segment-') as temporary:
        path=Path(temporary)/'ledger.sqlite'
        target=sqlite3.connect(path); con.backup(target); target.close(); con.close()
        parameters=[path,contract['config'],contract['ownership_config']]
        if issubclass(cls,ResearchRunner): parameters.append(shared)
        kwargs={'source':contract['source']}
        if 'experiment' in contract: kwargs['definition']=contract['experiment']
        runner=cls(*parameters,**kwargs)
        try:
            if runner.contract!=contract: raise ValueError('Historical contract drift')
            initial=proof(runner.db,runner.state); count=0; previous=runner.state['last_at']
            with gzip.open(args.inputs,'rt') as inputs:
                while line:=inputs.readline(64*1024**2+1):
                    if len(line)>64*1024**2 or count>=50000 or time.monotonic()>deadline: raise ValueError('Segment budget exceeded')
                    row=json.loads(line); decoder=zlib.decompressobj()
                    raw=decoder.decompress(base64.b64decode(row['payload_zlib_base64'],validate=True),64*1024**2+1)
                    if len(raw)>64*1024**2 or not decoder.eof or decoder.unused_data or hashlib.sha256(raw).hexdigest()!=row['id']:
                        raise ValueError('Invalid archived input')
                    batch=json.loads(raw); at=batch['at']
                    if at!=row['at'] or previous is not None and at<=previous: raise ValueError('Invalid segment chronology')
                    previous=at; count+=1
                    shared.last_input_id=row['id']; shared.last_at=at
                    shared.features={(f['mint'],f['window']):{k:v for k,v in f.items() if k not in ('mint','window')} for f in batch.get('features',[])}
                    if issubclass(cls,ResearchRunner): batch.pop('features',None)
                    runner.step(batch)
            final=proof(runner.db,runner.state)
            if not count or final!=expected: raise ValueError('Historical final state or records differ')
            with args.output.open('x') as stream: json.dump({'initial':initial,'final':final,'contract':contract,'inputs':count},stream)
        finally: runner.close()


if __name__=='__main__': main()
