import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from paper.shared_inputs import Archive, read_connection
from paper.replay_package import create,verify
from scripts.compact_shared_inputs import Importer, convert, proof, compact
from scripts.finalize_input_compaction import finalize
from test_replay_package import fixture
from paper.ladder import LadderRunner


class CompactInputsTests(unittest.TestCase):
    def test_lossless_conversion_unknown_tables_and_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fixture(root,LadderRunner)
            original=root/'ledger.sqlite'; destination=root/'compact.sqlite'
            con=sqlite3.connect(original)
            con.executescript('CREATE TABLE extra(id INTEGER PRIMARY KEY AUTOINCREMENT,payload BLOB); CREATE INDEX extra_payload ON extra(payload);')
            con.execute('INSERT INTO extra VALUES (?,?)',(5,b'unchanged'))
            con.execute('INSERT INTO extra VALUES (?,?)',(9,b'deleted'));con.execute('DELETE FROM extra WHERE id=9');con.commit();con.close()
            archive=Archive(root/'shared-research.sqlite')
            try: result=convert(original,destination,Importer(archive))
            finally:archive.close()
            self.assertEqual(result['input_rows_converted'],18)
            a=sqlite3.connect(original);b=sqlite3.connect(destination)
            try:
                self.assertEqual(proof(a,'extra'),proof(b,'extra'))
                self.assertEqual(a.execute('SELECT * FROM sqlite_sequence').fetchall(),b.execute('SELECT * FROM sqlite_sequence').fetchall())
                self.assertEqual(b.execute('SELECT count(*) FROM inputs').fetchone()[0],0)
                self.assertEqual(b.execute('SELECT count(*) FROM input_refs').fetchone()[0],18)
                reader=read_connection(b,destination)
                self.assertEqual(reader.execute('SELECT id,at,payload FROM inputs ORDER BY at').fetchall(),a.execute('SELECT id,at,payload FROM inputs ORDER BY at').fetchall())
                reader.execute('PRAGMA query_only=ON')
                with self.assertRaises(sqlite3.OperationalError):reader.execute('INSERT INTO meta VALUES (?,?)',('bad','bad'))
            finally:a.close();b.close()
            result=create(destination,root/'shared-research.sqlite',root/'package',frozen_source=True)
            self.assertEqual(result,verify(root/'package'))

    def test_corrupt_payload_refuses_conversion_and_leaves_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fixture(root,LadderRunner)
            con=sqlite3.connect(root/'ledger.sqlite');con.execute("UPDATE inputs SET payload=x'0000' WHERE rowid=1");con.commit();con.close()
            archive=Archive(root/'shared.sqlite')
            try:
                with self.assertRaises(Exception):convert(root/'ledger.sqlite',root/'compact.sqlite',Importer(archive))
                self.assertTrue((root/'ledger.sqlite').is_file())
            finally:archive.close()

    def test_frozen_switch_preserves_original_and_records_hashes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);fixture(root,LadderRunner)
            (root/'ledger.sqlite').rename(root/'long-hold-test.sqlite')
            (root/'shared.sqlite').rename(root/'shared-research.sqlite')
            result=compact(root,root/'migration')
            self.assertEqual(result['phase'],'switched')
            manifest=json.loads((root/'migration/migration.json').read_text())
            self.assertEqual(manifest['switched'],['long-hold-test.sqlite'])
            self.assertTrue((root/'migration/originals/long-hold-test.sqlite').is_file())
            con=sqlite3.connect(root/'long-hold-test.sqlite')
            self.assertEqual(con.execute('SELECT count(*) FROM inputs').fetchone()[0],0);con.close()
            self.assertEqual(finalize(root,root/'migration')['files'],1)
            self.assertTrue((root/'migration/originals/long-hold-test.sqlite').exists())
            reclaimed=finalize(root,root/'migration',apply=True)
            self.assertGreater(reclaimed['removed_bytes'],0)
            self.assertFalse((root/'migration/originals/long-hold-test.sqlite').exists())
            self.assertTrue((root/'long-hold-test.sqlite').is_file())
