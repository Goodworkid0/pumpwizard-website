"""Persistent coin evidence across experiments; repeated polls are not distinct trades."""
import hashlib
import json
import math
import sqlite3
import threading
import time
import zlib
from collections import defaultdict
from pathlib import Path

from .engine import canonical

MILESTONES=(10000,20000,50000,100000,250000,1000000,10000000,100000000)


class Memory:
    def __init__(self,path,config):
        self.config=config
        self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS coins(mint TEXT PRIMARY KEY,data TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bars(mint TEXT,bucket INTEGER,first REAL,last REAL,low REAL,high REAL,changes INTEGER,PRIMARY KEY(mint,bucket));
            CREATE TABLE IF NOT EXISTS milestones(mint TEXT,threshold INTEGER,at REAL,cap REAL,PRIMARY KEY(mint,threshold));
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY,at REAL);
            CREATE TABLE IF NOT EXISTS trades(ledger TEXT,id INTEGER,mint TEXT,data TEXT,PRIMARY KEY(ledger,id));
            CREATE TABLE IF NOT EXISTS ownership(mint TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS ownership_audit(id INTEGER PRIMARY KEY,mint TEXT,at REAL,data TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        ''')

    def close(self):
        self.db.close()

    def get(self,mint):
        row=self.db.execute('SELECT data FROM coins WHERE mint=?',(mint,)).fetchone()
        return json.loads(row[0]) if row else None

    def observe(self,s,at,source='pump',live=False):
        mint=s['mint'];cap=s.get('market_cap_usd');price=s.get('price_usd')
        if price is None and s.get('x') and s.get('y'):
            price=s['y']/s['x']*s['quote_usd']
        if not all(isinstance(v,(int,float)) and math.isfinite(v) and v>0 for v in (cap,price)):
            return False
        if source=='pump' and (s.get('validation_error') or at-s.get('source_at',0)>30 or s.get('source_at',0)>at+2):
            return False
        old=self.get(mint)
        if old and at<=old['last_at']:
            return False
        complete=s.get('complete',False)
        if old and old.get('phase')=='graduated' and source=='pump':
            return False # Completed curve snapshots must not overwrite AMM observations.
        signature=hashlib.sha256(canonical([round(cap,5),round(price,14),s.get('real_y'),s.get('liquidity_usd'),s.get('txns_m5')]).encode()).hexdigest()
        # Source switches don't count as new activity by themselves.
        changed=bool(old and old['signature']!=signature and old['source']==source)
        c=old or {'mint':mint,'first_at':at,'first_cap':cap,'peak_cap':cap,'seen':0,'changes':0,'last_change':None,'first_source':source,'created_at':s.get('created_at')}
        c.update(symbol=str(s.get('symbol',mint[:8]))[:80],last_at=at,last_cap=cap,
                 peak_cap=max(c['peak_cap'],cap),seen=c['seen']+1,changes=c['changes']+int(changed),
                 signature=signature,source=source,phase='graduated' if source=='dex' and s.get('dex_id')!='pumpfun' else 'migration_pending' if complete else 'bonding_curve',
                 quote=s,price_usd=price)
        if changed:c['last_change']=at
        if s.get('curve_address'):c['curve_address']=s['curve_address']
        self.db.execute('INSERT OR REPLACE INTO coins VALUES (?,?)',(mint,canonical(c)))
        bucket=int(at//300)
        self.db.execute('''INSERT INTO bars VALUES (?,?,?,?,?,?,?) ON CONFLICT(mint,bucket) DO UPDATE SET
            last=excluded.last,low=min(low,excluded.low),high=max(high,excluded.high),changes=changes+excluded.changes''',
            (mint,bucket,cap,cap,cap,cap,int(changed)))
        # First observation above a threshold is evidence of crossing by that time, not an exact crossing price.
        for threshold in MILESTONES:
            if cap>=threshold:
                self.db.execute('INSERT OR IGNORE INTO milestones VALUES (?,?,?,?)',(mint,threshold,at,cap))
        return True

    def import_ledgers(self,directory):
        pending=[]
        for name in self.config['history_files']:
            path=Path(directory)/name
            if not path.exists():continue
            con=sqlite3.connect(f'file:{path}?mode=ro',uri=True)
            try:
                contract=json.loads(con.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
                if contract['source']!='public_index_live':continue
                prior=self.db.execute('SELECT at FROM cursors WHERE path=?',(name,)).fetchone()
                since=prior[0] if prior else -1
                for ident,at,payload in con.execute('SELECT id,at,payload FROM batches WHERE at>? ORDER BY at',(since,)):
                    pending.append((at,name,ident,payload))
                for ident,mint,data in con.execute('SELECT id,mint,data FROM trades'):
                    self.db.execute('INSERT OR IGNORE INTO trades VALUES (?,?,?,?)',(name,ident,mint,data))
            finally:con.close()
        for at,name,ident,payload in sorted(pending):
            if not self.db.execute('SELECT 1 FROM batches WHERE id=?',(ident,)).fetchone():
                batch=json.loads(zlib.decompress(payload))
                for snap in batch['snapshots']:
                    self.observe(snap,batch['at'])
                self.db.execute('INSERT INTO batches VALUES (?)',(ident,))
            self.db.execute('INSERT OR REPLACE INTO cursors VALUES (?,?)',(name,at))
        return len(pending)

    def store_ownership(self,result):
        self.db.execute('INSERT INTO ownership_audit(mint,at,data) VALUES (?,?,?)',(result['mint'],result['checked_at'],canonical(result)))
        old=self.db.execute('SELECT data FROM ownership WHERE mint=?',(result['mint'],)).fetchone()
        old=json.loads(old[0]) if old else None
        if result['status']=='unknown' and old and old['status']=='blocked':
            result=dict(result,status='blocked',last_flagged_at=old.get('last_flagged_at',old['checked_at']),
                reasons=list(dict.fromkeys(['Previous ownership flags remain unresolved; the latest check is unavailable']+old.get('reasons',[])+result.get('reasons',[]))))
        self.db.execute('INSERT OR REPLACE INTO ownership VALUES (?,?)',(result['mint'],canonical(result)))

    def ownership(self,mint,now):
        row=self.db.execute('SELECT data FROM ownership WHERE mint=?',(mint,)).fetchone()
        if not row:return {'mint':mint,'status':'unknown','reasons':['Ownership check pending']}
        result=json.loads(row[0])
        if now-result['checked_at']>120:
            result=dict(result,status='blocked' if result['status']=='blocked' else 'unknown',expired=True,
                        reasons=['Ownership check expired; previous flags remain unresolved' if result['status']=='blocked' else 'Ownership check expired']+result.get('reasons',[]))
        return result

    def search(self,query,now):
        query=str(query).strip().casefold()[:100]
        with self.lock:
            matches=[]
            for row in self.db.execute('SELECT data FROM coins'):
                c=json.loads(row[0])
                if query in (c['symbol']+' '+c['mint']).casefold():matches.append(c)
            matches.sort(key=lambda c:c['last_at'],reverse=True)
            result=[]
            for c in matches[:50]:
                c=self.evaluate(c,now)
                trades=[json.loads(t[0]) for t in self.db.execute('SELECT data FROM trades WHERE mint=?',(c['mint'],))]
                c['our_trades']={'count':len(trades),'pnl_usd':sum(t['pnl_usd'] for t in trades)}
                result.append(c)
            return result

    def evaluate(self,c,now):
        bars=[dict(b) for b in self.db.execute('SELECT * FROM bars WHERE mint=? ORDER BY bucket',(c['mint'],))]
        active=[b for b in bars if b['changes']>0]
        active_hours=len({b['bucket']//12 for b in active})
        age=now-c['first_at'];fresh=now-c['last_at']<=180
        growth=c['last_cap']/c['first_cap']-1;drawdown=1-c['last_cap']/c['peak_cap']
        recent=[b for b in bars if b['bucket']*300>=now-3600]
        trend=len(recent)>=3 and recent[-1]['last']>recent[0]['last'] and recent[-1]['low']>=min(b['low'] for b in recent[:-1])
        ownership=self.ownership(c['mint'],now)
        reasons=[]
        if not fresh:reasons.append('Awaiting fresh market evidence')
        if age<self.config['minimum_history_hours']*3600:reasons.append('Needs at least 6 hours of recorded history')
        if len(active)<self.config['minimum_active_buckets']:reasons.append('Needs activity in at least 12 separate five-minute windows')
        if active_hours<3:reasons.append('Needs changed market data across at least three distinct hours')
        if growth<self.config['minimum_growth_fraction']:reasons.append('Growth from first observation is below 50%')
        if drawdown>self.config['maximum_drawdown_fraction']:reasons.append('More than 35% below the observed peak')
        if not trend:reasons.append('No sustained recent price structure yet')
        q=c['quote'];txns=q.get('txns_m5') or {}
        if sum(txns.get(k,0) for k in ('buys','sells'))<20:reasons.append('Recent external trading activity is insufficient or unavailable')
        if not q.get('liquidity_usd') or q['liquidity_usd']<self.config['runner_min_liquidity_usd']:reasons.append('The six-hour growth thesis requires $10k of quoted AMM liquidity')
        if not 10000<=c['last_cap']<=250000:reasons.append('Outside the $10k–$250k growth-thesis range')
        if ownership['status']!='clear':reasons.append('Ownership screen blocked' if ownership['status']=='blocked' else 'Ownership evidence incomplete')
        recent_change=c.get('last_change') is not None and now-c['last_change']<1800
        stage='runner_candidate' if not reasons else 'blocked' if ownership['status']=='blocked' else 'dormant' if not fresh else 'quiet' if not recent_change and not sum(txns.get(k,0) for k in ('buys','sells')) else 'persistent' if len(active)>=3 else 'observing'
        return dict(c,stage=stage,reasons=reasons,ownership=ownership,active_windows=len(active),
                    history_hours=(c['last_at']-c['first_at'])/3600,growth_fraction=growth,drawdown_fraction=drawdown,
                    milestones=[dict(r) for r in self.db.execute('SELECT threshold,at,cap FROM milestones WHERE mint=? ORDER BY threshold',(c['mint'],))],
                    curve=[{'at':b['bucket']*300,'cap':b['last']} for b in bars[-288:]])

    def view(self,now=None):
        now=time.time() if now is None else now
        with self.lock:
            rows=[self.evaluate(json.loads(r[0]),now) for r in self.db.execute('SELECT data FROM coins')]
            trades=defaultdict(lambda:{'count':0,'pnl_usd':0})
            for r in self.db.execute('SELECT mint,data FROM trades'):
                t=json.loads(r['data']);trades[r['mint']]['count']+=1;trades[r['mint']]['pnl_usd']+=t['pnl_usd']
            for c in rows:c['our_trades']=trades[c['mint']]
            rows.sort(key=lambda c:(c['stage']=='runner_candidate',c['stage']=='persistent',c['last_at'],c['growth_fraction']),reverse=True)
            milestones=[dict(r) for r in self.db.execute('SELECT mint,threshold,at,cap FROM milestones WHERE threshold>=20000 ORDER BY at DESC LIMIT 20')]
            return {'coins':rows[:100],'tracked_count':len(rows),'persistent_count':sum(c['active_windows']>=3 for c in rows),
                    'candidate_count':sum(c['stage']=='runner_candidate' for c in rows),
                    'blocked_count':sum(c['ownership']['status']=='blocked' for c in rows),
                    'unknown_count':sum(c['ownership']['status']=='unknown' for c in rows),'milestones':milestones,
                    'as_of':max((c['last_at'] for c in rows),default=None),'config':self.config,
                    'coverage':'Observed coins only. Poll counts and our own trades are not independent market demand. Scores do not predict a $100m outcome.'}
