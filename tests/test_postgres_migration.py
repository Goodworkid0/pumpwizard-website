import copy
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scripts.migrate_postgres import inspect, rows, copy_field, Postgres


class MigrationUnitTests(unittest.TestCase):
    def test_lossless_rows_and_deterministic_composite_keys(self):
        with sqlite3.connect(':memory:') as con:
            con.row_factory=sqlite3.Row
            con.execute('CREATE TABLE records(a text,b integer,payload blob,PRIMARY KEY(a,b)) WITHOUT ROWID')
            con.execute('INSERT INTO records VALUES (?,?,?)',('quote\n\t\\N',2,b'\x00\xff'))
            first=inspect(con); second=inspect(con)
            self.assertEqual(first,second)
            payload=json.loads(next(rows(con,'records')))
            self.assertEqual(payload['payload'],{'$sqlite_blob_base64':'AP8='})
            self.assertEqual(payload['a'],'quote\n\t\\N')
            con.execute('INSERT INTO records VALUES (?,?,?)',('another',1,b''))
            self.assertNotEqual(first['sha256'],inspect(con)['sha256'])

    def test_copy_escapes_and_schema_is_constrained(self):
        self.assertEqual(copy_field(None),r'\N')
        self.assertEqual(copy_field('a\nb\tc\\d'),r'a\nb\tc\\d')
        for schema in ('public','pg_catalog','pg_other','bad;drop schema public','UPPER'):
            with self.assertRaises(ValueError):Postgres(schema)


if __name__=='__main__':unittest.main()
