import asyncio
import io
import unittest
from unittest.mock import patch
from lab_backup.fast_upload import upload_parallel, PART_SIZE


class FastUploadTests(unittest.TestCase):
    def test_bounded_parallel_parts_and_cleanup(self):
        async def scenario(fail=False):
            saved={}; senders=[]; active=0; peak=0
            class Sender:
                closed=False
                async def send(self,request):
                    nonlocal active,peak
                    active+=1; peak=max(peak,active)
                    try:
                        await asyncio.sleep(.001)
                        if fail and request.file_part==2: raise ValueError('synthetic RPC failure')
                        saved[request.file_part]=request.bytes
                        return True
                    finally: active-=1
                async def disconnect(self): self.closed=True
            async def factory(_):
                sender=Sender(); senders.append(sender); return sender
            data=b'z'*(11*1024**2+17); progress=[]
            with patch('lab_backup.fast_upload.make_sender',factory):
                try:
                    output=await upload_parallel(None,io.BytesIO(data),len(data),'part.pwb',4,
                                                 lambda done,total:progress.append(done))
                    self.assertFalse(fail)
                    self.assertEqual(output.parts,(len(data)+PART_SIZE-1)//PART_SIZE)
                    self.assertEqual(b''.join(saved[i] for i in sorted(saved)),data)
                    self.assertEqual(progress[-1],len(data))
                except ExceptionGroup:
                    self.assertTrue(fail)
            self.assertTrue(all(s.closed for s in senders))
            self.assertEqual(active,0); self.assertLessEqual(peak,4); self.assertGreater(peak,1)
        asyncio.run(scenario()); asyncio.run(scenario(True))
