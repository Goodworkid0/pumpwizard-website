"""Bounded multi-connection MTProto upload adapted from OTS fast_upload.py.

See third_party/OpenTelegramStorage-LICENSE.txt. Private sender setup is tested
against the pinned Telethon version; errors do not silently bypass throttling.
"""
import asyncio
from telethon import helpers
from telethon.tl import functions, types
from .common import BackupError

PART_SIZE=512*1024
BIG_FILE_THRESHOLD=10*1024**2


async def make_sender(client):
    from telethon.network import MTProtoSender
    dc=await client._get_dc(client.session.dc_id)
    sender=MTProtoSender(client.session.auth_key,loggers=client._log)
    try:
        await asyncio.wait_for(sender.connect(client._connection(
            dc.ip_address,dc.port,dc.id,loggers=client._log,proxy=client._proxy)),timeout=30)
        return sender
    except BaseException:
        await sender.disconnect()
        raise


async def upload_parallel(client,stream,size,name,connections=4,progress=None):
    if not 1<=connections<=8 or size<=BIG_FILE_THRESHOLD: raise BackupError('Invalid parallel upload size or concurrency')
    file_id=helpers.generate_random_long(); count=(size+PART_SIZE-1)//PART_SIZE
    read=0; index=0; sent=0; senders=[]
    # Reader operations have no await: workers receive disjoint ranges and memory
    # stays bounded by connections * 512 KiB, not total archive size.
    async def worker(sender):
        nonlocal read,index,sent
        while read<size:
            n=min(PART_SIZE,size-read); data=stream.read(n); idx=index
            if len(data)!=n: raise BackupError('Upload stream ended early')
            read+=n; index+=1
            request=functions.upload.SaveBigFilePartRequest(file_id,idx,count,data)
            for attempt in range(3):
                try:
                    result=await asyncio.wait_for(sender.send(request),timeout=90)
                    if result is not True: raise BackupError('Telegram rejected upload bytes')
                    break
                except (OSError,TimeoutError):
                    if attempt==2: raise
                    await asyncio.sleep(attempt+1)
            sent+=len(data)
            if progress: progress(sent,size)
    try:
        for _ in range(connections): senders.append(await make_sender(client))
        # TaskGroup cancels/joins siblings before disconnecting on any failure.
        async with asyncio.TaskGroup() as tasks:
            for sender in senders: tasks.create_task(worker(sender))
    finally:
        for sender in senders:
            try: await asyncio.wait_for(sender.disconnect(),timeout=10)
            except Exception: pass
    if sent!=size: raise BackupError('Upload byte total differs')
    return types.InputFileBig(id=file_id,parts=count,name=name)
