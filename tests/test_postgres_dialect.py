import unittest
from paper.postgres_runtime import translate, Row


class DialectTests(unittest.TestCase):
    def test_atomic_ids_and_upserts(self):
        self.assertIn('coalesce(max(id),0)+1',translate('INSERT INTO events(at,mint,action,data) VALUES (?,?,?,?)'))
        self.assertIn('ON CONFLICT (key) DO UPDATE',translate('INSERT OR REPLACE INTO meta VALUES (?,?)'))
        self.assertIn('ON CONFLICT DO NOTHING',translate('INSERT OR IGNORE INTO inputs VALUES (?,?,?)'))
        self.assertIn('trunc(at / %s)::bigint',translate('SELECT cast(at / ? as integer) FROM equity'))
        for sql in ('DELETE FROM inputs','DROP TABLE events','INSERT OR REPLACE INTO trades VALUES (?,?)','SELECT 1; DELETE FROM trades'):
            with self.assertRaises(ValueError): translate(sql)

    def test_row_protocol(self):
        row=Row(['id','data'],[1,'{}'])
        self.assertEqual(row[0],row['id'])
        self.assertEqual(dict(row),{'id':1,'data':'{}'})
        self.assertEqual(tuple(row),(1,'{}'))
