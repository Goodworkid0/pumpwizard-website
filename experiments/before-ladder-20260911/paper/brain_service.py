"""Independent memory, market follow-up and ownership workers; never signs transactions."""
import copy
import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from .engine import canonical
from .feed import MINT,get_json,normalize
from .memory import Memory
from .ownership import Rpc,check


def dex_quotes(mints,now):
    if not mints:return []
    if len(mints)>30 or any(not MINT.fullmatch(m) for m in mints):
        raise ValueError('Invalid market lookup')
    req=urllib.request.Request('https://api.dexscreener.com/tokens/v1/solana/'+','.join(mints),headers={'Accept':'application/json','User-Agent':'PumpPaperResearch/1.0'})
    with urllib.request.urlopen(req,timeout=10) as response:
        body=response.read(4_000_001)
        if len(body)>4_000_000:raise ValueError('Oversized market response')
        pairs=json.loads(body)
    selected={}
    for p in pairs:
        try:
            mint=p['baseToken']['address']
            if mint not in mints or p['chainId']!='solana':continue
            price=float(p['priceUsd']);cap=float(p.get('fdv') or p.get('marketCap') or 0)
            if price<=0 or cap<=0:continue
            liquidity=float((p.get('liquidity') or {}).get('usd',0))
            if not all(math.isfinite(v) for v in (price,cap,liquidity)) or liquidity<0:continue
            quote={'mint':mint,'symbol':p['baseToken']['symbol'],'market_cap_usd':cap,'price_usd':price,
                'liquidity_usd':liquidity,'base_reserve':float((p.get('liquidity') or {}).get('base',0)),
                'txns_m5':(p.get('txns') or {}).get('m5',{}),'volume_h1':(p.get('volume') or {}).get('h1'),
                'dex_id':p['dexId'],'pair_address':p['pairAddress'],'observed_at':now,
                'created_at':p.get('pairCreatedAt',0)/1000,'valuation_basis':'fdv' if p.get('fdv') else 'market_cap',
                'source_timestamp_known':False}
            if mint not in selected or liquidity>selected[mint]['liquidity_usd']:selected[mint]=quote
        except (KeyError,TypeError,ValueError):continue
    return list(selected.values())


class BrainService:
    def __init__(self,root,engine):
        self.root=Path(root);self.engine=engine
        self.config=json.loads((self.root/'brain.config.json').read_text())
        self.risk_config=json.loads((self.root/'ownership.config.json').read_text())
        self.memory=Memory(self.root/'data/coin-memory.sqlite',self.config)
        self.stop=threading.Event();self.probed={};self.snapshot=None
        self.status={'market':'starting','ownership':'starting'}
        self.rpc=Rpc(self.risk_config)
        self.runner=None
        from .runner import Runner
        self.runner=Runner(self.memory,self.config)
        self.threads=[threading.Thread(target=self._market_loop,daemon=True),threading.Thread(target=self._risk_loop,daemon=True)]
        for thread in self.threads:thread.start()

    def close(self):
        self.stop.set()
        # Workers may be awaiting a bounded RPC. Daemon shutdown leaves SQLite's committed state intact.
        for thread in self.threads:thread.join(timeout=1)
        if not any(t.is_alive() for t in self.threads):self.memory.close()

    def annotate(self,batch):
        with self.memory.lock:
            for snap in batch['snapshots']:
                gate=self.memory.ownership(snap['mint'],batch['at'])
                snap['entry_guard']={k:gate.get(k) for k in ('mint','checked_at','policy_hash','status')}
        return batch

    def view(self):
        result=copy.deepcopy(self.snapshot) if self.snapshot else {'coins':[],'tracked_count':0,'persistent_count':0,'candidate_count':0,'blocked_count':0,'unknown_count':0,'milestones':[],'as_of':None,'config':self.config,'coverage':'Loading recorded coin history'}
        result['service']=dict(self.status)
        return result

    def _market_loop(self):
        while not self.stop.is_set():
            try:
                with self.memory.lock,self.memory.db:
                    self.memory.import_ledgers(self.root/'data')
                    rows=[json.loads(r[0]) for r in self.memory.db.execute('SELECT data FROM coins')]
                    held=self.runner.held_mints()
                    self.snapshot=self.memory.view(time.time())
                    self.snapshot['runner']=self.runner.report()
                now=time.time()
                # Held positions first, then the least-recently probed names: no survivor-only deletion.
                rows.sort(key=lambda c:(c['mint'] not in held,self.probed.get(c['mint'],0),-c['peak_cap']))
                targets=[c['mint'] for c in rows[:self.config['probe_limit']]]
                quotes=dex_quotes(targets,time.time())
                found={q['mint'] for q in quotes}
                if targets and not quotes:self.status['market']='No follow-up quotes returned; history retained'
                else:self.status['market']='Live follow-up connected'
                # Pump fallback keeps following pre-graduation coins when DEX indexing has no pair yet.
                missing=[m for m in targets if m not in found][:3]
                if missing:
                    fx=get_json('/sol-price')
                    for mint in missing:
                        try:
                            raw=get_json('/coins/'+mint);quotes.append(normalize(raw,fx,time.time()))
                        except Exception:pass
                with self.memory.lock,self.memory.db:
                    for q in quotes:
                        source='dex' if 'dex_id' in q else 'pump'
                        self.memory.observe(q,q['observed_at'],source,live=True)
                    for mint in targets:self.probed[mint]=now
                    # Only this prospective loop can schedule/fill a longer-hold position.
                    self.runner.step(quotes,time.time())
                    self.snapshot=self.memory.view(time.time())
                    self.snapshot['runner']=self.runner.report()
            except Exception as e:
                detail='HTTP '+str(e.code) if isinstance(e,urllib.error.HTTPError) else type(e).__name__
                self.status['market']='Market follow-up unavailable ('+detail+'); recorded history retained'
            self.stop.wait(self.config['refresh_seconds'])

    def _risk_loop(self):
        while not self.stop.is_set():
            try:
                with self.engine.lock:
                    snapshots=copy.deepcopy(self.engine.state['snapshots'])
                    candidates=copy.deepcopy(self.engine.state['candidates'])
                now=time.time()
                choices=[]
                with self.memory.lock:
                    for mint,s in snapshots.items():
                        if now-s['observed_at']>30 or not 10000<=s.get('market_cap_usd',0)<=250000:continue
                        old=self.memory.ownership(mint,now)
                        due=now-old.get('checked_at',0)>self.risk_config['check_ttl_seconds']
                        if due:choices.append((candidates.get(mint,{}).get('reason') not in ('ownership_unverified','ownership_blocked'),old.get('checked_at',0),mint))
                    # Longer-hold candidates remain eligible for checks after leaving the scalp band.
                    for row in self.memory.db.execute('SELECT mint,data FROM coins'):
                        c=json.loads(row['data']);old=self.memory.ownership(row['mint'],now)
                        if now-c['last_at']<180 and now-old.get('checked_at',0)>120:
                            choices.append((True,old.get('checked_at',0),row['mint']))
                if choices:
                    mint=sorted(choices)[0][2]
                    result=check(mint,self.risk_config,self.rpc)
                    with self.memory.lock,self.memory.db:self.memory.store_ownership(result)
                    self.status['ownership']='Screen passed for '+mint[:6] if result['status']=='clear' else '; '.join(result['reasons'][:2])
                    if result['status']=='unknown' and any('RPC' in x for x in result['reasons']):
                        self.stop.wait(60)
                else:self.status['ownership']='Waiting for fresh candidates or scheduled rechecks'
            except Exception as e:
                self.status['ownership']='Ownership checks unavailable ('+type(e).__name__+')'
            self.stop.wait(5)
