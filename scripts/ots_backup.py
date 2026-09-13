"""OpenTelegramStorage adapter for completed lab backups; no live-ledger uploads."""
import argparse
from contextlib import contextmanager
import fcntl
import getpass
import gzip
import hashlib
import http.cookiejar
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

try:
    from .backup_lab import digest, verify, prepare_destination
except ImportError:
    from backup_lab import digest, verify, prepare_destination


class OTSError(Exception):
    """Safe-to-log failure; never includes credentials or remote response bodies."""


def read_json(path, private=False):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 5_000_000:
        raise OTSError('Invalid JSON file')
    if private and (stat.S_IMODE(path.stat().st_mode) & 0o077 or path.stat().st_uid != os.getuid()):
        raise OTSError('Credential file must be owned by this user with mode 0600')
    return json.loads(path.read_text())


def save(path, value):
    if path.is_symlink(): raise OTSError('Refusing symlink state file')
    fd, temporary = tempfile.mkstemp(prefix='.ots-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, allow_nan=False)
            stream.flush(); os.fsync(stream.fileno())
        os.replace(temporary, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(temporary): os.unlink(temporary)


@contextmanager
def job_lock(job):
    if job.is_symlink() or not job.is_dir(): raise OTSError('Job directory must be a regular directory')
    fd = os.open(job/'.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise OTSError('This job is already running') from None
        yield
    finally: os.close(fd)


def base_url(value):
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in ('http','https') or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ('','/')):
        raise OTSError('OTS address must be an origin URL without credentials, path or query')
    if parsed.scheme == 'http' and parsed.hostname not in ('127.0.0.1','localhost','::1'):
        raise OTSError('Remote OTS connections require HTTPS')
    return value.rstrip('/')


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', value):
        raise OTSError('Invalid remote object identifier')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise OTSError('Redirect refused; check the configured OTS address')


class Client:
    def __init__(self, config, authenticate=True):
        self.base = base_url(config['base_url'])
        # OTS 0.1.12 normalizes a successful off/zero TTL to None, the same
        # value it uses before a successful check. Never silently infer off.
        self.auto_delete_confirmed_off = config.get('channel_auto_delete_confirmed_off') is True
        self.session_file = Path(config['session_file']).expanduser()
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect(),
                                                  urllib.request.HTTPCookieProcessor(self.jar))
        self.session = read_json(self.session_file, private=True) if authenticate else None
        if self.session and self.session.get('base_url') != self.base:
            raise OTSError('Session belongs to a different OTS origin')

    def request(self, method, path, data=None, raw=None, headers=None, stream=False):
        if not path.startswith('/api/') or path.startswith('//'): raise OTSError('Invalid API path')
        request_headers = {'Accept':'application/json'}
        if self.session:
            session, csrf = self.session['session'], self.session['csrf']
            if any(not re.fullmatch(r'[A-Za-z0-9_-]{10,256}', v) for v in (session, csrf)):
                raise OTSError('Invalid session credentials')
            request_headers.update({'Cookie':f'ots_session={session}; ots_csrf={csrf}', 'X-CSRF-Token':csrf})
        if data is not None:
            raw = json.dumps(data).encode(); request_headers['Content-Type'] = 'application/json'
        if headers: request_headers.update(headers)
        req = urllib.request.Request(self.base+path, data=raw, method=method, headers=request_headers)
        try:
            response = self.opener.open(req, timeout=45)
            if stream: return response
            with response:
                body = response.read(5_000_001)
                if len(body)>5_000_000: raise OTSError('Oversized OTS response')
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            code = exc.code; exc.close()
            if code in (401,403): raise OTSError('OTS session expired or access denied; log in again') from None
            raise OTSError('OTS request failed (HTTP '+str(code)+'); rerun this job to resume') from None
        except (OSError, ValueError):
            raise OTSError('OTS connection or response failed; rerun this job to resume') from None

    def login(self, username, password, code_prompt):
        result = self.request('POST','/api/auth/login',{'username':username,'password':password})
        if result.get('mfa_required'):
            self.request('POST','/api/auth/login/mfa',{'mfa_token':result['mfa_token'],'code':code_prompt()})
        cookies = {c.name:c.value for c in self.jar}
        if not cookies.get('ots_session') or not cookies.get('ots_csrf'): raise OTSError('Login did not return a session')
        save(self.session_file, {'base_url':self.base,'session':cookies['ots_session'],
                                'csrf':cookies['ots_csrf'],'created_at':time.time()})


def prepare(source, job):
    manifest = verify(source)
    if source.resolve() == job.resolve() or source.resolve() in job.resolve().parents:
        raise OTSError('Keep transfer jobs outside the verified snapshot')
    total = sum(item['bytes'] for item in manifest['files'])+(source/'manifest.json').stat().st_size
    if shutil.disk_usage(job.parent).free < total*1.05+1024**3:
        raise OTSError('Insufficient staging space for compressed archive')
    if job.exists() or job.is_symlink(): raise OTSError('Prepare requires a new job directory')
    job.mkdir(mode=0o700)
    names = ['manifest.json']+[item['path'] for item in manifest['files']]
    expected = {item['path']:item for item in manifest['files']}
    expected['manifest.json'] = {'bytes':(source/'manifest.json').stat().st_size,'sha256':digest(source/'manifest.json')}
    with (job/'archive.tar.gz').open('xb') as raw:
        os.chmod(job/'archive.tar.gz', 0o600)
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0, compresslevel=3) as compressed:
            with tarfile.open(fileobj=compressed, mode='w|', format=tarfile.PAX_FORMAT) as archive:
                for name in names:
                    path = source/name
                    if path.is_symlink() or not path.is_file(): raise OTSError('Snapshot source changed')
                    info = tarfile.TarInfo(name); info.size=expected[name]['bytes']; info.mode=0o600
                    with path.open('rb') as stream: archive.addfile(info, stream)
        raw.flush(); os.fsync(raw.fileno())
    # Verify the actual archived bytes, not just source files checked before packing.
    inspect_archive(job/'archive.tar.gz', expected)
    archive = job/'archive.tar.gz'
    state = {'schema':'pumpwizard.ots-job/v1','id':uuid.uuid4().hex,'created_at':time.time(),
             'snapshot_completed_at':manifest['completed_at'], 'snapshot_consistency':manifest['consistency'],
             'archive_bytes':archive.stat().st_size,'archive_sha256':digest(archive),
             'members':expected,'status':'prepared'}
    save(job/'job.json', state)
    return state


def inspect_archive(archive, expected, destination=None):
    seen = set()
    with tarfile.open(archive, 'r|gz') as tar:
        for member in tar:
            if member.name not in expected or member.name in seen or not member.isfile():
                raise OTSError('Unexpected, unsafe or duplicate archive member')
            item = expected[member.name]
            if member.size != item['bytes']: raise OTSError('Archive member size mismatch')
            seen.add(member.name); h = hashlib.sha256()
            source = tar.extractfile(member)
            output = (destination/member.name).open('xb') if destination else None
            try:
                while block := source.read(1024*1024):
                    h.update(block)
                    if output: output.write(block)
                if output: output.flush(); os.fsync(output.fileno())
            finally:
                source.close()
                if output: output.close()
            if h.hexdigest() != item['sha256']: raise OTSError('Archive member checksum mismatch')
    if seen != set(expected): raise OTSError('Archive is incomplete')


def load_job(job):
    state = read_json(job/'job.json')
    if state.get('schema') != 'pumpwizard.ots-job/v1': raise OTSError('Unsupported job')
    identifier(state['id'])
    for name, item in state['members'].items():
        parts = Path(name).parts
        if name != 'manifest.json' and (len(parts)!=2 or parts[0] not in ('data','config') or parts[1] in ('.','..')):
            raise OTSError('Invalid archived path')
        if not isinstance(item['bytes'],int) or item['bytes']<0: raise OTSError('Invalid member length')
    archive = job/'archive.tar.gz'
    if (archive.is_symlink() or archive.stat().st_size != state['archive_bytes']
            or digest(archive) != state['archive_sha256']): raise OTSError('Prepared archive changed')
    return state


def check_file(remote, state, completed=False):
    if (remote.get('id') != state['file_id'] or remote.get('size') != state['archive_bytes']
            or remote.get('name') != state['remote_name']): raise OTSError('Remote file identity mismatch')
    if remote.get('encrypted') is not True or not remote.get('key_id'):
        raise OTSError('OTS encryption must be enabled; no backup bytes will be sent unencrypted')
    if remote.get('status') == 'failed' or remote.get('integrity_error'):
        raise OTSError('OTS reports a failed transfer or integrity error')
    if completed and (remote.get('status') != 'ready' or remote.get('sha256') != state['archive_sha256']
            or not remote.get('parts_total') or remote.get('parts_uploaded') != remote.get('parts_total')):
        raise OTSError('Remote archive is not complete or its checksum differs')


def upload(job, client, timeout=21600, pause=time.sleep):
    state = load_job(job)
    if state.get('base_url') and state['base_url'] != client.base: raise OTSError('Job is pinned to a different OTS origin')
    state['base_url'] = client.base
    if state.get('status') == 'verified':
        # Recover a crash between persisting success and writing its receipt.
        save(job/'remote-receipt.json',state)
        return state
    setup = client.request('GET','/api/setup/status')
    ttl = setup.get('channel_auto_delete_seconds')
    ttl_off = type(ttl) is int and ttl == 0
    legacy_confirmed = ttl is None and getattr(client,'auto_delete_confirmed_off',False)
    if not setup.get('telegram_connected') or not setup.get('channel_configured') or not (ttl_off or legacy_confirmed):
        raise OTSError('OTS must be connected and channel auto-delete confirmed off')
    deadline = time.monotonic()+timeout
    def tick():
        if time.monotonic()>deadline: raise OTSError('Transfer time budget exceeded; resume this job later')
    state.setdefault('remote_name', 'pumpwizard-'+state['id']+'.tar.gz')
    if not state.get('file_id'):
        # A lost POST response must not silently create a second backup. Save an
        # intent first, then reconcile the unique name on the next invocation.
        if state.get('status') == 'initializing':
            files = client.request('GET','/api/files?q='+state['remote_name'])['files']
            matches = [f for f in files if f.get('name') == state['remote_name']]
            if len(matches)!=1: raise OTSError('Ambiguous upload creation; inspect OTS before retrying')
            state['file_id'] = identifier(matches[0]['id'])
            uploads = client.request('GET','/api/uploads')
            matches = [u for u in uploads if u.get('file_id') == state['file_id']]
            if len(matches)!=1: raise OTSError('Cannot safely recover upload identity')
            state['upload_id'] = identifier(matches[0]['id'])
        else:
            state['status']='initializing'; save(job/'job.json',state)
            up = client.request('POST','/api/uploads',{'name':state['remote_name'],
                'size':state['archive_bytes'],'mime_type':'application/gzip'})
            state['file_id']=identifier(up['file_id']); state['upload_id']=identifier(up['id'])
        state['status']='uploading'; save(job/'job.json',state)
    remote = client.request('GET','/api/files/'+identifier(state['file_id']))
    check_file(remote,state)
    if remote['status'] == 'receiving':
        up = client.request('GET','/api/uploads/'+identifier(state['upload_id']))
        cs = up.get('chunk_size')
        if (up.get('file_id')!=state['file_id'] or up.get('size')!=state['archive_bytes']
                or not isinstance(cs,int) or not 1<=cs<=32*1024**2): raise OTSError('Invalid upload state')
        total = (state['archive_bytes']+cs-1)//cs
        # missing_chunks may be a limited list; keep requesting until none remain.
        while True:
            tick(); missing = up.get('missing_chunks')
            if not isinstance(missing,list) or any(not isinstance(n,int) or not 0<=n<total for n in missing):
                raise OTSError('Invalid resume map')
            if not missing:
                if up.get('received')!=state['archive_bytes']: raise OTSError('Incomplete upload without missing-chunk map')
                break
            with (job/'archive.tar.gz').open('rb') as stream:
                for index in missing:
                    tick(); stream.seek(index*cs); block=stream.read(min(cs,state['archive_bytes']-index*cs))
                    client.request('PUT','/api/uploads/'+state['upload_id']+'/chunk',raw=block,
                                   headers={'Content-Type':'application/octet-stream','X-Chunk-Offset':str(index*cs)})
            up=client.request('GET','/api/uploads/'+state['upload_id'])
        client.request('POST','/api/uploads/'+state['upload_id']+'/complete',{'sha256':state['archive_sha256']})
    state['status']='awaiting_remote'; save(job/'job.json',state)
    while True:
        tick(); remote=client.request('GET','/api/files/'+state['file_id']); check_file(remote,state)
        if remote['status']=='ready': break
        pause(5)
    check_file(remote,state,completed=True)
    before=remote.get('verified_at')
    state['status']='verifying'; save(job/'job.json',state)
    client.request('POST','/api/files/'+state['file_id']+'/verify',{})
    while True:
        tick(); remote=client.request('GET','/api/files/'+state['file_id']); check_file(remote,state,completed=True)
        if remote.get('verified_at') and remote['verified_at']!=before and remote.get('verifying') is False: break
        pause(5)
    state.update(status='verified',verified_at=time.time(),ots_verified_at=remote['verified_at'],key_id=remote['key_id'])
    save(job/'job.json',state)
    save(job/'remote-receipt.json',state)
    return state


def download_restore(receipt, destination, client):
    state=read_json(receipt)
    if state.get('status')!='verified' or state.get('base_url')!=client.base: raise OTSError('Verified receipt and OTS origin required')
    # Validate all paths before opening any output file.
    for name in state['members']:
        if name!='manifest.json' and not re.fullmatch(r'(data|config)/[A-Za-z0-9_.-]+',name): raise OTSError('Unsafe restore member')
        if Path(name).name in ('.','..'): raise OTSError('Unsafe restore member')
    remote=client.request('GET','/api/files/'+identifier(state['file_id'])); check_file(remote,state,completed=True)
    required=state['archive_bytes']+sum(i['bytes'] for i in state['members'].values())+1024**3
    if shutil.disk_usage(destination.parent).free<required: raise OTSError('Insufficient restore space')
    prepare_destination(destination)
    archive=destination/'download.tar.gz'; h=hashlib.sha256(); count=0
    with client.request('GET','/api/files/'+state['file_id']+'/download',stream=True) as response, archive.open('xb') as out:
        deadline=time.monotonic()+21600
        while block:=response.read(1024**2):
            if time.monotonic()>deadline: raise OTSError('Download time budget exceeded')
            count+=len(block)
            if count>state['archive_bytes']: raise OTSError('Oversized download')
            h.update(block); out.write(block)
        out.flush(); os.fsync(out.fileno())
    if count!=state['archive_bytes'] or h.hexdigest()!=state['archive_sha256']: raise OTSError('Downloaded archive checksum mismatch')
    inspect_archive(archive,state['members'],destination)
    verify(destination)
    save(destination/'restore-receipt.json',{'schema':'pumpwizard.ots-restore/v1','verified_at':time.time(),
                                         'file_id':state['file_id'],'archive_sha256':state['archive_sha256']})


def notify(credentials, success, job_id):
    secret=read_json(credentials,private=True)
    token=secret['bot_token']; chat=str(secret['chat_id'])
    if not re.fullmatch(r'[0-9]+:[A-Za-z0-9_-]{20,}',token) or not re.fullmatch(r'-?[0-9]+',chat):
        raise OTSError('Invalid Telegram alert credentials')
    text='PumpWizard backup '+('remotely verified' if success else 'needs attention')+'. Job '+identifier(job_id)+'.'
    request=urllib.request.Request('https://api.telegram.org/bot'+token+'/sendMessage',
        data=json.dumps({'chat_id':chat,'text':text}).encode(),headers={'Content-Type':'application/json'})
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(request,timeout=15) as response:
            if json.loads(response.read(32768)).get('ok') is not True: raise OTSError('Telegram alert delivery failed')
    except (OSError,ValueError): raise OTSError('Telegram alert delivery failed') from None


def main():
    os.umask(0o077)
    p=argparse.ArgumentParser(description=__doc__); sub=p.add_subparsers(dest='command',required=True)
    s=sub.add_parser('prepare'); s.add_argument('--source',type=Path,required=True); s.add_argument('--job',type=Path,required=True)
    s=sub.add_parser('login'); s.add_argument('--config',type=Path,required=True); s.add_argument('--username',required=True)
    s=sub.add_parser('upload'); s.add_argument('--config',type=Path,required=True); s.add_argument('--job',type=Path,required=True); s.add_argument('--apply',action='store_true')
    s=sub.add_parser('restore'); s.add_argument('--config',type=Path,required=True); s.add_argument('--receipt',type=Path,required=True); s.add_argument('--destination',type=Path,required=True)
    args=p.parse_args(); config=None
    try:
        if args.command=='prepare':
            result=prepare(args.source,args.job); print(json.dumps({'status':'prepared','job_id':result['id'],'archive_bytes':result['archive_bytes']})); return
        config=read_json(args.config)
        if args.command=='login':
            Client(config,authenticate=False).login(args.username,getpass.getpass('OTS password: '),lambda:getpass.getpass('OTS 2FA code: '))
            print('Private OTS session saved. Re-login when it expires.'); return
        if args.command=='upload':
            if not args.apply: raise OTSError('Uploading requires --apply; no data was sent')
            with job_lock(args.job):
                result=upload(args.job,Client(config))
                if config.get('alert_credentials_file'):
                    notify(Path(config['alert_credentials_file']),True,result['id'])
            print(json.dumps({'status':'verified','job_id':result['id'],'file_id':result['file_id']}))
        else:
            download_restore(args.receipt,args.destination,Client(config)); print('Remote download and SQLite restore verified; no collector started.')
    except (OTSError,OSError,ValueError,KeyError,TypeError,tarfile.TarError) as exc:
        # Never print remote URLs, raw exception text, response bodies or secrets.
        message=str(exc) if isinstance(exc,OTSError) else 'Local backup operation failed; inspect the private job and configuration'
        if config and args.command=='upload' and args.apply and config.get('alert_credentials_file'):
            try: notify(Path(config['alert_credentials_file']),False,read_json(args.job/'job.json')['id'])
            except Exception: message+='; alert delivery also failed'
        p.exit(1,message+'\n')


if __name__=='__main__': main()
