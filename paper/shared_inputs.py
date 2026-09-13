"""Lossless shared raw-input storage, independent of immutable strategy code."""
from collections import OrderedDict
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import zlib

SCHEMA='pumpwizard.shared-inputs/v1'
ARCHIVE='shared-research.sqlite'


def validate(ident, at, payload):
    decoder=zlib.decompressobj()
    raw=decoder.decompress(payload,64*1024*1024+1)
    if len(raw)>64*1024*1024 or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid compressed market input')
    if hashlib.sha256(raw).hexdigest()!=ident or json.loads(raw)['at']!=at:
        raise ValueError('Market input identity or timestamp differs')


class Archive:
    def __init__(self,path):
        self.path=Path(path)
        if self.path.is_symlink(): raise ValueError('Symlink archive refused')
        self.lock=threading.RLock(); self.cache=OrderedDict()
        self.db=sqlite3.connect(self.path,check_same_thread=False,timeout=15)
        self.db.execute('PRAGMA journal_mode=WAL'); self.db.execute('PRAGMA synchronous=FULL')
        self.db.execute('CREATE TABLE IF NOT EXISTS inputs(id TEXT PRIMARY KEY,at REAL,payload BLOB)')
        self.db.commit()

    def put(self,ident,at,payload):
        encoded=hashlib.sha256(payload).digest()
        with self.lock:
            if ident in self.cache:
                if self.cache[ident]!=(at,encoded): raise ValueError('Conflicting immutable market input')
                self.cache.move_to_end(ident); return
            validate(ident,at,payload)
            with self.db:
                old=self.db.execute('SELECT at,payload FROM inputs WHERE id=?',(ident,)).fetchone()
                if old is not None:
                    if old[0]!=at or old[1]!=payload: raise ValueError('Conflicting immutable market input')
                else: self.db.execute('INSERT INTO inputs VALUES (?,?,?)',(ident,at,payload))
            self.cache[ident]=(at,encoded)
            if len(self.cache)>512: self.cache.popitem(last=False)

    def close(self): self.db.close()


def marked(con):
    if not con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='input_storage'").fetchone():
        return False
    row=con.execute("SELECT value FROM input_storage WHERE key='schema'").fetchone()
    if not row or row[0]!=SCHEMA: raise ValueError('Unsupported input storage format')
    return True


def install_reader(con,archive):
    """A connection-local view preserves the original inputs query interface."""
    archive=Path(archive)
    if not archive.is_file() or archive.is_symlink(): raise ValueError('Shared input archive missing')
    con.execute('ATTACH DATABASE ? AS recorded_archive',(archive.resolve().as_uri()+'?mode=ro',))
    con.execute('''CREATE TEMP VIEW recorded_inputs AS
        SELECT id,at,payload FROM main.inputs
        UNION ALL SELECT r.id,r.at,a.payload FROM main.input_refs r
        LEFT JOIN recorded_archive.inputs a ON a.id=r.id
        WHERE NOT EXISTS(SELECT 1 FROM main.inputs i WHERE i.id=r.id)''')


class Connection:
    def __init__(self,con,archive=None): self.con,self.archive=con,archive
    def __getattr__(self,name): return getattr(self.con,name)
    def __enter__(self): self.con.__enter__(); return self
    def __exit__(self,*args): return self.con.__exit__(*args)
    def execute(self,sql,parameters=()):
        normalized=' '.join(sql.split())
        if normalized=='INSERT INTO inputs VALUES (?,?,?)':
            if self.archive is None: raise sqlite3.OperationalError('Read-only shared input connection')
            # Archive fsync/commit happens before the ledger can commit a reference.
            # An aborted ledger step may leave an unused payload, never a dangling ref.
            self.archive.put(*parameters)
            return self.con.execute('INSERT INTO input_refs VALUES (?,?)',parameters[:2])
        if re.match(r'^SELECT\b',normalized,re.I):
            sql=re.sub(r'\bFROM\s+inputs\b','FROM recorded_inputs',sql,flags=re.I)
        return self.con.execute(sql,parameters)


def attach(runner,archive):
    con=runner.db
    if not marked(con):
        with con:
            con.execute('CREATE TABLE IF NOT EXISTS input_refs(id TEXT PRIMARY KEY,at REAL)')
            con.execute('CREATE TABLE input_storage(key TEXT PRIMARY KEY,value TEXT)')
            con.execute('INSERT INTO input_storage VALUES (?,?)',('schema',SCHEMA))
    install_reader(con,archive.path)
    runner.db=Connection(con,archive)


def read_connection(con,path):
    if marked(con):
        install_reader(con,Path(path).parent/ARCHIVE)
        return Connection(con)
    return con


def activate(brain):
    flag=brain.root/'data/input-storage.json'
    migrated=any(marked(item['runner'].db) for item in [brain.primary_strategy]+brain.experiment_runners)
    if not flag.exists() and not migrated:
        return None
    if flag.exists() and (flag.is_symlink() or flag.stat().st_size>4096 or json.loads(flag.read_text()).get('schema')!=SCHEMA):
        raise ValueError('Invalid shared input activation metadata')
    if migrated and not (brain.root/'data'/ARCHIVE).is_file(): raise ValueError('Shared input archive missing')
    from .research_runner import ResearchRunner
    archive=Archive(brain.root/'data'/ARCHIVE)
    try:
        for item in [brain.primary_strategy]+brain.experiment_runners:
            runner=item['runner']
            if not isinstance(runner,ResearchRunner): attach(runner,archive)
        return archive
    except BaseException: archive.close(); raise
