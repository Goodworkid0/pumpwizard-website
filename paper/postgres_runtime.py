"""Opt-in PostgreSQL ledger backend; unchanged simulation implementations.

One bounded pool, transactional IDs and state/event/input commits. SQLite remains
the default. Enable only against an explicitly imported and reconciled namespace.
"""
import copy
import hashlib
import json
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
from contextlib import contextmanager

from .engine import canonical

TABLES = {
    'meta': 'key TEXT PRIMARY KEY, value TEXT',
    'inputs': 'id TEXT PRIMARY KEY, at DOUBLE PRECISION, payload BYTEA',
    'input_refs': 'id TEXT PRIMARY KEY, at DOUBLE PRECISION',
    'contract_history': 'hash TEXT PRIMARY KEY, data TEXT',
    'events': 'id BIGINT PRIMARY KEY, at DOUBLE PRECISION, mint TEXT, action TEXT, data TEXT',
    'sales': 'id BIGINT PRIMARY KEY, mint TEXT, data TEXT',
    'trades': 'id BIGINT PRIMARY KEY, mint TEXT, data TEXT',
    'equity': 'id BIGINT PRIMARY KEY, at DOUBLE PRECISION, value DOUBLE PRECISION, cash DOUBLE PRECISION',
    'samples': 'mint TEXT, bucket BIGINT, at DOUBLE PRECISION, data TEXT, PRIMARY KEY(mint,bucket)',
}


class Row:
    def __init__(self, names, values): self.names, self.values = names, values
    def keys(self): return self.names
    def __getitem__(self, key): return self.values[self.names.index(key)] if isinstance(key,str) else self.values[key]
    def __iter__(self): return iter(self.values)


class Rows:
    def __init__(self, cursor):
        self.rows = [Row([c.name for c in cursor.description], row) for row in cursor.fetchall()] if cursor.description else []
        self.index = 0
    def fetchone(self):
        if self.index >= len(self.rows): return None
        row=self.rows[self.index]; self.index+=1; return row
    def fetchall(self): result=self.rows[self.index:]; self.index=len(self.rows); return result
    def __iter__(self): return iter(self.fetchall())


def translate(sql):
    """Only the ledger's small SQL dialect, not a general SQLite compatibility layer."""
    sql = ' '.join(sql.split())
    if ';' in sql or not re.match(r'^(SELECT|INSERT)\b',sql,re.I):
        raise ValueError('Unsupported runtime ledger statement')
    sql = re.sub(r"json_extract\(data,'\$\.pnl_usd'\)", "((data::jsonb->>'pnl_usd')::double precision)",sql)
    sql = sql.replace('max(0,','greatest(0,')
    sql = sql.replace("sum(action='fill_entry')", "sum((action='fill_entry')::int)")
    sql = sql.replace("sum(((data::jsonb->>'pnl_usd')::double precision)>0)",
                      "sum((((data::jsonb->>'pnl_usd')::double precision)>0)::int)")
    # SQLite truncates toward zero; PostgreSQL float->integer casts round.
    sql = re.sub(r'cast\(at / \? as integer\)', 'trunc(at / ?)::bigint', sql, flags=re.I)
    ignore = sql.startswith('INSERT OR IGNORE ')
    replace = sql.startswith('INSERT OR REPLACE ')
    sql = sql.replace('INSERT OR IGNORE ', 'INSERT ').replace('INSERT OR REPLACE ', 'INSERT ')
    if replace:
        if not re.fullmatch(r'INSERT INTO meta VALUES \(\?,\s*\?\)',sql): raise ValueError('Unsupported upsert')
        sql += ' ON CONFLICT (key) DO UPDATE SET value=EXCLUDED.value'
    if ignore: sql += ' ON CONFLICT DO NOTHING'
    auto = re.match(r'INSERT INTO (events|sales|trades|equity)\(([^)]+)\) VALUES \((.+)\)$',sql)
    if auto and 'id' not in [c.strip() for c in auto[2].split(',')]:
        table,columns,values=auto.groups()
        sql=f'INSERT INTO {table}(id,{columns}) VALUES ((SELECT coalesce(max(id),0)+1 FROM {table}),{values})'
    return sql.replace('?', '%s')


class Backend:
    def __init__(self, dsn, namespace, *, writer=True, size=8):
        import psycopg
        if not re.fullmatch('[a-z][a-z0-9_]{0,24}',namespace): raise ValueError('Invalid runtime namespace')
        self.dsn,self.namespace,self.writer = dsn,namespace,writer
        self.pool=queue.LifoQueue(maxsize=size)
        self.connections=[]; self.lock=threading.Lock(); self.size=size
        self.lease=psycopg.connect(dsn,autocommit=True)
        if writer and not self.lease.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))',('pumpwizard/'+namespace,)).fetchone()[0]:
            self.lease.close(); raise ValueError('PostgreSQL namespace already has a writer')

    def schema(self, name):
        if not re.fullmatch(r'[a-z0-9-]+\.sqlite',name): raise ValueError('Invalid ledger name')
        return self.namespace+'_'+hashlib.sha256(name.encode()).hexdigest()[:24]

    @contextmanager
    def connection(self):
        import psycopg
        try: con=self.pool.get_nowait()
        except queue.Empty:
            with self.lock:
                if len(self.connections)<self.size:
                    con=psycopg.connect(self.dsn,autocommit=True); self.connections.append(con)
                else: con=None
            if con is None: con=self.pool.get(timeout=20)
        try: yield con
        finally:
            if con.closed:
                with self.lock: self.connections.remove(con)
            else: self.pool.put(con)

    def database(self, name): return Database(self,name)

    def import_ledger(self, path, *, frozen=False, shared_name=None):
        from .replay_package import frozen as open_frozen
        if not self.writer or not frozen: raise ValueError('Frozen source and writer ownership required')
        path=Path(path); schema=self.schema(path.name)
        source=open_frozen(path)
        try:
            tables={r[0] for r in source.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if not tables <= TABLES.keys(): raise ValueError('Unsupported ledger tables; use lossless staging importer')
            with self.connection() as con, con.transaction():
                if con.execute('SELECT 1 FROM pg_namespace WHERE nspname=%s',(schema,)).fetchone():
                    raise ValueError('Runtime destination already exists; never overwrite it')
                con.execute('CREATE SCHEMA '+schema)
                con.execute('SET LOCAL search_path TO '+schema+',pg_catalog')
                for name in sorted(tables):
                    con.execute('CREATE TABLE '+name+' ('+TABLES[name]+')')
                    names=[r['name'] for r in source.execute('PRAGMA table_info('+name+')')]
                    with con.cursor().copy('COPY '+name+' ('+','.join(names)+') FROM STDIN') as target:
                        for row in source.execute('SELECT * FROM '+name): target.write_row(tuple(row))
                if 'input_refs' in tables and shared_name:
                    con.execute('ALTER TABLE input_refs ADD CONSTRAINT shared_input_exists FOREIGN KEY (id) REFERENCES '+self.schema(shared_name)+'.inputs(id)')
                if 'equity' in tables: con.execute('CREATE INDEX equity_at ON equity(at)')
                if 'events' in tables: con.execute('CREATE INDEX events_at ON events(at)')
                con.execute('CREATE TABLE runtime_identity(name TEXT PRIMARY KEY)')
                con.execute('INSERT INTO runtime_identity VALUES (%s)',(path.name,))
                # Verify every preserved row before the import transaction commits.
                for name in sorted(tables):
                    primary=[r['name'] for r in source.execute('PRAGMA table_info('+name+')') if r['pk']]
                    order=','.join(primary)
                    local=source.execute('SELECT * FROM '+name+' ORDER BY '+order)
                    with con.cursor(name='verify_'+name) as remote:
                        remote.itersize=64
                        remote.execute('SELECT * FROM '+name+' ORDER BY '+order)
                        for a,b in zip(local,remote,strict=True):
                            if tuple(a)!=tuple(b): raise ValueError('Runtime import row mismatch')
        finally: source.close()

    def attach(self, runner):
        database=self.database(runner.path.name)
        contract=database.execute("SELECT value FROM meta WHERE key='contract'").fetchone()
        if contract is None or json.loads(contract[0]) != runner.contract:
            raise ValueError('Runtime contract differs; do not reset or reinterpret the instance')
        row=database.execute("SELECT value FROM meta WHERE key='state'").fetchone()
        runner.db.close(); runner.db=database
        if row: runner.state=json.loads(row[0])
        database.runner=runner

    def close(self):
        for con in self.connections: con.close()
        self.lease.close()


class Database:
    def __init__(self, backend, name):
        self.backend,self.name,self.schema=backend,name,backend.schema(name)
        self.local=threading.local()
        self.deadline=None

    def __enter__(self):
        if getattr(self.local,'depth',0): self.local.depth+=1; return self
        self.local.lease=self.backend.connection(); con=self.local.lease.__enter__()
        self.local.con=con; self.local.transaction=con.transaction()
        entered=False
        try:
            self.local.transaction.__enter__(); entered=True
            con.execute('SET LOCAL search_path TO '+self.schema+',pg_catalog')
            milliseconds=20000 if self.deadline is None else max(1,min(20000,int((self.deadline-time.monotonic())*1000)))
            con.execute("SELECT set_config('statement_timeout',%s,true)",(str(milliseconds),))
            con.execute("SET LOCAL lock_timeout='5s'")
            if not self.backend.writer: con.execute('SET TRANSACTION READ ONLY')
            else:
                self.backend.lease.execute('SELECT 1')  # A lost writer lease must stop execution.
                con.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,1))',(self.schema,))
            identity=con.execute('SELECT name FROM runtime_identity').fetchone()
            if identity is None or identity[0]!=self.name: raise ValueError('Runtime ledger identity mismatch')
            if getattr(self,'runner',None) is not None:
                stored=con.execute("SELECT value FROM meta WHERE key='state'").fetchone()
                if stored is not None and json.loads(stored[0])!=self.runner.state:
                    raise ValueError('Runtime state advanced or drifted; restart from committed state')
            self.local.depth=1; return self
        except BaseException:
            import sys
            error=sys.exc_info()
            try:
                if entered: self.local.transaction.__exit__(*error)
            finally: self.local.lease.__exit__(*error)
            raise

    def __exit__(self, *error):
        self.local.depth-=1
        if self.local.depth: return False
        try: return self.local.transaction.__exit__(*error)
        finally: self.local.lease.__exit__(*error)

    def execute(self, sql, parameters=()):
        if sql == 'BEGIN':
            if self.backend.writer: raise ValueError('Explicit read transaction requires read-only backend')
            self.__enter__(); self.local.explicit=True
            return RowsEmpty()
        import psycopg
        try:
            if self.deadline is not None and time.monotonic()>self.deadline:
                raise sqlite3.OperationalError('Public query deadline exceeded')
            with self:
                return Rows(self.local.con.execute(translate(sql), parameters))
        except psycopg.Error:
            raise sqlite3.OperationalError('PostgreSQL ledger operation failed; transaction requires recovery') from None

    def close(self):
        if getattr(self.local,'explicit',False):
            self.local.explicit=False; self.__exit__(None,None,None)
    def set_progress_handler(self,*_): pass  # PostgreSQL statement_timeout is enforced server-side


class RowsEmpty:
    def fetchone(self): return None


def activate(brain, dsn, namespace):
    """Attach already imported ledgers before any collector thread starts."""
    backend=Backend(dsn,namespace)
    try:
        for item in [brain.primary_strategy]+brain.experiment_runners: backend.attach(item['runner'])
        if brain.shared:
            # Rebuild feature history from PostgreSQL committed samples, never a stale SQLite mirror.
            shared=brain.shared; database=backend.database('shared-research.sqlite')
            latest=database.execute('SELECT max(at) FROM samples').fetchone()[0]
            from collections import defaultdict
            from .experiment_catalog import WINDOWS
            shared.histories=defaultdict(list)
            if latest is not None:
                for row in database.execute('SELECT mint,data FROM samples WHERE at>=? ORDER BY at',(latest-max(WINDOWS)-180,)):
                    point=json.loads(row['data']); history=shared.histories[row['mint']]
                    if history and ((history[-1]['route'],history[-1]['pool'])!=(point['route'],point['pool']) or point['at']-history[-1]['at']>180): history.clear()
                    history.append(point)
            shared.db.close(); shared.db=database
        return backend
    except BaseException: backend.close(); raise


_read_backend=None
_read_lock=threading.Lock()


def runtime_readonly(path, deadline=None):
    """Activated only by explicit runtime environment; memory stays on SQLite."""
    import os
    if os.environ.get('PUMP_LEDGER_BACKEND','sqlite')!='postgres': return None
    path=Path(path)
    if not (path.name.startswith('long-hold-') or path.name=='shared-research.sqlite'): return None
    global _read_backend
    with _read_lock:
        if _read_backend is None:
            _read_backend=Backend(os.environ['PUMP_POSTGRES_DSN'],os.environ['PUMP_POSTGRES_NAMESPACE'],writer=False)
    database=_read_backend.database(path.name); database.deadline=deadline
    return database
