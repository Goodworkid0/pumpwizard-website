import tempfile
from pathlib import Path
import sqlite3
import json
import unittest
from test_replay_package import fixture
from scripts.backup_lab import backup
from scripts.reconcile_restore import reconcile


class ReconciliationTests(unittest.TestCase):
    def test_frozen_accounting_and_reference_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); app=root/'app'; data=app/'data'; data.mkdir(parents=True)
            fixture(data)
            (data/'ledger.sqlite').rename(data/'long-hold-test.sqlite')
            (data/'shared.sqlite').rename(data/'shared-research.sqlite')
            backup(app,root/'good',frozen=True)
            self.assertEqual(reconcile(root/'good')['instances'],1)
            with sqlite3.connect(data/'long-hold-test.sqlite') as con:
                state=json.loads(con.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0])
                state['cash_usd']+=1
                con.execute("UPDATE meta SET value=? WHERE key='state'",(json.dumps(state),))
            con.close()
            backup(app,root/'bad',frozen=True)
            with self.assertRaisesRegex(ValueError,'Accounting'): reconcile(root/'bad')
