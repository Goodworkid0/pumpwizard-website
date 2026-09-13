import asyncio
import io
import unittest
from lab_backup.fast_download import CHUNK, download_parallel


class DownloadTests(unittest.TestCase):
    def test_strides_tail_and_cleanup(self):
        async def scenario(broken=False):
            data=bytes(range(256))*(CHUNK//256*11)+b'tail'; iterators=[]
            class Iterator:
                def __init__(self,offset,stride,limit): self.offset,self.stride,self.left=offset,stride,limit; self.closed=False
                async def __anext__(self):
                    await asyncio.sleep(.001)
                    if self.left<=0: raise StopAsyncIteration
                    value=data[self.offset:self.offset+CHUNK]
                    self.offset+=self.stride; self.left-=1
                    return value[:-1] if broken else value
                async def close(self): self.closed=True
            class Client:
                def iter_download(self,document,**kw):
                    iterator=Iterator(kw['offset'],kw['stride'],kw['limit']); iterators.append(iterator); return iterator
            out=io.BytesIO()
            if broken:
                with self.assertRaises(ExceptionGroup): await download_parallel(Client(),None,out,len(data))
            else:
                await download_parallel(Client(),None,out,len(data)); self.assertEqual(out.getvalue(),data)
            self.assertTrue(all(i.closed for i in iterators))
        asyncio.run(scenario()); asyncio.run(scenario(True))
