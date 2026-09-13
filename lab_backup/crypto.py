"""Authenticated bounded-block encryption adapted from OpenTelegramStorage.

See third_party/OpenTelegramStorage-LICENSE.txt. Distinct PWB1 format and HKDF
domain; each block authenticates header, purpose (run/part) and block index.
"""
import hashlib
import os
import secrets
import struct
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .common import BackupError

BLOCK=1024*1024
HEADER=28
MAGIC=b'PWB1'


def key_id(key): return hashlib.sha256(key).hexdigest()[:24]


def aead(key,salt):
    if len(key)!=32: raise BackupError('Invalid backup content key')
    return AESGCM(HKDF(algorithm=hashes.SHA256(),length=32,salt=salt,info=b'pumpwizard-backup-v1').derive(key))


def encrypt(source,target,key,context,offset=0,length=None):
    length=Path(source).stat().st_size-offset if length is None else length
    if length<0: raise BackupError('Invalid encryption length')
    salt=secrets.token_bytes(16); head=MAGIC+salt+struct.pack('>Q',length)
    cipher=aead(key,salt); remaining=length; plain_hash=hashlib.sha256(); cipher_hash=hashlib.sha256()
    with Path(source).open('rb') as src, Path(target).open('xb') as out:
        os.chmod(target,0o600); src.seek(offset); out.write(head); cipher_hash.update(head)
        for i in range(max(1,(length+BLOCK-1)//BLOCK)):
            want=min(BLOCK,remaining); data=src.read(want)
            if len(data)!=want: raise BackupError('Encryption source changed')
            index=struct.pack('>Q',i)
            ct=cipher.encrypt(b'\0'*4+index,data,head+context.encode()+index)
            out.write(ct); cipher_hash.update(ct); plain_hash.update(data); remaining-=want
        out.flush(); os.fsync(out.fileno())
    return {'bytes':length,'sha256':plain_hash.hexdigest(),'cipher_bytes':Path(target).stat().st_size,
            'cipher_sha256':cipher_hash.hexdigest()}


def decrypt(source,target,key,context,expected):
    if Path(source).stat().st_size!=expected['cipher_bytes']: raise BackupError('Ciphertext length differs')
    cipher_hash=hashlib.sha256(); plain_hash=hashlib.sha256()
    with Path(source).open('rb') as src, Path(target).open('xb') as out:
        os.chmod(target,0o600)
        head=src.read(HEADER); cipher_hash.update(head)
        if len(head)!=HEADER or head[:4]!=MAGIC: raise BackupError('Invalid encrypted backup header')
        length=struct.unpack('>Q',head[20:])[0]
        if length!=expected['bytes']: raise BackupError('Authenticated backup size differs')
        cipher=aead(key,head[4:20]); remaining=length
        for i in range(max(1,(length+BLOCK-1)//BLOCK)):
            want=min(BLOCK,remaining); ct=src.read(want+16)
            if len(ct)!=want+16: raise BackupError('Truncated encrypted backup')
            index=struct.pack('>Q',i)
            try: data=cipher.decrypt(b'\0'*4+index,ct,head+context.encode()+index)
            except Exception: raise BackupError('Backup authentication failed') from None
            out.write(data); cipher_hash.update(ct); plain_hash.update(data); remaining-=want
        if src.read(1): raise BackupError('Unexpected encrypted backup trailer')
        out.flush(); os.fsync(out.fileno())
    if cipher_hash.hexdigest()!=expected['cipher_sha256'] or plain_hash.hexdigest()!=expected['sha256']:
        raise BackupError('Backup checksum mismatch')
