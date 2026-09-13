"""Durable multipart backup protocol; only verified read-back creates a receipt."""
import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from scripts.backup_lab import digest, prepare_destination, verify
from .archive import load_job, inspect_archive
from .common import BackupError, load, save
from .crypto import encrypt, decrypt, key_id


def purpose(run,index): return 'pumpwizard/'+run+'/'+str(index)


def validate_receipt(receipt):
    if receipt.get('schema')!='pumpwizard.telegram-receipt/v1' or receipt.get('status')!='verified': raise BackupError('Invalid verified receipt')
    if not re.fullmatch('[a-f0-9]{32}',receipt['id']): raise BackupError('Invalid backup identifier')
    for name,member in receipt['members'].items():
        if name!='manifest.json' and not re.fullmatch(r'(data|config)/[A-Za-z0-9_.-]+',name): raise BackupError('Unsafe archive path')
        if Path(name).name in ('.','..') or type(member['bytes']) is not int or member['bytes']<0: raise BackupError('Invalid archive member')
    offset=0
    for index,part in enumerate(receipt['parts']):
        if part['index']!=index or part['offset']!=offset or part['bytes']<0 or not isinstance(part['message_id'],int):
            raise BackupError('Invalid part sequence')
        offset+=part['bytes']
    if offset!=receipt['archive_bytes']: raise BackupError('Incomplete backup part sequence')


async def read_part(transport,key,run,part,folder):
    cipher=folder/'received.pwb'; plain=folder/'received.bin'
    await transport.download(part['message_id'],cipher,part['cipher_bytes'])
    decrypt(cipher,plain,key,purpose(run,part['index']),part)
    return plain


async def send_item(transport,path,caption,state,field,job):
    item=state[field] if isinstance(field,str) else state['parts'][field]
    if item.get('message_id'): return
    if item.get('sending'):
        # A missing acknowledgement is not proof that Telegram rejected a post.
        # Stop rather than create a duplicate or overwrite any remote record.
        raise BackupError('Previous send outcome is uncertain; reconcile the labelled channel message before resuming')
    def before_send():
        item['sending']=True; save(job/'job.json',state)
    message_id=await transport.upload(path,caption,before_send=before_send)
    if type(message_id) is not int or message_id<=0: raise BackupError('Invalid Telegram message acknowledgement')
    item.update(message_id=message_id,sending=False); save(job/'job.json',state)


async def upload(job,transport,key,part_bytes=128*1024**2):
    state=load_job(job)
    if not 1024<=part_bytes<=1024**3: raise BackupError('Invalid Telegram part size')
    if state.get('channel_id',transport.channel_id)!=transport.channel_id or state.get('key_id',key_id(key))!=key_id(key):
        raise BackupError('Backup destination or key changed; refusing to mix parts')
    if state['status']=='verified' and (job/'telegram-receipt.json').exists(): return load(job/'telegram-receipt.json')
    state.update(channel_id=transport.channel_id,key_id=key_id(key),status='uploading')
    if 'parts' not in state:
        state['parts']=[{'index':i,'offset':start,'bytes':min(part_bytes,state['archive_bytes']-start)}
                        for i,start in enumerate(range(0,state['archive_bytes'],part_bytes))]
    save(job/'job.json',state)
    await transport.preflight()
    # One part at a time bounds staging, memory and Telegram pressure. Every
    # acknowledged part is downloaded/decrypted again on resume.
    whole=hashlib.sha256(); total_read=0
    for part in state['parts']:
        index=part['index']; cipher=job/f'part-{index:05d}.pwb'
        if not part.get('message_id'):
            if part.get('sending'): raise BackupError('Previous part send outcome is uncertain; inspect channel before resuming')
            if cipher.exists():
                if not part.get('cipher_sha256') or digest(cipher)!=part['cipher_sha256']:
                    raise BackupError('Staged encrypted part changed')
            else:
                part.update(encrypt(job/'archive.tar.gz',cipher,key,purpose(state['id'],index),part['offset'],part['bytes']))
                save(job/'job.json',state)
            await send_item(transport,cipher,{'pwbackup':1,'id':state['id'],'kind':'part','part':index,
                            'of':len(state['parts'])},state,index,job)
        with tempfile.TemporaryDirectory(prefix='verify-',dir=job) as temp:
            plain_part=await read_part(transport,key,state['id'],part,Path(temp))
            with plain_part.open('rb') as stream:
                while block:=stream.read(1024**2): whole.update(block); total_read+=len(block)
        part['verified_at']=time.time(); save(job/'job.json',state)
        print('Verified encrypted part '+str(index+1)+'/'+str(len(state['parts'])),flush=True)
        if cipher.exists(): cipher.unlink()  # only this generated, remotely verified staging part
    if total_read!=state['archive_bytes'] or whole.hexdigest()!=state['archive_sha256']:
        raise BackupError('Whole archive read-back checksum differs')
    receipt={'schema':'pumpwizard.telegram-receipt/v1','status':'verified','id':state['id'],
             'snapshot_completed_at':state['snapshot_completed_at'],'snapshot_consistency':state['snapshot_consistency'],
             'channel_id':state['channel_id'],'key_id':state['key_id'],'archive_bytes':state['archive_bytes'],
             'archive_sha256':state['archive_sha256'],'members':state['members'],'parts':state['parts'],
             'verified_at':time.time(),'purpose':state.get('purpose','production')}
    validate_receipt(receipt)
    # Encrypted recovery manifest in the channel: data/key recovery does not rely
    # on the local job directory surviving. No OTS caption marker is used.
    plain=job/'manifest-plain.json'; cipher=job/'manifest.pwb'
    if 'remote_manifest' not in state:
        save(plain,receipt)
        state['remote_manifest']=encrypt(plain,cipher,key,purpose(state['id'],'manifest'))
        save(job/'job.json',state)
    await send_item(transport,cipher,{'pwbackup':1,'id':state['id'],'kind':'manifest',
                    **{k:state['remote_manifest'][k] for k in ('bytes','sha256','cipher_bytes','cipher_sha256')}},state,'remote_manifest',job)
    with tempfile.TemporaryDirectory(prefix='manifest-check-',dir=job) as temp:
        received=Path(temp)/'manifest.pwb'; decoded=Path(temp)/'manifest.json'
        await transport.download(state['remote_manifest']['message_id'],received,state['remote_manifest']['cipher_bytes'])
        decrypt(received,decoded,key,purpose(state['id'],'manifest'),state['remote_manifest'])
        receipt=load(decoded); validate_receipt(receipt)
    receipt['manifest_message_id']=state['remote_manifest']['message_id']
    receipt['verified_at']=time.time()
    save(job/'telegram-receipt.json',receipt)
    state.update(status='verified',verified_at=receipt['verified_at']); save(job/'job.json',state)
    for generated in (cipher,plain):
        if generated.exists(): generated.unlink()
    return receipt


async def restore(receipt,transport,key,destination):
    validate_receipt(receipt)
    if receipt['channel_id']!=transport.channel_id or receipt['key_id']!=key_id(key): raise BackupError('Restore key or channel differs')
    required=receipt['archive_bytes']+sum(i['bytes'] for i in receipt['members'].values())+1024**3
    if shutil.disk_usage(destination.parent).free<required: raise BackupError('Insufficient restore space')
    prepare_destination(destination)
    archive=destination/'download.tar.gz'
    with archive.open('xb') as out:
        for part in receipt['parts']:
            with tempfile.TemporaryDirectory(prefix='part-',dir=destination) as temp:
                plain=await read_part(transport,key,receipt['id'],part,Path(temp))
                with plain.open('rb') as stream: shutil.copyfileobj(stream,out,1024**2)
        out.flush(); os.fsync(out.fileno())
    if digest(archive)!=receipt['archive_sha256']: raise BackupError('Restored archive checksum differs')
    inspect_archive(archive,receipt['members'],destination)
    verify(destination)
    save(destination/'telegram-restore.json',{'id':receipt['id'],'verified_at':time.time(),'archive_sha256':receipt['archive_sha256']})


async def recover_manifest(transport,key,message_id,folder):
    info=await transport.manifest_info(message_id)
    if info.get('pwbackup')!=1 or info.get('kind')!='manifest' or not re.fullmatch('[a-f0-9]{32}',info.get('id','')):
        raise BackupError('Message is not a PumpWizard recovery manifest')
    if not 0<info['cipher_bytes']<=5_000_000: raise BackupError('Recovery manifest is too large')
    folder.mkdir(mode=0o700,exist_ok=False)
    await transport.download(message_id,folder/'manifest.pwb',info['cipher_bytes'])
    decrypt(folder/'manifest.pwb',folder/'telegram-receipt.json',key,purpose(info['id'],'manifest'),info)
    receipt=load(folder/'telegram-receipt.json'); validate_receipt(receipt)
    if receipt['id']!=info['id']: raise BackupError('Recovery manifest identity differs')
    return receipt
