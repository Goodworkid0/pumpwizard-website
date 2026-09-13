"""Direct Telegram MTProto transport. No OTS service or user session required.

Channel resolution and bounded upload pattern adapted from OpenTelegramStorage.
See third_party/OpenTelegramStorage-LICENSE.txt. No OTS captions or bot metadata
are modified, and this client never receives updates or calls getUpdates.
"""
import asyncio
import json
import logging
import time
from pathlib import Path
import urllib.request

from telethon import TelegramClient
from telethon.sessions import StringSession
from telethon.tl.functions.channels import GetChannelsRequest
from telethon.tl.types import InputChannel, InputPeerChannel, DocumentAttributeFilename

from .common import BackupError, load, save
from .fast_upload import upload_parallel, BIG_FILE_THRESHOLD
from .fast_download import download_parallel


def transfer_failure(exc):
    """Only exception class names and numeric backoff; never provider URLs/tokens."""
    leaves=[]
    def visit(error):
        if isinstance(error,BaseExceptionGroup):
            for child in error.exceptions: visit(child)
        else: leaves.append(error)
    visit(exc)
    return ','.join(sorted({type(e).__name__ for e in leaves})), max(
        (int(getattr(e,'seconds',0)) for e in leaves),default=0)


class Transport:
    def __init__(self,config):
        self.config=config; self.credentials=load(config['credentials'],private=True)
        self.channel_id=int(self.credentials['channel_id']); self.client=None; self.entity=None

    def bot_api(self,method,payload):
        # Provider exceptions can contain the token-bearing URL: never propagate.
        try:
            req=urllib.request.Request('https://api.telegram.org/bot'+self.credentials['bot_token']+'/'+method,
                data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
            class NoRedirect(urllib.request.HTTPRedirectHandler):
                def redirect_request(self,*a,**kw): raise BackupError('Telegram redirect refused')
            with urllib.request.build_opener(urllib.request.ProxyHandler({}),NoRedirect()).open(req,timeout=20) as response:
                data=json.loads(response.read(262144))
                if data.get('ok') is not True: raise BackupError('Telegram API rejected request')
                return data['result']
        except Exception: raise BackupError('Telegram API request failed') from None

    async def preflight(self):
        chat=await asyncio.to_thread(self.bot_api,'getChat',{'chat_id':self.channel_id})
        if chat.get('id')!=self.channel_id or chat.get('type')!='channel': raise BackupError('Configured Telegram destination is not a channel')
        if chat.get('username') or chat.get('active_usernames'): raise BackupError('Backup channel must be private')
        # A successful Bot API ChatFullInfo omits auto_delete_time when off. This
        # is distinct from OTS's ambiguous cached null and requires a live call.
        if chat.get('message_auto_delete_time',0)!=0: raise BackupError('Channel auto-delete must be off')
        return {'private_channel':True,'auto_delete_off':True}

    async def __aenter__(self):
        await self.preflight()
        path=Path(self.config['session'])
        session=load(path,private=True) if path.exists() else {}
        log=logging.getLogger('pumpwizard.telegram'); log.disabled=True
        self.client=TelegramClient(StringSession(session.get('session','')),self.credentials['api_id'],self.credentials['api_hash'],
            receive_updates=False,request_retries=3,connection_retries=3,retry_delay=2,
            flood_sleep_threshold=30,base_logger=log,device_model='PumpWizard backup',app_version='1.0')
        try:
            await self.client.connect()
            if not await self.client.is_user_authorized(): await self.client.sign_in(bot_token=self.credentials['bot_token'])
            me=await self.client.get_me()
            if not me.bot or me.id!=int(self.credentials['bot_token'].split(':',1)[0]): raise BackupError('Unexpected Telegram identity')
            save(path,{'session':self.client.session.save(),'bot_id':me.id})
            bare=int(str(self.channel_id)[4:])
            result=await self.client(GetChannelsRequest([InputChannel(bare,0)]))
            channel=result.chats[0]
            if channel.id!=bare: raise BackupError('Telegram channel identity mismatch')
            self.entity=InputPeerChannel(channel.id,channel.access_hash)
            return self
        except Exception:
            await self.client.disconnect()
            raise BackupError('Telegram connection or channel access failed') from None

    async def __aexit__(self,*args):
        if self.client: await self.client.disconnect()

    async def upload(self,path,caption,before_send=None):
        try:
            last=[0.0]
            def progress(done,total):
                if time.monotonic()-last[0]>=30 or done==total:
                    self.progress('upload',done,total); last[0]=time.monotonic()
            for attempt in range(4):
                try:
                    with Path(path).open('rb') as stream:
                        self.progress('upload',0,Path(path).stat().st_size)
                        size=Path(path).stat().st_size
                        connections=int(self.config.get('upload_connections',4)) if attempt==0 else 1
                        operation=(upload_parallel(self.client,stream,size,Path(path).name,connections,progress)
                            if size>BIG_FILE_THRESHOLD and connections>1 else self.client.upload_file(stream,
                                file_size=size,file_name=Path(path).name,part_size_kb=512,progress_callback=progress))
                        uploaded=await asyncio.wait_for(operation,timeout=1800)
                    break
                except Exception as exc:
                    kind,delay=transfer_failure(exc)
                    print('Telegram staging upload failure: '+kind,flush=True)
                    if attempt==3 or delay>600 or any(x in kind for x in ('AuthKey','Unauthorized','Forbidden')):
                        raise
                    remaining=max(delay,5*(attempt+1))
                    while remaining>0:
                        self.progress('upload_backoff',0,size)
                        pause=min(30,remaining); await asyncio.sleep(pause); remaining-=pause
            if before_send: before_send()
            msg=await self.client.send_file(self.entity,uploaded,force_document=True,
                caption=json.dumps(caption,separators=(',',':')),parse_mode=None,
                attributes=[DocumentAttributeFilename(Path(path).name)])
            return msg.id
        except Exception as exc:
            kind,_=transfer_failure(exc)
            raise BackupError('Telegram transfer interrupted ('+kind+'); saved parts are retained') from None

    async def download(self,message_id,destination,expected_size):
        try:
            msg=await self.client.get_messages(self.entity,ids=int(message_id))
            if not msg or not msg.document or msg.document.size!=expected_size: raise BackupError('Backup message missing or size differs')
            count=0; last=0.0
            self.progress('download',0,expected_size)
            with Path(destination).open('xb') as out:
                connections=int(self.config.get('download_connections',4))
                if not 1<=connections<=8: raise BackupError('Invalid download concurrency')
                if connections>1 and expected_size>512*1024:
                    def progress(done,total):
                        nonlocal last
                        if time.monotonic()-last>=30 or done==total:
                            self.progress('download',done,total); last=time.monotonic()
                    await download_parallel(self.client,msg.document,out,expected_size,connections,progress)
                    return
                blocks=self.client.iter_download(msg.document,request_size=512*1024)
                while True:
                    try: block=await asyncio.wait_for(anext(blocks),timeout=120)
                    except StopAsyncIteration: break
                    count+=len(block)
                    if count>expected_size: raise BackupError('Oversized backup part')
                    out.write(block)
                    if time.monotonic()-last>=30 or count==expected_size:
                        self.progress('download',count,expected_size); last=time.monotonic()
                out.flush()
            if count!=expected_size: raise BackupError('Truncated backup part')
        except Exception: raise BackupError('Telegram download failed or backup part is missing') from None

    def progress(self,phase,done,total):
        save(Path(self.config['session']).parent/'progress.json',
             {'phase':phase,'updated_at':time.time(),'bytes':done,'total_bytes':total})
        print('Telegram '+phase+': '+str(done)+'/'+str(total)+' bytes',flush=True)

    async def notify(self,text):
        await asyncio.to_thread(self.bot_api,'sendMessage',{'chat_id':self.channel_id,'text':text})

    async def manifest_info(self,message_id):
        msg=await self.client.get_messages(self.entity,ids=int(message_id))
        if not msg or not msg.document: raise BackupError('Recovery manifest not found')
        try: return json.loads(msg.message)
        except ValueError: raise BackupError('Invalid manifest caption') from None
