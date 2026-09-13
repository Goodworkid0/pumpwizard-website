"""Local verified archive preparation. No storage service dependency."""
import gzip
import hashlib
import os
from pathlib import Path
import re
import shutil
import tarfile
import time
import uuid

from scripts.backup_lab import digest, verify
from .common import BackupError, load as read_json, save

def prepare(source, job):
    manifest = verify(source)
    if source.resolve() == job.resolve() or source.resolve() in job.resolve().parents:
        raise BackupError('Keep transfer jobs outside the verified snapshot')
    total = sum(item['bytes'] for item in manifest['files'])+(source/'manifest.json').stat().st_size
    if shutil.disk_usage(job.parent).free < total*1.05+1024**3:
        raise BackupError('Insufficient staging space for compressed archive')
    if job.exists() or job.is_symlink(): raise BackupError('Prepare requires a new job directory')
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
                    if path.is_symlink() or not path.is_file(): raise BackupError('Snapshot source changed')
                    info = tarfile.TarInfo(name); info.size=expected[name]['bytes']; info.mode=0o600
                    with path.open('rb') as stream: archive.addfile(info, stream)
        raw.flush(); os.fsync(raw.fileno())
    # Verify the actual archived bytes, not just source files checked before packing.
    inspect_archive(job/'archive.tar.gz', expected)
    archive = job/'archive.tar.gz'
    state = {'schema':'pumpwizard.telegram-job/v1','id':uuid.uuid4().hex,'created_at':time.time(),
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
                raise BackupError('Unexpected, unsafe or duplicate archive member')
            item = expected[member.name]
            if member.size != item['bytes']: raise BackupError('Archive member size mismatch')
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
            if h.hexdigest() != item['sha256']: raise BackupError('Archive member checksum mismatch')
    if seen != set(expected): raise BackupError('Archive is incomplete')


def load_job(job):
    state = read_json(job/'job.json')
    if state.get('schema') != 'pumpwizard.telegram-job/v1': raise BackupError('Unsupported job')
    if not re.fullmatch(r'[0-9a-f]{32}',state['id']): raise BackupError('Invalid backup identifier')
    for name, item in state['members'].items():
        parts = Path(name).parts
        if name != 'manifest.json' and (len(parts)!=2 or parts[0] not in ('data','config') or parts[1] in ('.','..')):
            raise BackupError('Invalid archived path')
        if not isinstance(item['bytes'],int) or item['bytes']<0: raise BackupError('Invalid member length')
    archive = job/'archive.tar.gz'
    if (archive.is_symlink() or archive.stat().st_size != state['archive_bytes']
            or digest(archive) != state['archive_sha256']): raise BackupError('Prepared archive changed')
    return state



