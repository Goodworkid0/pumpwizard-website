"""Persistent coin evidence across experiments; repeated polls are not distinct trades."""
import hashlib
import csv
import io
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
OUTCOME_WINDOWS=(300,900,3600,21600,86400,604800,2592000)


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
            CREATE TABLE IF NOT EXISTS research_bars(
                mint TEXT,bucket INTEGER,first_at REAL,last_at REAL,
                open_price REAL,close_price REAL,low_price REAL,high_price REAL,
                open_cap REAL,close_cap REAL,low_cap REAL,high_cap REAL,
                liquidity_usd REAL,buys INTEGER,sells INTEGER,volume_h1 REAL,source TEXT,samples INTEGER,
                PRIMARY KEY(mint,bucket));
            CREATE TABLE IF NOT EXISTS research_events(
                strategy_id TEXT,event_id INTEGER,mint TEXT,kind TEXT,at REAL,
                reference_price REAL,reference_cap REAL,data TEXT,
                PRIMARY KEY(strategy_id,event_id));
            CREATE TABLE IF NOT EXISTS research_outcomes(
                strategy_id TEXT,event_id INTEGER,window_seconds INTEGER,mint TEXT,kind TEXT,due_at REAL,
                reference_price REAL,reference_cap REAL,observed_at REAL,price_usd REAL,market_cap_usd REAL,
                liquidity_usd REAL,return_fraction REAL,cap_change_fraction REAL,
                PRIMARY KEY(strategy_id,event_id,window_seconds));
            CREATE TABLE IF NOT EXISTS milestones(mint TEXT,threshold INTEGER,at REAL,cap REAL,PRIMARY KEY(mint,threshold));
            CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY);
            CREATE TABLE IF NOT EXISTS cursors(path TEXT PRIMARY KEY,at REAL);
            CREATE TABLE IF NOT EXISTS trades(ledger TEXT,id INTEGER,mint TEXT,data TEXT,PRIMARY KEY(ledger,id));
            CREATE TABLE IF NOT EXISTS ownership(mint TEXT PRIMARY KEY,data TEXT);
            CREATE TABLE IF NOT EXISTS ownership_audit(id INTEGER PRIMARY KEY,mint TEXT,at REAL,data TEXT);
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS research_migrations(mint TEXT PRIMARY KEY,at REAL);
        ''')
        columns={row['name'] for row in self.db.execute('PRAGMA table_info(research_outcomes)')}
        with self.db:
            if 'quality' not in columns:
                self.db.execute("ALTER TABLE research_outcomes ADD COLUMN quality TEXT NOT NULL DEFAULT 'pending'")
                self.db.execute('ALTER TABLE research_outcomes ADD COLUMN source TEXT')
                self.db.execute('ALTER TABLE research_outcomes ADD COLUMN source_timestamp_known INTEGER')
                # Keep historical values intact, but distinguish delayed captures.
                self.db.execute("""UPDATE research_outcomes SET quality=CASE
                    WHEN observed_at IS NULL THEN 'pending'
                    WHEN observed_at-due_at<=max(60,min(3600,window_seconds*.1)) THEN 'on_time'
                    ELSE 'late' END""")
            self.db.executescript('''CREATE INDEX IF NOT EXISTS outcome_due ON research_outcomes(quality,due_at);
                CREATE INDEX IF NOT EXISTS outcome_mint ON research_outcomes(mint,observed_at,due_at);
                CREATE TABLE IF NOT EXISTS research_probes(mint TEXT PRIMARY KEY,next_at REAL,attempts INTEGER);''')

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
        # Preserve a compact, source-attributed one-minute research series. This
        # is intentionally separate from the five-minute decision bars above.
        research_bucket=int(at//60);txns=s.get('txns_m5') or {}
        liquidity=s.get('liquidity_usd');volume=s.get('volume_h1')
        liquidity=liquidity if isinstance(liquidity,(int,float)) and math.isfinite(liquidity) and liquidity>=0 else None
        volume=volume if isinstance(volume,(int,float)) and math.isfinite(volume) and volume>=0 else None
        buys=txns.get('buys');sells=txns.get('sells')
        buys=buys if isinstance(buys,int) and buys>=0 else None
        sells=sells if isinstance(sells,int) and sells>=0 else None
        self.db.execute('''INSERT INTO research_bars VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(mint,bucket) DO UPDATE SET last_at=excluded.last_at,
            close_price=excluded.close_price,low_price=min(low_price,excluded.low_price),high_price=max(high_price,excluded.high_price),
            close_cap=excluded.close_cap,low_cap=min(low_cap,excluded.low_cap),high_cap=max(high_cap,excluded.high_cap),
            liquidity_usd=excluded.liquidity_usd,buys=excluded.buys,sells=excluded.sells,volume_h1=excluded.volume_h1,
            source=excluded.source,samples=samples+1''',
            (mint,research_bucket,at,at,price,price,price,price,cap,cap,cap,cap,liquidity,buys,sells,volume,source,1))
        self._capture_due_outcomes(mint,dict(s,price_usd=price),at)
        # First observation above a threshold is evidence of crossing by that time, not an exact crossing price.
        for threshold in MILESTONES:
            if cap>=threshold:
                self.db.execute('INSERT OR IGNORE INTO milestones VALUES (?,?,?,?)',(mint,threshold,at,cap))
        return True

    def _capture_due_outcomes(self,mint,quote,at):
        """Fill due outcome windows with the first recorded quote at/after due time."""
        price=quote.get('price_usd');cap=quote.get('market_cap_usd')
        if not all(isinstance(value,(int,float)) and math.isfinite(value) and value>0 for value in (price,cap)):
            return
        liquidity=quote.get('liquidity_usd')
        if not isinstance(liquidity,(int,float)) or not math.isfinite(liquidity) or liquidity<0:liquidity=None
        rows=self.db.execute('''SELECT strategy_id,event_id,window_seconds,due_at,reference_price,reference_cap
            FROM research_outcomes WHERE mint=? AND observed_at IS NULL AND due_at<=?''',(mint,at)).fetchall()
        for row in rows:
            reference_price,reference_cap=row['reference_price'],row['reference_cap']
            price_return=price/reference_price-1 if reference_price else None
            cap_return=cap/reference_cap-1 if reference_cap else None
            quality='on_time' if at-row['due_at']<=max(60,min(3600,row['window_seconds']*.1)) else 'late'
            source=quote.get('source') or quote.get('dex_id') or quote.get('route')
            known=int(quote.get('source_timestamp_known',quote.get('source_at') is not None))
            self.db.execute('''UPDATE research_outcomes SET observed_at=?,price_usd=?,market_cap_usd=?,liquidity_usd=?,
                return_fraction=?,cap_change_fraction=?,quality=?,source=?,source_timestamp_known=?
                WHERE strategy_id=? AND event_id=? AND window_seconds=?''',
                (at,price,cap,liquidity,price_return,cap_return,quality,source,known,row['strategy_id'],row['event_id'],row['window_seconds']))

    def ingest_strategy_events(self,strategy_id,runner):
        """Persist immutable entry/exit markers and schedule comparable outcome windows."""
        with self.lock,runner.lock,self.db:
            cursor_key='research_cursor:'+strategy_id
            cursor=self.db.execute('SELECT value FROM meta WHERE key=?',(cursor_key,)).fetchone()
            since=int(cursor[0]) if cursor else 0
            events=runner.db.execute('''SELECT id,at,mint,action,data FROM events
                WHERE id>? AND action IN ('fill_entry','fill_partial_exit','fill_exit') ORDER BY id''',(since,)).fetchall()
            for event in events:
                payload=json.loads(event['data']);kind=event['action']
                if kind=='fill_entry':
                    reference_price=payload.get('entry_price_usd');reference_cap=payload.get('entry_cap')
                else:
                    reference_price=payload.get('exit_price_usd');reference_cap=payload.get('exit_cap')
                if not all(isinstance(value,(int,float)) and math.isfinite(value) and value>0 for value in (reference_price,reference_cap)):
                    continue
                inserted=self.db.execute('''INSERT OR IGNORE INTO research_events
                    VALUES (?,?,?,?,?,?,?,?)''',(strategy_id,event['id'],event['mint'],kind,event['at'],reference_price,
                    reference_cap,canonical(payload))).rowcount
                if inserted:
                    for window in OUTCOME_WINDOWS:
                        self.db.execute('''INSERT OR IGNORE INTO research_outcomes
                            (strategy_id,event_id,window_seconds,mint,kind,due_at,reference_price,reference_cap,
                             observed_at,price_usd,market_cap_usd,liquidity_usd,return_fraction,cap_change_fraction)
                            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',(strategy_id,event['id'],window,event['mint'],kind,
                            event['at']+window,reference_price,reference_cap,None,None,None,None,None,None))
            if events:self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(cursor_key,str(events[-1]['id'])))

    def due_outcome_mints(self,now,limit=12):
        with self.lock:
            rows=self.db.execute('''SELECT o.mint,min(o.due_at) AS due_at FROM research_outcomes o
                LEFT JOIN research_probes p ON p.mint=o.mint
                WHERE o.quality='pending' AND o.due_at<=? AND coalesce(p.next_at,0)<=?
                AND o.due_at+max(60,min(3600,o.window_seconds*.1))>=?
                GROUP BY o.mint ORDER BY due_at LIMIT ?''',(now,now,now,limit)).fetchall()
            return [row['mint'] for row in rows]

    def finish_research_probes(self,targets,found,now):
        with self.lock,self.db:
            self.db.execute("""UPDATE research_outcomes SET quality='missed' WHERE quality='pending'
                AND observed_at IS NULL AND due_at+max(60,min(3600,window_seconds*.1))<?""",(now,))
            for mint in targets:
                prior=self.db.execute('SELECT attempts FROM research_probes WHERE mint=?',(mint,)).fetchone()
                attempts=0 if mint in found else min(6,(prior[0] if prior else 0)+1)
                self.db.execute('INSERT OR REPLACE INTO research_probes VALUES (?,?,?)',
                    (mint,now+(30 if not attempts else min(900,30*2**attempts)),attempts))

    def research_view(self):
        with self.lock:
            tracked_coins=self.db.execute('SELECT count(*) FROM coins').fetchone()[0]
            total_bars=self.db.execute('SELECT count(*) FROM research_bars').fetchone()[0]
            total_events=self.db.execute('SELECT count(*) FROM research_events').fetchone()[0]
            total_outcomes=self.db.execute('SELECT count(*) FROM research_outcomes').fetchone()[0]
            completed=self.db.execute("SELECT count(*) FROM research_outcomes WHERE quality='on_time'").fetchone()[0]
            late=self.db.execute("SELECT count(*) FROM research_outcomes WHERE quality='late'").fetchone()[0]
            missed=self.db.execute("SELECT count(*) FROM research_outcomes WHERE quality='missed'").fetchone()[0]
            windows=[]
            for row in self.db.execute('''SELECT window_seconds,count(*) AS scheduled,
                sum(quality='on_time') AS completed,sum(quality='late') AS late,sum(quality='missed') AS missed
                FROM research_outcomes GROUP BY window_seconds ORDER BY window_seconds'''):
                windows.append(dict(row))
            recent=[]
            for row in self.db.execute('''SELECT strategy_id,event_id,mint,kind,at,reference_cap FROM research_events
                ORDER BY at DESC LIMIT 20'''):
                recent.append(dict(row))
            return {'tracked_coins':tracked_coins,'minute_bars':total_bars,'lifecycle_events':total_events,'outcomes_scheduled':total_outcomes,
                    'outcomes_completed':completed,'outcome_windows':windows,'recent_events':recent,
                    'outcomes_late':late,'outcomes_missed':missed,
                    'outcome_mints_due':len(self.due_outcome_mints(time.time(),1000))}

    def export_research_csv(self):
        """Export normalized bars, lifecycle markers, and outcome observations."""
        fields=('record_type','strategy_id','event_id','mint','kind','at','bucket','window_seconds','due_at',
                'observed_at','reference_price_usd','reference_market_cap_usd','price_usd','market_cap_usd',
                'liquidity_usd','return_fraction','cap_change_fraction','open_price_usd','close_price_usd',
                'low_price_usd','high_price_usd','open_market_cap_usd','close_market_cap_usd',
                'low_market_cap_usd','high_market_cap_usd','buys_m5','sells_m5','volume_h1_usd','source','samples','payload',
                'quality','lateness_seconds','source_timestamp_known')
        out=io.StringIO();writer=csv.DictWriter(out,fields);writer.writeheader()
        def write(row):
            for key,value in row.items():
                if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):row[key]="'"+value
            writer.writerow(row)
        with self.lock:
            for row in self.db.execute('SELECT * FROM research_bars ORDER BY bucket,mint'):
                item=dict(row);write({'record_type':'market_bar_1m','mint':item['mint'],'at':item['last_at'],'bucket':item['bucket'],
                    'open_price_usd':item['open_price'],'close_price_usd':item['close_price'],'low_price_usd':item['low_price'],
                    'high_price_usd':item['high_price'],'open_market_cap_usd':item['open_cap'],'close_market_cap_usd':item['close_cap'],
                    'low_market_cap_usd':item['low_cap'],'high_market_cap_usd':item['high_cap'],'liquidity_usd':item['liquidity_usd'],
                    'buys_m5':item['buys'],'sells_m5':item['sells'],'volume_h1_usd':item['volume_h1'],'source':item['source'],'samples':item['samples']})
            for row in self.db.execute('SELECT * FROM research_events ORDER BY at,strategy_id,event_id'):
                item=dict(row);write({'record_type':'lifecycle_event','strategy_id':item['strategy_id'],'event_id':item['event_id'],
                    'mint':item['mint'],'kind':item['kind'],'at':item['at'],'reference_price_usd':item['reference_price'],
                    'reference_market_cap_usd':item['reference_cap'],'payload':item['data']})
            for row in self.db.execute('SELECT * FROM research_outcomes ORDER BY due_at,strategy_id,event_id,window_seconds'):
                item=dict(row);write({'record_type':'forward_outcome','strategy_id':item['strategy_id'],'event_id':item['event_id'],
                    'mint':item['mint'],'kind':item['kind'],'window_seconds':item['window_seconds'],'due_at':item['due_at'],
                    'observed_at':item['observed_at'],'reference_price_usd':item['reference_price'],
                    'reference_market_cap_usd':item['reference_cap'],'price_usd':item['price_usd'],
                    'market_cap_usd':item['market_cap_usd'],'liquidity_usd':item['liquidity_usd'],
                    'return_fraction':item['return_fraction'],'cap_change_fraction':item['cap_change_fraction'],
                    'quality':item['quality'],'lateness_seconds':item['observed_at']-item['due_at'] if item['observed_at'] is not None else None,
                    'source':item['source'],'source_timestamp_known':item['source_timestamp_known']})
        return out.getvalue()

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
            tracking_started_at=min((c['first_at'] for c in rows),default=None)
            return {'coins':rows[:100],'tracked_count':len(rows),'persistent_count':sum(c['active_windows']>=3 for c in rows),
                    'candidate_count':sum(c['stage']=='runner_candidate' for c in rows),
                    'blocked_count':sum(c['ownership']['status']=='blocked' for c in rows),
                    'unknown_count':sum(c['ownership']['status']=='unknown' for c in rows),'milestones':milestones,
                    'as_of':max((c['last_at'] for c in rows),default=None),'tracking_started_at':tracking_started_at,'config':self.config,
                    'coverage':'Observed coins only. Poll counts and our own trades are not independent market demand. Scores do not predict a $100m outcome.'}
