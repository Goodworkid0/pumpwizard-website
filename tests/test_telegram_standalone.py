import asyncio
import hashlib
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest

from scripts.backup_lab import backup, verify
from scripts.check_lab import telegram_backup_age
from scripts.telegram_backup import first_restore_drill
from lab_backup.archive import prepare
from lab_backup.common import BackupError, load, save
from lab_backup.crypto import BLOCK, encrypt, decrypt
from lab_backup.store import upload, restore, recover_manifest
from lab_backup.transport import Transport


class FakeTelegram:
    channel_id=-100123456
    def __init__(self):
        self.messages={}; self.captions={}; self.fail_upload=False; self.fail_send=False; self.corrupt=False
    async def preflight(self): return {'private_channel':True,'auto_delete_off':True}
    async def upload(self,path,caption,before_send=None):
        if self.fail_upload:
            self.fail_upload=False; raise BackupError('synthetic upload failure')
        if before_send: before_send()
        mid=len(self.messages)+1; self.messages[mid]=Path(path).read_bytes(); self.captions[mid]=caption
        if self.fail_send: self.fail_send=False; raise BackupError('synthetic lost acknowledgement')
        return mid
    async def download(self,mid,path,size):
        data=self.messages[mid]
        if self.corrupt: data=data[:-1]+bytes([data[-1]^1])
        if len(data)!=size: raise BackupError('size mismatch')
        Path(path).write_bytes(data)
    async def manifest_info(self,mid): return self.captions[mid]


class StandaloneBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); lab=self.root/'lab'; (lab/'data').mkdir(parents=True)
        con=sqlite3.connect(lab/'data/ledger.sqlite')
        con.execute('CREATE TABLE evidence(id INTEGER PRIMARY KEY,payload BLOB)')
        con.execute('INSERT INTO evidence VALUES(1,?)',(bytes(range(256))*1000,)); con.commit(); con.close()
        (lab/'brain.config.json').write_text('{"test":true}')
        self.snapshot=self.root/'snapshot'; backup(lab,self.snapshot)
        self.job=self.root/'job'; prepare(self.snapshot,self.job)
        self.transport=FakeTelegram(); self.key=b'k'*32
    def run_async(self,awaitable): return asyncio.run(awaitable)

    def test_monitor_uses_production_snapshot_age_not_smoke_or_upload_time(self):
        receipt=self.run_async(upload(self.job,self.transport,self.key,1024))
        receipt.update(snapshot_completed_at=1000,verified_at=99000,purpose='production')
        save(self.job/'telegram-receipt.json',receipt)
        self.assertEqual(telegram_backup_age(self.root,100000),99000)
        receipt['purpose']='smoke'; save(self.job/'telegram-receipt.json',receipt)
        self.assertIsNone(telegram_backup_age(self.root,100000))
        receipt.update(purpose='production',snapshot_completed_at=100001)
        save(self.job/'telegram-receipt.json',receipt)
        self.assertIsNone(telegram_backup_age(self.root,100000))

    def test_multipart_roundtrip_and_channel_only_manifest_recovery(self):
        receipt=self.run_async(upload(self.job,self.transport,self.key,1024))
        self.assertGreater(len(receipt['parts']),1)
        self.assertEqual(receipt['status'],'verified')
        self.assertTrue(all(c.get('pwbackup')==1 and 'ots' not in c for c in self.transport.captions.values()))
        self.assertFalse(list(self.job.glob('*.pwb')))
        destination=self.root/'restore'
        self.run_async(restore(receipt,self.transport,self.key,destination))
        self.assertEqual(verify(destination),verify(self.snapshot))
        recovered=self.run_async(recover_manifest(self.transport,self.key,receipt['manifest_message_id'],self.root/'recovery'))
        self.assertEqual(recovered['archive_sha256'],receipt['archive_sha256'])
        with self.assertRaises(ValueError): self.run_async(restore(receipt,self.transport,self.key,destination))

    def test_first_production_drill_is_recorded_once_without_overwriting(self):
        receipt=self.run_async(upload(self.job,self.transport,self.key,1024))
        config={'backup_root':str(self.root),'session':str(self.root/'session.json'),'notify':False}
        self.run_async(first_restore_drill(config,self.transport,self.key,receipt))
        record=load(self.root/'production-drill.json')
        self.assertEqual(record['status'],'verified')
        self.assertEqual(verify(Path(record['destination'])),verify(self.snapshot))
        self.run_async(first_restore_drill(config,self.transport,self.key,receipt))
        self.assertEqual(load(self.root/'production-drill.json'),record)
        record['status']='incomplete'; save(self.root/'production-drill.json',record)
        with self.assertRaises(BackupError): self.run_async(first_restore_drill(config,self.transport,self.key,receipt))

    def test_upload_failure_resumes_without_new_message_for_acknowledged_parts(self):
        self.transport.fail_upload=True
        with self.assertRaises(BackupError): self.run_async(upload(self.job,self.transport,self.key,1024))
        self.assertFalse((self.job/'telegram-receipt.json').exists())
        self.assertFalse(load(self.job/'job.json')['parts'][0].get('sending'))
        receipt=self.run_async(upload(self.job,self.transport,self.key,1024))
        count=len(self.transport.messages)
        self.run_async(upload(self.job,self.transport,self.key,1024))
        self.assertEqual(len(self.transport.messages),count)
        self.assertEqual(count,len(receipt['parts'])+1)

    def test_lost_acknowledgement_never_silently_duplicates(self):
        self.transport.fail_send=True
        with self.assertRaises(BackupError): self.run_async(upload(self.job,self.transport,self.key,1024))
        self.assertEqual(len(self.transport.messages),1)
        with self.assertRaisesRegex(BackupError,'uncertain'): self.run_async(upload(self.job,self.transport,self.key,1024))
        self.assertEqual(len(self.transport.messages),1)

    def test_corruption_or_wrong_key_never_creates_success(self):
        self.transport.corrupt=True
        with self.assertRaises(BackupError): self.run_async(upload(self.job,self.transport,self.key,1024))
        self.assertFalse((self.job/'telegram-receipt.json').exists())
        self.transport.corrupt=False
        with self.assertRaises(BackupError): self.run_async(upload(self.job,self.transport,b'x'*32,1024))

    def test_changed_archive_and_unsafe_restore_rejected(self):
        receipt=self.run_async(upload(self.job,self.transport,self.key,1024))
        receipt['members']['../escape']={'bytes':1,'sha256':'0'*64}
        with self.assertRaises(BackupError): self.run_async(restore(receipt,self.transport,self.key,self.root/'bad'))
        self.assertFalse((self.root/'bad').exists())
        (self.job/'archive.tar.gz').write_bytes(b'changed')
        with self.assertRaises(BackupError): self.run_async(upload(self.job,self.transport,self.key,1024))

    def test_aead_empty_multiblock_context_truncation_and_trailer(self):
        for size in (0,1,BLOCK+7):
            source=self.root/f'source-{size}'; source.write_bytes(b'z'*size)
            encrypted=self.root/f'cipher-{size}'; expected=encrypt(source,encrypted,self.key,'run/part')
            target=self.root/f'decoded-{size}'; decrypt(encrypted,target,self.key,'run/part',expected)
            self.assertEqual(target.read_bytes(),source.read_bytes())
            with self.assertRaises(BackupError): decrypt(encrypted,self.root/f'wrong-{size}',self.key,'other/part',expected)
            original=encrypted.read_bytes(); encrypted.write_bytes(original[:-1])
            with self.assertRaises(BackupError): decrypt(encrypted,self.root/f'truncated-{size}',self.key,'run/part',expected)
            encrypted.write_bytes(original+b'x')
            with self.assertRaises(BackupError): decrypt(encrypted,self.root/f'trailer-{size}',self.key,'run/part',expected)

    def test_live_channel_preflight_requires_private_channel_and_off_ttl(self):
        transport=Transport.__new__(Transport); transport.channel_id=-1001
        transport.bot_api=lambda *_:{'id':-1001,'type':'channel'}
        self.assertTrue(self.run_async(transport.preflight())['auto_delete_off'])
        for data in ({'id':-1001,'type':'channel','username':'public'},
                     {'id':-1001,'type':'channel','message_auto_delete_time':86400},
                     {'id':-1002,'type':'channel'}, {'id':-1001,'type':'group'}):
            transport.bot_api=lambda *_,d=data:d
            with self.assertRaises(BackupError): self.run_async(transport.preflight())


if __name__=='__main__': unittest.main()
