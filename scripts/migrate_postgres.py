"""Lossless, staged SQLite -> PostgreSQL import. Never changes the active backend.

Uses installed psql, not an ORM. Source reads are read-only transactions. Import
requires --apply; use a stopped/frozen source set for a coordinated cutover.
"""
import argparse
from contextlib import closing
import base64
import csv
import hashlib
import io
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys

NAME = re.compile(r'[a-z][a-z0-9_]{0,47}\Z')
csv.field_size_limit(100_000_000)


def canonical(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(',', ':'), allow_nan=False)


def value_json(value):
    return {'$sqlite_blob_base64':base64.b64encode(value).decode()} if isinstance(value, bytes) else value


def quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def copy_field(value):
    if value is None: return r'\N'
    return str(value).replace('\\', r'\\').replace('\t', r'\t').replace('\n', r'\n').replace('\r', r'\r')


def rows(con, table):
    info = con.execute('PRAGMA table_info('+quote_identifier(table)+')').fetchall()
    pk = [row['name'] for row in sorted(info, key=lambda row:row['pk']) if row['pk']]
    order = ','.join(quote_identifier(name) for name in pk) if pk else 'rowid'
    for row in con.execute('SELECT * FROM '+quote_identifier(table)+' ORDER BY '+order):
        yield canonical({key:value_json(row[key]) for key in row.keys()})


def inspect(con):
    if con.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise ValueError('Source integrity check failed')
    tables = []
    for row in con.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall():
        digest = hashlib.sha256(); count = 0
        for payload in rows(con, row['name']):
            digest.update((payload+'\n').encode()); count += 1
        tables.append({'name':row['name'], 'sqlite_schema':row['sql'], 'rows':count, 'sha256':digest.hexdigest()})
    return {'tables':tables, 'sha256':hashlib.sha256(canonical(tables).encode()).hexdigest(), 'rows':sum(t['rows'] for t in tables)}


class Postgres:
    def __init__(self, schema, connection_env=None):
        if not NAME.fullmatch(schema) or schema in ('public','pg_catalog','information_schema') or schema.startswith('pg_'):
            raise ValueError('Use a dedicated lowercase staging schema, not a system/public schema')
        self.schema = schema
        self.env = dict(os.environ)
        if connection_env:
            allowed = {'PGHOST','PGPORT','PGUSER','PGDATABASE','PGPASSWORD','PGSERVICE','PGSERVICEFILE','PGSSLMODE','PGPASSFILE'}
            if not isinstance(connection_env,dict) or set(connection_env)-allowed: raise ValueError('Invalid PostgreSQL environment')
            self.env.update(connection_env)  # Never put connection secrets in argv/log output.
        self.command = ['psql','-X','-q','-v','ON_ERROR_STOP=1']

    def execute(self, sql):
        result = subprocess.run(self.command, input='SET standard_conforming_strings=on;\n'+sql, text=True, env=self.env, capture_output=True)
        if result.returncode: raise RuntimeError('PostgreSQL command failed; no credentials or SQL payload are logged')

    def query(self, sql):
        result = subprocess.run(self.command+['--csv','-c',sql], text=True, env=self.env, capture_output=True)
        if result.returncode: raise RuntimeError('PostgreSQL query failed')
        return list(csv.DictReader(io.StringIO(result.stdout)))

    def initialize(self):
        s = self.schema
        self.execute(f'''
          BEGIN;
          CREATE SCHEMA IF NOT EXISTS {s};
          CREATE TABLE IF NOT EXISTS {s}.batches(batch_id text PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT now(),
            format text NOT NULL CHECK(format='pumpwizard.sqlite-staging/v1'));
          CREATE TABLE IF NOT EXISTS {s}.sources(batch_id text REFERENCES {s}.batches, source_id text,
            source_filename text NOT NULL, row_count bigint NOT NULL, sha256 text NOT NULL,
            PRIMARY KEY(batch_id,source_id));
          CREATE TABLE IF NOT EXISTS {s}.source_tables(batch_id text, source_id text, table_name text,
            sqlite_schema text NOT NULL, row_count bigint NOT NULL, sha256 text NOT NULL,
            PRIMARY KEY(batch_id,source_id,table_name), FOREIGN KEY(batch_id,source_id) REFERENCES {s}.sources);
          CREATE TABLE IF NOT EXISTS {s}.source_rows(batch_id text, source_id text, table_name text, ordinal bigint,
            payload text NOT NULL CHECK(jsonb_typeof(payload::jsonb)='object'), sha256 text NOT NULL,
            PRIMARY KEY(batch_id,source_id,table_name,ordinal),
            FOREIGN KEY(batch_id,source_id,table_name) REFERENCES {s}.source_tables);
          CREATE OR REPLACE VIEW {s}.instance_state AS
            SELECT batch_id,source_id, (payload::jsonb->>'value')::jsonb AS state
            FROM {s}.source_rows WHERE table_name='meta' AND payload::jsonb->>'key'='state'
              AND source_id LIKE 'long-hold-%';
          CREATE OR REPLACE VIEW {s}.instance_contracts AS
            SELECT batch_id,source_id, (payload::jsonb->>'value')::jsonb AS contract
            FROM {s}.source_rows WHERE table_name='meta' AND payload::jsonb->>'key'='contract'
              AND source_id LIKE 'long-hold-%';
          CREATE OR REPLACE VIEW {s}.closed_positions AS
            SELECT batch_id,source_id,(payload::jsonb->>'id')::bigint AS trade_id,
              (payload::jsonb->>'data')::jsonb AS trade
            FROM {s}.source_rows WHERE table_name='trades' AND source_id LIKE 'long-hold-%';
          CREATE OR REPLACE VIEW {s}.equity_marks AS
            SELECT batch_id,source_id,(payload::jsonb->>'id')::bigint AS mark_id,
              (payload::jsonb->>'at')::double precision AS at,
              (payload::jsonb->>'value')::double precision AS value,
              (payload::jsonb->>'cash')::double precision AS cash
            FROM {s}.source_rows WHERE table_name='equity' AND source_id LIKE 'long-hold-%';
          COMMIT;
        ''')

    def verify(self, batch, source_id, metadata):
        s=self.schema; where=f'batch_id={literal(batch)} AND source_id={literal(source_id)}'
        found=self.query(f'SELECT row_count,sha256 FROM {s}.sources WHERE {where}')
        if not found: return False
        if found[0]['sha256'] != metadata['sha256'] or int(found[0]['row_count']) != metadata['rows']:
            raise ValueError('Batch/source already exists with different content; choose a new batch ID')
        stored=self.query(f'SELECT table_name,sqlite_schema,row_count,sha256 FROM {s}.source_tables WHERE {where} ORDER BY table_name')
        expected=[{'table_name':t['name'],'sqlite_schema':t['sqlite_schema'],'row_count':str(t['rows']),'sha256':t['sha256']} for t in metadata['tables']]
        if stored != expected: raise ValueError('PostgreSQL source-table metadata mismatch')
        # Stream each stored row to verify full ordered table hashes, not just counts.
        for table in metadata['tables']:
            sql=f'SELECT payload FROM {s}.source_rows WHERE {where} AND table_name={literal(table["name"])} ORDER BY ordinal'
            with subprocess.Popen(self.command+['--csv','-c',sql], env=self.env, text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL) as process:
                digest=hashlib.sha256(); count=0
                for row in csv.DictReader(process.stdout):
                    digest.update((row['payload']+'\n').encode()); count+=1
                if process.wait(): raise RuntimeError('PostgreSQL verification failed')
            if count != table['rows'] or digest.hexdigest() != table['sha256']:
                raise ValueError('PostgreSQL row-count/hash mismatch for '+table['name'])
        counts=self.query(f'SELECT count(*) AS n FROM {s}.source_rows WHERE {where}')
        if int(counts[0]['n']) != metadata['rows']: raise ValueError('Unexpected extra destination rows')
        return True

    def import_source(self, batch, path, con, metadata):
        s=self.schema; source_id=path.stem
        if not re.fullmatch(r'[a-zA-Z0-9_-]{1,160}',source_id) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',batch):
            raise ValueError('Invalid source or batch identifier')
        if self.verify(batch,source_id,metadata): return 'already_verified'
        # psql stops on the first error; closing its connection rolls back the source transaction.
        with subprocess.Popen(self.command, env=self.env, text=True, stdin=subprocess.PIPE,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) as process:
            try:
                def write(text): process.stdin.write(text+'\n')
                write('SET standard_conforming_strings=on; BEGIN; SET LOCAL statement_timeout = \'10min\';')
                write(f"INSERT INTO {s}.batches(batch_id,format) VALUES ({literal(batch)},'pumpwizard.sqlite-staging/v1') ON CONFLICT DO NOTHING;")
                write(f'INSERT INTO {s}.sources VALUES ({literal(batch)},{literal(source_id)},{literal(path.name)},{metadata["rows"]},{literal(metadata["sha256"])});')
                for table in metadata['tables']:
                    write(f'INSERT INTO {s}.source_tables VALUES ({literal(batch)},{literal(source_id)},{literal(table["name"])},{literal(table["sqlite_schema"])},{table["rows"]},{literal(table["sha256"])});')
                    write(f'COPY {s}.source_rows (batch_id,source_id,table_name,ordinal,payload,sha256) FROM STDIN;')
                    for ordinal,payload in enumerate(rows(con,table['name']),1):
                        write('\t'.join(copy_field(value) for value in (batch,source_id,table['name'],ordinal,payload,hashlib.sha256(payload.encode()).hexdigest())))
                    write(r'\.')
                where=f'batch_id={literal(batch)} AND source_id={literal(source_id)}'
                write(f'''DO $$ BEGIN
                  IF (SELECT count(*) FROM {s}.source_rows WHERE {where}) <> {metadata['rows']} OR
                     EXISTS(SELECT 1 FROM {s}.source_rows WHERE {where}
                       AND sha256 <> encode(sha256(convert_to(payload,'UTF8')),'hex'))
                  THEN RAISE EXCEPTION 'Staging verification failed'; END IF;
                END $$; COMMIT;''')
                process.stdin.close()
                if process.wait(): raise RuntimeError('PostgreSQL import failed and the source transaction was rolled back')
            except BaseException:
                if not process.stdin.closed: process.stdin.close()
                process.wait()
                raise
        if not self.verify(batch,source_id,metadata): raise ValueError('Missing imported source')
        return 'imported_and_verified'


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('plan','import','verify'))
    parser.add_argument('--source-dir',type=Path,required=True,help='Explicit frozen SQLite source directory; only top-level *.sqlite files are read')
    parser.add_argument('--batch',default='rehearsal',help='Immutable import batch ID; use a new ID for each snapshot')
    parser.add_argument('--schema',default='pumpwizard_staging')
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--frozen-source',action='store_true',help='Confirm source files are an operator-prepared frozen set, not live collector databases')
    args=parser.parse_args(argv)
    if args.command=='import' and (not args.apply or not args.frozen_source): parser.error('Import requires --apply --frozen-source; do not target live ledgers')
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,80}',args.batch): parser.error('Invalid batch ID')
    source=args.source_dir.resolve()
    files=sorted(source.glob('*.sqlite'))
    if not files: parser.error('No SQLite files found')
    if any(path.is_symlink() for path in files): parser.error('Source files must not be symlinks')
    pg=None
    if args.command!='plan':
        pg=Postgres(args.schema)
        if args.command=='import': pg.initialize()
    for path in files:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as con:
            con.row_factory=sqlite3.Row; con.execute('PRAGMA query_only=ON'); con.execute('BEGIN')
            metadata=inspect(con)
            status='planned'
            if args.command=='import': status=pg.import_source(args.batch,path,con,metadata)
            elif args.command=='verify':
                if not pg.verify(args.batch,path.stem,metadata): raise ValueError('Source not found in destination')
                status='verified'
            print(json.dumps({'source':path.name,'rows':metadata['rows'],'tables':len(metadata['tables']),
                              'sha256':metadata['sha256'],'status':status}),flush=True)
    print('Active SQLite backend was not changed. Staging is not a live PostgreSQL executor.')


if __name__=='__main__':
    try: main()
    except (ValueError,RuntimeError,sqlite3.Error,OSError) as exc:
        print(type(exc).__name__+': migration stopped; check source integrity, batch identity and PostgreSQL access.',file=sys.stderr)
        sys.exit(1)
