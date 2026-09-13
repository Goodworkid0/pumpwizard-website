"""One observation archive and one feature calculation per coin/window for all pilots."""
import hashlib
import json
import sqlite3
import threading
import zlib
from collections import defaultdict

from .engine import canonical
from .experiment_catalog import BANDS, WINDOWS


def summarize(points, seconds, now):
    """Only observed samples, same pool/route, with explicit gap and age checks."""
    if not points or now-points[-1]['at']>120:
        return None
    end=points[-1]['at'];prior=[i for i,p in enumerate(points) if p['at']<=end-seconds]
    if not prior:
        return None
    rows=points[prior[-1]:]
    if len(rows)<6 or max(b['at']-a['at'] for a,b in zip(rows,rows[1:]))>180:
        return None
    values=[p['price'] for p in rows];n=len(values)
    if sum(a!=b for a,b in zip(values,values[1:]))<3:
        return None
    third=max(1,n//3);early=values[:third];middle=values[third:2*third];late=values[2*third:]
    half=values[n//2];quarter=values[3*n//4]
    growth=values[-1]/values[0]-1
    liquid_growth=rows[-1]['liquidity']/rows[0]['liquidity']-1 if rows[0]['liquidity']>0 else None
    peak=max(values);trough=min(values[third:]);prior_high=max(early)
    # A retest must come after the breakout; the current bar cannot supply both.
    breakout=next((i for i in range(third,n-2) if values[i]>prior_high*1.01),None)
    retest=breakout is not None and any(prior_high*.97<=v<=prior_high*1.02 for v in values[breakout+1:-1])
    return {'growth':growth,'first_half':half/values[0]-1,'second_half':values[-1]/half-1,
        'recent_quarter':values[-1]/quarter-1,'drawdown':1-values[-1]/peak,
        'higher_lows':min(early)<min(middle)<min(late) and values[-1]>min(late)*1.02,
        'squeeze':max(middle)/min(middle)-1<=.05 and values[-1]>max(middle)*1.02,
        'retest':retest and values[-1]>prior_high*1.02,
        'recovery':1-trough/max(early)>=.12 and max(late[:-1] or late)/min(late[:-1] or late)-1<=.08 and values[-1]>trough*1.04,
        'liquidity_growth':liquid_growth,'samples':n,'start_at':rows[0]['at'],'end_at':end,
        'route':rows[-1]['route'],'pool':rows[-1]['pool'],'peer_count':0,'percentile':None}


class SharedResearch:
    def __init__(self,path):
        self.lock=threading.RLock()
        self.db=sqlite3.connect(path,check_same_thread=False)
        self.db.row_factory=sqlite3.Row
        self.db.executescript('''PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS inputs(id TEXT PRIMARY KEY,at REAL,payload BLOB);
            CREATE INDEX IF NOT EXISTS shared_input_at ON inputs(at);
            CREATE TABLE IF NOT EXISTS samples(mint TEXT,bucket INTEGER,at REAL,data TEXT,PRIMARY KEY(mint,bucket));
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);''')
        self.histories=defaultdict(list)
        last=self.db.execute('SELECT max(at) FROM samples').fetchone()[0]
        if last is not None:
            for row in self.db.execute('SELECT mint,data FROM samples WHERE at>=? ORDER BY at',(last-max(WINDOWS)-180,)):
                point=json.loads(row['data']);history=self.histories[row['mint']]
                if history and ((history[-1]['route'],history[-1]['pool'])!=(point['route'],point['pool']) or point['at']-history[-1]['at']>180):
                    history.clear()
                history.append(point)
        self.features={};self.last_input_id=None;self.last_at=None

    def close(self):
        self.db.close()

    def prepare(self,batch,market):
        with self.lock,self.db:
            now=batch['at'];self.features={};markets={}
            for q in batch['quotes']:
                m=market(q,now)
                if not m:continue
                mint=q['mint'];markets[mint]=m
                # Curve trends use SOL units so FX changes cannot create a signal.
                p={'at':m['observed_at'],'price':q['y']/q['x'] if m['route']=='curve' else m['price'],
                    'liquidity':q['real_y'] if m['route']=='curve' else m['real'],
                    'route':m['route'],'pool':m['pool']}
                h=self.histories[mint]
                if h and p['at']<=h[-1]['at']:continue
                if h and ((h[-1]['route'],h[-1]['pool'])!=(p['route'],p['pool']) or p['at']-h[-1]['at']>180):
                    h.clear()
                # Retain the first sample per 30-second bucket plus the most recent
                # quote. All executions still receive the original raw quote.
                if len(h)>1 and int(h[-1]['at']//30)==int(p['at']//30) and int(h[-2]['at']//30)==int(p['at']//30):
                    h[-1]=p
                else:h.append(p)
                h[:]=[x for x in h if x['at']>=now-max(WINDOWS)-180]
                self.db.execute('INSERT OR IGNORE INTO samples VALUES (?,?,?,?)',
                    (mint,int(p['at']//30),p['at'],canonical(p)))
            self.histories=defaultdict(list,{m:h for m,h in self.histories.items() if h and now-h[-1]['at']<=max(WINDOWS)+180})
            for mint,m in markets.items():
                for window in WINDOWS:
                    f=summarize(self.histories[mint],window,now)
                    if f:self.features[(mint,window)]=f
            for low,high in BANDS:
                for window in WINDOWS:
                    peers=[(mint,self.features[(mint,window)]) for mint,m in markets.items()
                           if low<=m['cap']<high and (mint,window) in self.features]
                    for mint,f in peers:
                        f['peer_count']=len(peers)
                        f['percentile']=sum(p['growth']<f['growth'] for _,p in peers)/max(1,len(peers)-1)
            feature_rows=[{'mint':mint,'window':window,**value} for (mint,window),value in self.features.items()]
            payload=canonical(dict(batch,features=feature_rows))
            ident=hashlib.sha256(payload.encode()).hexdigest()
            self.db.execute('INSERT OR IGNORE INTO inputs VALUES (?,?,?)',(ident,now,zlib.compress(payload.encode())))
            self.last_input_id=ident;self.last_at=now
            return ident

    def read(self,ident):
        with self.lock:
            row=self.db.execute('SELECT payload FROM inputs WHERE id=?',(ident,)).fetchone()
            if not row:raise ValueError('Missing shared research input')
            return json.loads(zlib.decompress(row[0]))
