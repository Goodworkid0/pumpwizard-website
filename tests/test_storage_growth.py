from collections import namedtuple
from pathlib import Path
import tempfile
import unittest
from lab_backup.common import save
from scripts.storage_growth import record,summarize


class StorageGrowthTests(unittest.TestCase):
    def test_short_windows_do_not_project_and_drops_do_not_mean_negative_growth(self):
        Usage=namedtuple('Usage','total used free');usage=Usage(100*1024**3,0,50*1024**3)
        samples=[{'at':0,'sqlite_bytes':1000,'wal_bytes':0},{'at':120,'sqlite_bytes':2000,'wal_bytes':200}]
        self.assertIsNone(summarize(samples,usage)['estimated_bytes_per_day'])
        samples[-1]['at']=3600
        self.assertEqual(summarize(samples,usage)['estimated_bytes_per_day'],24000)
        samples[-1]['sqlite_bytes']=500
        self.assertIsNone(summarize(samples,usage)['estimated_days_to_disk_reserve'])

    def test_migration_generation_resets_old_size_comparison(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);(root/'ledger.sqlite').write_bytes(b'x'*100)
            record(root,1000);record(root,4000)
            save(root/'input-storage.json',{'activated_at':4100})
            result=record(root,4200)
            self.assertEqual(result['observed_seconds'],0)
            self.assertIsNone(result['estimated_bytes_per_day'])
