import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
from contextlib import contextmanager


class BackupError(Exception):
    """Safe-to-log operational error; never embed credentials or provider bodies."""


def load(path, private=False):
    path=Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size>5_000_000:
        raise BackupError('Invalid configuration or state file')
    if private and (path.stat().st_uid!=os.getuid() or stat.S_IMODE(path.stat().st_mode)&0o077):
        raise BackupError('Secret file requires owner-only permissions')
    return json.loads(path.read_text())


def save(path, value):
    path=Path(path)
    if path.is_symlink(): raise BackupError('Refusing symlink state file')
    fd,name=tempfile.mkstemp(prefix='.backup-',dir=path.parent)
    try:
        with os.fdopen(fd,'w') as stream:
            json.dump(value,stream,indent=2,allow_nan=False); stream.flush(); os.fsync(stream.fileno())
        os.replace(name,path)
        fd=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
    finally:
        if os.path.exists(name): os.unlink(name)


@contextmanager
def lock(path):
    fd=os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        try: fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError: raise BackupError('Another backup operation is running') from None
        yield
    finally: os.close(fd)
