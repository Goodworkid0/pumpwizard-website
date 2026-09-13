import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from scripts.backup_lab import backup, restore, verify
from lab_backup.archive import prepare
from lab_backup.common import save, BackupError
from lab_backup.retention import prune
from scripts.check_lab import retained_backup_age


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name); self.app=self.root/'app'; (self.app/'data').mkdir(parents=True)
        with sqlite3.connect(self.app/'data/test.sqlite') as con:
            con.execute('CREATE TABLE records(id INTEGER PRIMARY KEY,value TEXT)')
            con.execute("INSERT INTO records VALUES(1,'preserved')")
        con.close()
        self.store=self.root/'backups'; (self.store/'snapshots').mkdir(parents=True); (self.store/'jobs').mkdir()
        self.config={'backup_root':str(self.store),'local_retention':'latest_verified'}
        self.job,self.receipt=self.completed('first')
        self.recovered=self.store/'production-restore-first'
        restore(self.store/'snapshots/first',self.recovered)
        (self.recovered/'download.tar.gz').write_bytes((self.job/'archive.tar.gz').read_bytes())
        save(self.recovered/'telegram-restore.json',{'id':self.receipt['id'],'archive_sha256':self.receipt['archive_sha256']})
        save(self.store/'production-drill.json',{'status':'verified','id':self.receipt['id'],'key_id':'test',
            'destination':str(self.recovered)})

    def completed(self,name):
        source=self.store/'snapshots'/name; backup(self.app,source)
        job=self.store/'jobs'/name; state=prepare(source,job)
        receipt={**state,'schema':'pumpwizard.telegram-receipt/v1','status':'verified','purpose':'production',
                 'key_id':'test','parts':[{'index':0,'offset':0,'bytes':state['archive_bytes'],'message_id':1}]}
        save(job/'telegram-receipt.json',receipt); state['status']='verified'; save(job/'job.json',state)
        return job,receipt

    def test_retains_verified_copy_receipts_and_live_data(self):
        self.assertGreater(prune(self.config)['candidate_bytes'],0)
        self.assertTrue((self.job/'archive.tar.gz').exists())
        result=prune(self.config,apply=True)
        self.assertGreater(result['removed_bytes'],0); verify(self.recovered)
        self.assertFalse((self.recovered/'download.tar.gz').exists())
        self.assertFalse((self.job/'archive.tar.gz').exists())
        self.assertTrue((self.job/'telegram-receipt.json').exists())
        self.assertTrue((self.store/'snapshots/first/retired-manifest.json').exists())
        self.assertTrue((self.app/'data/test.sqlite').exists())
        self.assertEqual(prune(self.config,apply=True)['removed_bytes'],0)
        self.assertEqual(retained_backup_age(self.store,self.receipt['snapshot_completed_at']+10),10)

    def test_next_verified_snapshot_replaces_only_old_local_copy(self):
        prune(self.config,apply=True)
        job,receipt=self.completed('second')
        result=prune(self.config,apply=True)
        self.assertEqual(result['kept_copy'],str(self.store/'snapshots/second'))
        verify(self.store/'snapshots/second')
        self.assertTrue((self.recovered/'retired-manifest.json').exists())
        self.assertTrue((self.job/'telegram-receipt.json').exists())
        self.assertTrue((job/'telegram-receipt.json').exists())
        self.assertEqual(retained_backup_age(self.store,receipt['snapshot_completed_at']+20),20)

    def test_no_cleanup_without_recovery_or_opt_in(self):
        self.assertEqual(prune({**self.config,'local_retention':None},apply=True)['status'],'disabled')
        (self.store/'production-drill.json').unlink()
        self.assertEqual(prune(self.config,apply=True)['status'],'awaiting_verified_recovery')
        self.assertTrue((self.job/'archive.tar.gz').exists())

    def test_corrupt_kept_copy_blocks_all_cleanup(self):
        (self.recovered/'data/test.sqlite').write_bytes(b'corrupt')
        with self.assertRaises(ValueError): prune(self.config,apply=True)
        self.assertTrue((self.job/'archive.tar.gz').exists())

    def test_changed_candidate_and_symlink_are_preserved(self):
        archive=self.job/'archive.tar.gz'; archive.write_bytes(b'changed')
        with self.assertRaises(BackupError): prune(self.config,apply=True)
        self.assertTrue((self.recovered/'download.tar.gz').exists())
        archive.unlink(); archive.symlink_to(self.app/'data/test.sqlite')
        with self.assertRaises(BackupError): prune(self.config,apply=True)
        self.assertTrue((self.app/'data/test.sqlite').exists())
