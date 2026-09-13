import hashlib
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch

from scripts.backup_lab import backup, verify
from scripts.check_lab import remote_backup_age
from scripts.ots_backup import (Client, OTSError, base_url, download_restore, inspect_archive,
                                job_lock, notify, prepare, read_json, save, upload, NoRedirect)


class FakeOTS:
    """Contract fake only: no Telegram connections or actual OTS installation."""
    def __init__(self):
        self.calls=[]; self.up=None; self.file=None; self.chunks={}; self.encrypted=True
        self.auto_delete=0; self.fail_chunk=False; self.fail_create=False; self.bad_verify=False
        self.bad_download=False; self.expired=False; self.require_mfa=False
        self.created=0; self.verifies=0
        model=self
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self): self.respond()
            def do_POST(self): self.respond()
            def do_PUT(self): self.respond()
            def log_message(self,*_): pass
            def respond(self):
                raw=self.rfile.read(int(self.headers.get('Content-Length',0)))
                model.calls.append((self.command,self.path))
                status,body=model.handle(self.command,self.path,raw,self.headers)
                self.send_response(status)
                if self.path.startswith('/api/auth/login') and status==200 and not body.get('mfa_required'):
                    self.send_header('Set-Cookie','ots_session='+'s'*32+'; Path=/; HttpOnly')
                    self.send_header('Set-Cookie','ots_csrf='+'c'*32+'; Path=/')
                if isinstance(body,bytes): output=body
                else: output=json.dumps(body).encode()
                self.send_header('Content-Length',str(len(output))); self.end_headers(); self.wfile.write(output)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)

    def close(self): self.server.shutdown(); self.server.server_close(); self.thread.join()

    def handle(self,method,path,raw,headers):
        if path=='/api/auth/login':
            if self.require_mfa: return 200,{'mfa_required':True,'mfa_token':'pending'}
            return 200,{'username':'backup'}
        if path=='/api/auth/login/mfa': return 200,{'username':'backup'}
        if self.expired or 'ots_session=' not in headers.get('Cookie',''): return 401,{'detail':'secret-provider-error'}
        if method!='GET' and headers.get('X-CSRF-Token')!='c'*32: return 403,{}
        if path=='/api/setup/status': return 200,{'telegram_connected':True,'channel_configured':True,'channel_auto_delete_seconds':self.auto_delete}
        if path.startswith('/api/files?q='): return 200,{'files':[self.file] if self.file else []}
        if path=='/api/uploads' and method=='GET': return 200,[self.upload_status()] if self.up else []
        if path=='/api/uploads' and method=='POST':
            data=json.loads(raw); self.created+=1
            self.up={'id':'upload-1','file_id':'file-1','name':data['name'],'size':data['size'],'chunk_size':128}
            self.file={'id':'file-1','name':data['name'],'size':data['size'],'status':'receiving',
                       'encrypted':self.encrypted,'key_id':'key-one','sha256':None,'parts_total':1,
                       'parts_uploaded':0,'verified_at':None,'verifying':False,'integrity_error':None}
            if self.fail_create: self.fail_create=False; return 503,{}
            return 200,self.upload_status()
        if path=='/api/uploads/upload-1' and self.up: return 200,self.upload_status()
        if path=='/api/uploads/upload-1/chunk':
            self.chunks[int(headers['X-Chunk-Offset'])//128]=raw
            if self.fail_chunk: self.fail_chunk=False; return 503,{}
            return 200,self.upload_status()
        if path=='/api/uploads/upload-1/complete':
            body=b''.join(self.chunks[i] for i in sorted(self.chunks))
            self.file.update(status='ready',sha256=hashlib.sha256(body).hexdigest(),parts_uploaded=1)
            assert self.file['sha256']==json.loads(raw)['sha256']
            self.up=None
            return 200,{'file':self.file,'upload':None}
        if path=='/api/files/file-1/verify':
            self.verifies+=1
            self.file.update(verified_at='2026-09-12T12:00:'+str(self.verifies),verifying=False,
                             integrity_error='bad ciphertext' if self.bad_verify else None)
            return 200,{'ok':True,'verifying':True}
        if path=='/api/files/file-1/download':
            body=b''.join(self.chunks[i] for i in sorted(self.chunks))
            return 200,(b'x'+body[1:] if self.bad_download else body)
        if path=='/api/files/file-1': return 200,self.file
        return 404,{}

    def upload_status(self):
        total=(self.up['size']+127)//128
        return {**self.up,'received':sum(len(b) for b in self.chunks.values()),
                'missing_chunks':[i for i in range(total) if i not in self.chunks][:3], 'total_chunks':total}


class OTSBackupTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name); lab=self.root/'lab'; (lab/'data').mkdir(parents=True)
        con=sqlite3.connect(lab/'data/ledger.sqlite')
        con.execute('CREATE TABLE trades(id INTEGER PRIMARY KEY, data TEXT)')
        con.execute('INSERT INTO trades VALUES (1,?)',('{"pnl":1.23}',)); con.commit(); con.close()
        (lab/'brain.config.json').write_text('{"synthetic":true}')
        self.snapshot=self.root/'snapshot'; backup(lab,self.snapshot)
        self.job=self.root/'job'; prepare(self.snapshot,self.job)
        self.fake=FakeOTS(); self.addCleanup(self.fake.close)
        self.config={'base_url':self.fake.base,'session_file':str(self.root/'session.json')}
        Client(self.config,authenticate=False).login('backup','synthetic-password',lambda:'123456')
        self.client=Client(self.config)

    def test_full_upload_remote_verification_and_real_sqlite_restore(self):
        state=upload(self.job,self.client,pause=lambda _:None)
        self.assertEqual(state['status'],'verified'); self.assertEqual(self.fake.verifies,1)
        self.assertEqual(self.fake.created,1)
        receipt=read_json(self.job/'remote-receipt.json')
        self.assertEqual(receipt['snapshot_completed_at'],read_json(self.snapshot/'manifest.json')['completed_at'])
        destination=self.root/'restored'
        download_restore(self.job/'remote-receipt.json',destination,self.client)
        self.assertEqual(verify(destination)['files'],verify(self.snapshot)['files'])
        self.assertTrue((destination/'restore-receipt.json').exists())
        (self.job/'remote-receipt.json').unlink()  # synthetic crash between final state and receipt
        upload(self.job,self.client,pause=lambda _:None)
        self.assertTrue((self.job/'remote-receipt.json').exists())
        self.assertEqual(self.fake.created,1)
        with self.assertRaises(ValueError): download_restore(self.job/'remote-receipt.json',destination,self.client)

    def test_interruption_resumes_without_repeating_accepted_chunk(self):
        self.fake.fail_chunk=True
        with self.assertRaises(OTSError): upload(self.job,self.client)
        self.assertFalse((self.job/'remote-receipt.json').exists())
        accepted=dict(self.fake.chunks)
        upload(self.job,self.client,pause=lambda _:None)
        self.assertEqual(self.fake.created,1)
        for key,value in accepted.items(): self.assertEqual(self.fake.chunks[key],value)
        puts=[x for x in self.fake.calls if x[0]=='PUT']
        self.assertEqual(len(puts),len(self.fake.chunks))

    def test_lost_creation_response_is_reconciled_not_duplicated(self):
        self.fake.fail_create=True
        with self.assertRaises(OTSError): upload(self.job,self.client)
        upload(self.job,self.client,pause=lambda _:None)
        self.assertEqual(self.fake.created,1)

    def test_encryption_and_auto_delete_preflight_fail_closed(self):
        self.fake.auto_delete=None
        with self.assertRaises(OTSError): upload(self.job,self.client)
        self.assertEqual(self.fake.created,0)
        self.fake.auto_delete=60
        with self.assertRaises(OTSError): upload(self.job,self.client)
        self.fake.auto_delete=0; self.fake.encrypted=False
        with self.assertRaises(OTSError): upload(self.job,self.client)
        self.assertFalse(self.fake.chunks)
        self.assertFalse((self.job/'remote-receipt.json').exists())

    def test_remote_corruption_never_records_success(self):
        self.fake.bad_verify=True
        with self.assertRaises(OTSError): upload(self.job,self.client,pause=lambda _:None)
        self.assertFalse((self.job/'remote-receipt.json').exists())

    def test_legacy_null_ttl_requires_confirmation_but_positive_ttl_always_blocks(self):
        self.fake.auto_delete=None
        with self.assertRaises(OTSError): upload(self.job,self.client)
        client=Client({**self.config,'channel_auto_delete_confirmed_off':True})
        self.fake.auto_delete=3600
        with self.assertRaises(OTSError): upload(self.job,client)
        self.assertEqual(self.fake.created,0)
        self.fake.auto_delete=None
        state=upload(self.job,client,pause=lambda _:None)
        self.assertEqual(state['status'],'verified')

    def test_download_corruption_has_no_restore_success_marker(self):
        upload(self.job,self.client,pause=lambda _:None); self.fake.bad_download=True
        destination=self.root/'corrupt-restore'
        with self.assertRaises(OTSError): download_restore(self.job/'remote-receipt.json',destination,self.client)
        self.assertFalse((destination/'restore-receipt.json').exists())
        self.assertFalse((destination/'data/ledger.sqlite').exists())

    def test_expired_session_origin_pinning_and_private_credentials(self):
        self.fake.expired=True
        with self.assertRaisesRegex(OTSError,'session expired'): upload(self.job,self.client)
        with self.assertRaises(OTSError): base_url('http://storage.example.com')
        with self.assertRaises(OTSError): base_url('https://user:password@storage.example.com')
        with self.assertRaises(OTSError): Client({**self.config,'base_url':'https://other.example'})
        Path(self.config['session_file']).chmod(0o644)
        with self.assertRaises(OTSError): Client(self.config)

    def test_archive_tampering_and_job_lock(self):
        with job_lock(self.job):
            with self.assertRaises(OTSError):
                with job_lock(self.job): pass
        (self.job/'archive.tar.gz').write_bytes(b'corrupt')
        with self.assertRaises(OTSError): upload(self.job,self.client)
        self.assertEqual(self.fake.created,0)

    def test_login_uses_mfa_without_storing_password_or_code(self):
        self.fake.require_mfa=True
        Client(self.config,authenticate=False).login('backup','not-stored-password',lambda:'654321')
        text=Path(self.config['session_file']).read_text()
        self.assertNotIn('not-stored-password',text); self.assertNotIn('654321',text)
        self.assertEqual(Path(self.config['session_file']).stat().st_mode & 0o777,0o600)

    def test_alert_uses_only_static_status_no_paths_or_credentials(self):
        credentials=self.root/'alerts.json'; save(credentials,{'bot_token':'123:'+('a'*30),'chat_id':'-100123'})
        sent=[]
        class Opener:
            def open(self,request,timeout):
                sent.append(json.loads(request.data)); return io.BytesIO(b'{"ok":true}')
        with patch('scripts.ots_backup.urllib.request.build_opener',return_value=Opener()):
            notify(credentials,True,'test-job')
        self.assertEqual(sent[0]['chat_id'],'-100123')
        self.assertNotIn('123:',sent[0]['text'])
        self.assertNotIn(str(self.root),sent[0]['text'])

    def test_old_snapshot_uploaded_today_is_not_a_fresh_backup(self):
        state=upload(self.job,self.client,pause=lambda _:None)
        now=time.time(); state['snapshot_completed_at']=now-90000; state['verified_at']=now-1
        save(self.job/'remote-receipt.json',state)
        self.assertEqual(remote_backup_age(self.root,now),90000)
        state['status']='verifying'; save(self.job/'remote-receipt.json',state)
        self.assertIsNone(remote_backup_age(self.root,now))

    def test_restore_rejects_traversal_before_downloading(self):
        state=upload(self.job,self.client,pause=lambda _:None)
        state['members']['../escaped']={'bytes':1,'sha256':'0'*64}
        save(self.job/'remote-receipt.json',state)
        destination=self.root/'unsafe-restore'
        with self.assertRaises(OTSError): download_restore(self.job/'remote-receipt.json',destination,self.client)
        self.assertFalse(destination.exists())
        self.assertFalse(any(path.endswith('/download') for _,path in self.fake.calls))

    def test_remote_mismatch_and_deadline_never_record_success(self):
        self.fake.fail_chunk=True
        with self.assertRaises(OTSError): upload(self.job,self.client)
        self.fake.file['size']+=1
        with self.assertRaisesRegex(OTSError,'identity mismatch'): upload(self.job,self.client)
        self.fake.file['size']-=1
        with self.assertRaisesRegex(OTSError,'time budget'): upload(self.job,self.client,timeout=-1)
        self.assertFalse((self.job/'remote-receipt.json').exists())

    def test_redirects_cannot_forward_session_credentials(self):
        with self.assertRaises(OTSError):
            NoRedirect().redirect_request(None,None,302,None,None,'https://other.example')


if __name__=='__main__': unittest.main()
