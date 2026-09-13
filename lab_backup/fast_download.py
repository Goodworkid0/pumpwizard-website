"""Bounded strided downloads using Telethon's documented iterator API."""
import asyncio
from .common import BackupError

CHUNK=512*1024


async def download_parallel(client, document, stream, size, connections=4, progress=None):
    if not 1<=connections<=8 or not 0<size<=1024**3+1024**2: raise BackupError('Invalid parallel download bounds')
    count=(size+CHUNK-1)//CHUNK; done=0
    async def worker(index):
        nonlocal done
        parts=(count-1-index)//connections+1
        if parts<=0: return
        iterator=client.iter_download(document,offset=index*CHUNK,stride=connections*CHUNK,
            limit=parts,chunk_size=CHUNK,request_size=CHUNK,file_size=size)
        try:
            for step in range(parts):
                try: block=await asyncio.wait_for(anext(iterator),timeout=120)
                except StopAsyncIteration: raise BackupError('Truncated parallel download') from None
                offset=(index+step*connections)*CHUNK
                if len(block)!=min(CHUNK,size-offset): raise BackupError('Download chunk size differs')
                stream.seek(offset); stream.write(block)
                done+=len(block)
                if progress: progress(done,size)
        finally: await iterator.close()
    async with asyncio.TaskGroup() as tasks:
        for index in range(connections): tasks.create_task(worker(index))
    if done!=size: raise BackupError('Download byte total differs')
    stream.flush()
