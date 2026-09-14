"""Independent memory, market follow-up and ownership workers; never signs transactions."""
import copy
import csv
import hashlib
import io
import json
import math
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from .engine import canonical
from .feed import MINT,get_json,normalize
from .memory import Memory
from .ownership import Rpc,check
from .experiment_catalog import catalog
from .research_runner import ResearchRunner
from .shared_research import SharedResearch
from .versioned_trials import open_trials
from .forward_trials_v3 import open_trials as open_v3_trials


def experiment_entries(document):
    """Expand each immutable base cohort into defined observation-window tests."""
    entries=document.get('experiments')
    if not isinstance(entries,list) or not entries:
        raise ValueError('Long-hold experiment list is required')
    factors=document.get('observation_window_multipliers',[])
    maximum=document.get('max_observation_window_seconds',21600)
    long_hold=document.get('long_window_max_hold_seconds',2592000)
    if (not isinstance(factors,list) or any(isinstance(value,bool) or not isinstance(value,int) or value < 2 for value in factors)
            or isinstance(maximum,bool) or not isinstance(maximum,int) or maximum < 60
            or isinstance(long_hold,bool) or not isinstance(long_hold,int) or long_hold < 86400):
        raise ValueError('Invalid observation-window experiment matrix')
    result=[]
    for item in entries:
        result.append(item)
        config=item.get('config')
        if not isinstance(config,dict):
            continue
        for factor in factors:
            seconds=min(maximum,config.get('observation_seconds',0)*factor)
            if seconds <= config.get('observation_seconds',0):
                continue
            variant=copy.deepcopy(item)
            variant['id']=item.get('id','')+f'-obs-{seconds}s'
            variant['label']=item.get('label','')+f' · {seconds//60}m observation'
            variant_config=variant['config']
            variant_config['observation_seconds']=seconds
            variant_config['minimum_samples']=min(24,max(variant_config['minimum_samples'],math.ceil(seconds/300)))
            variant_config['minimum_price_changes']=min(variant_config['minimum_samples']-1,
                                                        max(variant_config['minimum_price_changes'],2))
            variant_config['max_hold_seconds']=max(variant_config['max_hold_seconds'],long_hold)
            result.append(variant)
    return result


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
        research=self.root/'exports/ladder-research-20260911/analysis.json'
        self.ladder_research=json.loads(research.read_text()) if research.exists() else None
        self.risk_config=json.loads((self.root/'ownership.config.json').read_text())
        self.memory=Memory(self.root/'data/coin-memory.sqlite',self.config)
        self.stop=threading.Event();self.probed={};self.snapshot=None
        self.shared=None;self.research_catalog=[];self.next_graduated_discovery=0;self.research_watch=[]
        self.research_timing={'cycles':0,'last_seconds':0,'max_seconds':0}
        self.status={'market':'starting','ownership':'starting'}
        self.rpc=Rpc(self.risk_config)
        self.runner=None
        # The former fast sampler is retired; every public cohort now has its own
        # simulation entry policy in the immutable strategy manifest.
        self.sampler=None
        self.experiment_runners=[]
        self.strategy_lock=threading.RLock()
        self.manifest_path=self.root/'long_hold_experiments.json'
        self.community_path=self.root/'community.config.json'
        self.primary_strategy=None
        self.ladder_enabled=self.manifest_path.exists()
        if self.ladder_enabled:
            runners=self._open_experiments()
            self.primary_strategy=runners[0]
            self.runner=runners[0]['runner']
            self.experiment_runners=runners[1:]
            matrix_path=self.root/'research_experiments.json'
            if matrix_path.exists():
                matrix=json.loads(matrix_path.read_text())
                if matrix.get('version')!='research-matrix-v1' or matrix.get('activation') not in ('pilot-100','all-1000'):
                    raise ValueError('Invalid research activation manifest')
                self.shared=SharedResearch(self.root/'data/shared-research.sqlite')
                self.research_catalog=catalog(self.runner.cfg)
                for entry in self.research_catalog:
                    if not entry['pilot'] and matrix['activation']!='all-1000':continue
                    runner=ResearchRunner(self.root/'data'/('long-hold-'+entry['id']+'.sqlite'),entry['config'],self.risk_config,self.shared)
                    self.experiment_runners.append(dict(entry,runner=runner))
            trials_path=self.root/'trial_experiments.json'
            if trials_path.exists():
                trials=json.loads(trials_path.read_text())
                self.experiment_runners.extend(open_trials(self.root,trials,
                    [self.primary_strategy]+self.experiment_runners,self.risk_config,self.shared))
            v3_trials_path=self.root/'v3_trial_experiments.json'
            if v3_trials_path.exists():
                v3_trials=json.loads(v3_trials_path.read_text())
                parents=[self.primary_strategy]+[item for item in self.experiment_runners if not item.get('trial')]
                self.experiment_runners.extend(open_v3_trials(self.root,v3_trials,parents,
                    self.risk_config,self.shared))
        else:
            from .runner import Runner
            self.runner=Runner(self.memory,self.config)
        import os
        backend=os.environ.get('PUMP_LEDGER_BACKEND','sqlite')
        if backend not in ('sqlite','postgres'): raise ValueError('Unknown ledger backend')
        self.runtime_backend=None
        self.input_archive=None
        if backend=='postgres':
            from .postgres_runtime import activate
            self.runtime_backend=activate(self,os.environ['PUMP_POSTGRES_DSN'],os.environ['PUMP_POSTGRES_NAMESPACE'])
        elif self.ladder_enabled:
            from .shared_inputs import activate
            self.input_archive=activate(self)
        self.threads=[threading.Thread(target=self._market_loop,daemon=True),threading.Thread(target=self._risk_loop,daemon=True)]
        for thread in self.threads:thread.start()

    def close(self):
        self.stop.set()
        # Workers may be awaiting a bounded RPC. Daemon shutdown leaves SQLite's committed state intact.
        for thread in self.threads:thread.join(timeout=1)
        if not any(t.is_alive() for t in self.threads):
            if self.ladder_enabled:
                for experiment in self.experiment_runners:experiment['runner'].close()
                self.runner.close()
            if self.shared:self.shared.close()
            if self.input_archive:self.input_archive.close()
            self.memory.close()
            if self.runtime_backend: self.runtime_backend.close()

    def _open_experiments(self):
        """Open isolated, immutable forward-paper cohorts outside the account."""
        path=self.manifest_path
        if not path.exists():return []
        document=json.loads(path.read_text())
        if document.get('version')!='long-hold-forward-cohorts-v1':
            raise ValueError('Unknown long-hold experiment manifest')
        entries=experiment_entries(document)
        from .ladder import LadderRunner
        result=[];seen=set()
        for item in entries:
            if not isinstance(item,dict) or not isinstance(item.get('id'),str) or not re.fullmatch(r'[a-z0-9-]{1,40}',item['id']):
                raise ValueError('Invalid long-hold experiment id')
            if item['id'] in seen or not isinstance(item.get('label'),str) or not item['label'].strip() or len(item['label'])>80:
                raise ValueError('Invalid or duplicate long-hold experiment')
            if not isinstance(item.get('config'),dict):raise ValueError('Long-hold experiment configuration is required')
            if item['config'].get('entry_style') not in ('momentum','breakout','pullback_reclaim','slow_burn',None):
                raise ValueError('Research-family configurations belong to the versioned research matrix')
            seen.add(item['id'])
            runner=LadderRunner(self.root/'data'/f"long-hold-{item['id']}.sqlite",item['config'],self.risk_config)
            result.append({'id':item['id'],'label':item['label'].strip(),'runner':runner})
        return result

    def all_long_hold_runners(self):
        return ([self.runner] if self.ladder_enabled and self.runner else [])+[item['runner'] for item in self.experiment_runners]

    def experiments_view(self):
        return [{'id':item['id'],'label':item['label'],'report':item['runner'].report()}
                for item in self.experiment_runners]

    def public_strategies(self, now=None):
        if not self.ladder_enabled:return []
        primary={'id':self.primary_strategy['id'],'label':self.primary_strategy['label'],'runner':self.runner}
        rows=[primary]+self.experiment_runners
        if self.sampler:
            rows.append({'id':'simulation-sampler','label':'Data sampler · $8k–$1M','runner':self.sampler})
        result=[]
        for row in rows:
            report=row['runner'].report()
            # Use recorded equity immediately before each period boundary. A
            # younger cohort is measured from its isolated initial balance.
            for seconds,key in ((86400,'pnl_24h_usd'),(604800,'pnl_7d_usd'),(2592000,'pnl_30d_usd')):
                baseline=report['initial_usd']
                if now is not None:
                    with row['runner'].lock:
                        previous=row['runner'].db.execute(
                            'SELECT value FROM equity WHERE at<=? ORDER BY at DESC LIMIT 1',
                            (now-seconds,)).fetchone()
                    if previous:
                        baseline=previous['value']
                report[key]=report['equity_usd']-baseline
            # A virtual balance alone does not make a cohort active. Count only
            # recorded execution fills so untouched strategies can be omitted
            # from each period's public portfolio ranking.
            activity_counts={};trade_counts={}
            with row['runner'].lock:
                for seconds,key in ((86400,'today'),(604800,'7d'),(2592000,'30d')):
                    counts=row['runner'].db.execute('''SELECT count(*) AS activity,
                        coalesce(sum(action='fill_entry'),0) AS trades FROM events
                        WHERE at>=? AND action IN ('fill_entry','fill_exit','fill_partial_exit')''',
                        ((now or time.time())-seconds,)).fetchone()
                    activity_counts[key]=counts['activity'];trade_counts[key]=counts['trades']
                counts=row['runner'].db.execute('''SELECT count(*) AS activity,
                    coalesce(sum(action='fill_entry'),0) AS trades FROM events
                    WHERE action IN ('fill_entry','fill_exit','fill_partial_exit')''').fetchone()
                activity_counts['all']=counts['activity'];trade_counts['all']=counts['trades']
            report['activity_counts']=activity_counts
            report['trade_counts']=trade_counts
            sales=report['trades']
            completed=[sale for sale in sales if sale.get('initial_fraction',0)>=1-1e-9]
            wins=sum(sale['pnl_usd']>0 for sale in completed)
            losses=sum(sale['pnl_usd']<0 for sale in completed)
            result.append({'id':row['id'],'label':row['label'],'report':report,
                           'trial':row.get('trial'),
                           'description':row.get('description'),'family':row.get('family',report['config']['entry_style']),
                           'wins':wins,'losses':losses,'recent_sales':sales[:8]})
        return result

    def portfolio_view(self,strategies,now):
        """Aggregate independent virtual ledgers for display; never pools their cash."""
        allocations=[]
        runner_lookup={self.primary_strategy['id']:self.runner,
                       **{item['id']:item['runner'] for item in self.experiment_runners}}
        if self.sampler:runner_lookup['simulation-sampler']=self.sampler
        for index,strategy in enumerate(strategies):
            report=strategy['report'];runner=runner_lookup.get(strategy['id'])
            allocations.append({'id':strategy['id'],'label':strategy['label'],'initial_usd':report['initial_usd'],
                'equity_usd':report['equity_usd'],'cash_usd':report['cash_usd'],'pnl_usd':report['pnl_usd'],
                'open_positions':len(report['positions']),'pending':report['pending_count'],
                'activity_counts':report.get('activity_counts',{})})
        initial=sum(row['initial_usd'] for row in allocations)
        def combined_curve(cutoff,bucket_seconds):
            current=[row['initial_usd'] for row in allocations];samples=[]
            for index,strategy in enumerate(strategies):
                runner=runner_lookup.get(strategy['id'])
                if not runner:continue
                with runner.lock:
                    if cutoff is not None:
                        prior=runner.db.execute('SELECT value FROM equity WHERE at<=? ORDER BY id DESC LIMIT 1',(cutoff,)).fetchone()
                        if prior:samples.append((cutoff,index,prior['value']))
                        if getattr(self,'public_equity_cache',None):
                            rows=[r for r in self.public_equity_cache.rows(runner,bucket_seconds,now,cutoff) if r['at']>=cutoff]
                        else: rows=runner.db.execute('''SELECT e.at,e.value FROM equity e JOIN
                            (SELECT max(id) AS id FROM equity WHERE at>=? GROUP BY cast(at / ? AS integer)) sampled
                            ON e.id=sampled.id ORDER BY e.id''',(cutoff,bucket_seconds))
                    else:
                        if getattr(self,'public_equity_cache',None):
                            rows=self.public_equity_cache.rows(runner,bucket_seconds,now)
                        else: rows=runner.db.execute('''SELECT e.at,e.value FROM equity e JOIN
                            (SELECT max(id) AS id FROM equity GROUP BY cast(at / ? AS integer)) sampled
                            ON e.id=sampled.id ORDER BY e.id''',(bucket_seconds,))
                    samples.extend((row['at'],index,row['value']) for row in rows)
            curve=[]
            for at,index,value in sorted(samples):
                current[index]=value
                point={'at':at,'value':sum(current)}
                if curve and abs(curve[-1]['at']-at)<1e-6:curve[-1]=point
                else:curve.append(point)
            return curve or [{'at':now,'value':initial}]
        started=[strategy['report']['started_at'] for strategy in strategies if strategy['report']['started_at']]
        available=max(0,now-min(started)) if started else 0
        def bucket_for(seconds):
            if seconds <= 86400:return 300
            if seconds <= 604800:return 1800
            if seconds <= 2592000:return 7200
            return 21600
        curves={'today':combined_curve(now-86400,bucket_for(min(86400,available))),
                '7d':combined_curve(now-604800,bucket_for(min(604800,available))),
                '30d':combined_curve(now-2592000,bucket_for(min(2592000,available))),
                'all':combined_curve(None,bucket_for(available))}
        curve=curves['all']
        opening=curves['today'][0]['value'] if curves['today'] else initial
        equity=sum(row['equity_usd'] for row in allocations)
        for row in allocations:row['allocation_fraction']=row['initial_usd']/initial if initial else 0
        return {'initial_usd':initial,'equity_usd':equity,'pnl_usd':equity-initial,
                'today_pnl_usd':equity-opening,'cash_usd':sum(row['cash_usd'] for row in allocations),
                'open_positions':sum(row['open_positions'] for row in allocations),
                'allocations':allocations,'equity_curve':curve,'equity_curves':curves}

    def _closed_position_rows(self,strategy,limit=None):
        """Map one strategy's complete-trade ledger without exposing any signing data."""
        query='SELECT data FROM trades ORDER BY id DESC'
        if limit is not None:query+=' LIMIT '+str(int(limit))
        with strategy['runner'].lock:
            trades=[json.loads(row['data']) for row in strategy['runner'].db.execute(query)]
        sampler=getattr(strategy['runner'],'is_simulation_sampler',False)
        return [{'strategy_id':strategy['id'],'strategy_label':strategy['label'],
                 'mint':trade['mint'],'symbol':trade['symbol'],'entry_at':trade['entry_at'],
                 'exit_at':trade['exit_at'],'entry_cap':trade['entry_market_cap_usd'] if sampler else trade['entry_cap'],
                 'exit_cap':trade.get('exit_market_cap_usd') if sampler else trade.get('last_cap'),'pnl_usd':trade['pnl_usd'],
                 'cost_usd':trade.get('cost_usd'),'proceeds_usd':trade.get('proceeds_usd'),
                 'entry_price_usd':trade.get('entry_price_usd'),'exit_price_usd':trade.get('exit_price_usd'),
                 'exit_fees_usd':trade.get('exit_fees_usd'),'exit_reason':trade['exit_reason']}
                for trade in trades]

    def closed_positions(self):
        """Return the newest fully closed positions across the public lab."""
        rows=[self.primary_strategy]+self.experiment_runners
        if self.sampler:rows.append({'id':'simulation-sampler','label':'Data sampler · $8k–$1M','runner':self.sampler})
        result=[]
        for strategy in rows:result.extend(self._closed_position_rows(strategy,limit=100))
        return sorted(result,key=lambda trade:trade['exit_at'],reverse=True)[:100]

    def strategy_closed_positions(self,strategy_id):
        """Return every closed position for one public instance, newest first."""
        rows=[self.primary_strategy]+self.experiment_runners
        strategy=next((row for row in rows if row['id']==strategy_id),None)
        if strategy is None:return None
        return self._closed_position_rows(strategy)

    def export_public_history_csv(self):
        """Export complete public execution records without high-frequency value marks."""
        fields=('strategy_id','strategy_label','record_type','at','mint','symbol','action',
                'entry_cap_usd','exit_cap_usd','pnl_usd','equity_usd','cash_usd','payload')
        out=io.StringIO();writer=csv.DictWriter(out,fields);writer.writeheader()
        def write(row):
            for key,value in row.items():
                if isinstance(value,str) and value.lstrip().startswith(('=','+','-','@')):row[key]="'"+value
            writer.writerow(row)
        rows=[self.primary_strategy]+self.experiment_runners
        if self.sampler:rows.append({'id':'simulation-sampler','label':'Data sampler · $8k–$1M','runner':self.sampler})
        for strategy in rows:
            runner=strategy['runner'];common={'strategy_id':strategy['id'],'strategy_label':strategy['label']}
            with runner.lock:
                sampler=getattr(runner,'is_simulation_sampler',False)
                events=[dict(row,data=json.loads(row['data'])) for row in runner.db.execute(
                    'SELECT at,mint,action,data FROM '+('decisions' if sampler else 'events')+' ORDER BY id')]
                sales=[] if sampler else [json.loads(row['data']) for row in runner.db.execute('SELECT data FROM sales ORDER BY id')]
                trades=[json.loads(row['data']) for row in runner.db.execute('SELECT data FROM trades ORDER BY id')]
            for event in events:write(dict(common,record_type='event',at=event['at'],mint=event['mint'],
                action=event['action'] if sampler else event['action'],payload=canonical(event['data'])))
            for sale in sales:write(dict(common,record_type='sale_leg',at=sale['exit_at'],mint=sale['mint'],
                symbol=sale['symbol'],entry_cap_usd=sale['entry_cap'],exit_cap_usd=sale['exit_cap'],
                pnl_usd=sale['pnl_usd'],action=sale['exit_reason'],payload=canonical(sale)))
            for trade in trades:write(dict(common,record_type='closed_position',at=trade['exit_at'],mint=trade['mint'],
                symbol=trade['symbol'],entry_cap_usd=trade['entry_market_cap_usd'] if sampler else trade['entry_cap'],exit_cap_usd=trade.get('exit_market_cap_usd') if sampler else trade.get('last_cap'),
                pnl_usd=trade['pnl_usd'],action=trade['exit_reason'],payload=canonical(trade)))
        return out.getvalue()

    def export_research_csv(self):
        return self.memory.export_research_csv()

    def community_view(self):
        try:document=json.loads(self.community_path.read_text())
        except (OSError,json.JSONDecodeError):document={}
        return {'token_status':document.get('token_status','Community token planning is not active.'),
                'telegram_url':document.get('telegram_url',''),'developer_url':document.get('developer_url',''),
                'x_url':document.get('x_url',''),'lab_email':document.get('lab_email','')}

    def update_community(self,item):
        if not isinstance(item,dict):raise ValueError('Community settings must be an object')
        urls={}
        for key in ('telegram_url','developer_url','x_url'):
            value=item.get(key,'')
            if not isinstance(value,str) or len(value)>300:raise ValueError('Invalid community URL')
            value=value.strip()
            if value:
                parsed=urlsplit(value)
                if parsed.scheme!='https' or not parsed.netloc:raise ValueError('Community links must use HTTPS')
            urls[key]=value
        email=item.get('lab_email','')
        if not isinstance(email,str) or len(email)>254:raise ValueError('Invalid lab email')
        email=email.strip()
        if email and ('@' not in email or any(char.isspace() for char in email)):raise ValueError('Invalid lab email')
        status=item.get('token_status','')
        if not isinstance(status,str) or not status.strip() or len(status)>240:raise ValueError('Invalid community status')
        document={'version':'community-v2','token_status':status.strip(),**urls,'lab_email':email}
        self.community_path.write_text(json.dumps(document,indent=2)+'\n')
        return self.community_view()

    def public_view(self,now):
        strategies=self.public_strategies(now)
        sales=[];candidates=[];outcomes=[];positions=[]
        for strategy in strategies:
            report=strategy['report']
            for candidate in report['candidates']:
                candidates.append(dict(candidate,strategy_id=strategy['id'],strategy_label=strategy['label'],
                                       state='Signal scheduled' if candidate['reason']=='Entry scheduled' else candidate['reason']))
            for position in report['positions']:
                row={'strategy_id':strategy['id'],'strategy_label':strategy['label'],
                    'symbol':position['symbol'],'mint':position['mint'],'entry_cap':position['entry_cap'],
                    'current_cap':position.get('last_cap'),'pnl_usd':position['total_pnl_usd'],
                    'entry_at':position['entry_at'],'status':position['status'],'kind':'Open position',
                    'cost_usd':position.get('cost_usd'),'mark_value_usd':position.get('mark_value_usd'),
                    'entry_price_usd':position.get('entry_price_usd'),'next_target':position.get('next_target'),
                    'remaining_fraction':position.get('remaining_fraction'),'ownership_status':position.get('ownership_status')}
                positions.append(row);outcomes.append(row)
            for sale in strategy['recent_sales']:
                row=dict(sale,strategy_id=strategy['id'],strategy_label=strategy['label'])
                sales.append(row)
                outcomes.append({'strategy_id':strategy['id'],'strategy_label':strategy['label'],
                    'symbol':sale['symbol'],'mint':sale['mint'],'entry_cap':sale['entry_cap'],
                    'current_cap':sale['exit_cap'],'pnl_usd':sale['pnl_usd'],'kind':'Realized sale'})
        sales.sort(key=lambda sale:sale['exit_at'],reverse=True)
        candidates.sort(key=lambda candidate:(candidate['reason']!='Entry scheduled',candidate['market_cap_usd'],candidate['strategy_id']))
        winners=sorted((row for row in outcomes if row['pnl_usd']>=0),key=lambda row:row['pnl_usd'],reverse=True)[:10]
        losers=sorted((row for row in outcomes if row['pnl_usd']<0),key=lambda row:row['pnl_usd'])[:10]
        closed=self.closed_positions()
        with self.memory.lock:
            tracking_started_at=self.memory.view(now).get('tracking_started_at')
        return {'mode':'SIMULATION','as_of':now,'market_tracking_started_at':tracking_started_at,
                'service':dict(self.status),'strategies':strategies,
                'recent_sales':sales[:30],'candidates':candidates[:40],
                'open_positions':sorted(positions,key=lambda row:row['entry_at'],reverse=True),
                'closed_positions':closed,
                'top_winners':winners,'top_losers':losers,'portfolio':self.portfolio_view(strategies,now),
                'research':self.memory.research_view(),
                'experiment_matrix':self.experiment_matrix_view(),
                'community':self.community_view(),
                'disclaimer':'Public market observations with simulated fills only. No wallet, signing, deposits, or real orders.'}

    def live_feed_view(self,now):
        """Small public header feed; it never exposes a wallet or order action."""
        latest=None
        rows=[self.primary_strategy]+self.experiment_runners
        for item in rows:
            runner=item['runner']
            with runner.lock:
                event=runner.db.execute('''SELECT at,mint,action,data FROM events
                    WHERE action IN ('fill_entry','fill_exit') ORDER BY id DESC LIMIT 1''').fetchone()
            if not event or (latest and event['at']<=latest['at']):
                continue
            payload=json.loads(event['data'])
            latest={'at':event['at'],'mint':event['mint'],'action':event['action'],
                    'symbol':str(payload.get('symbol',event['mint'][:8]))[:80],
                    'strategy_id':item['id'],'strategy_label':item['label'],
                    'pnl_usd':payload.get('pnl_usd') if event['action']=='fill_exit' else None}
        return {'as_of':now,'market':self.status.get('market','Live follow-up connected'),
                'event':latest,'strategy_count':len(rows)}

    def add_strategy(self,item):
        """Add one locally administered, paper-only strategy without touching others."""
        with self.strategy_lock:
            if not isinstance(item,dict):raise ValueError('Strategy must be an object')
            existing=[self.primary_strategy]+self.experiment_runners
            if any(row['id']==item.get('id') for row in existing):raise ValueError('Strategy id already exists')
            document=json.loads(self.manifest_path.read_text())
            candidate={'id':item.get('id'),'label':item.get('label'),'config':item.get('config')}
            temporary={'version':document.get('version'),'experiments':[candidate],
                       'observation_window_multipliers':document.get('observation_window_multipliers',[]),
                       'max_observation_window_seconds':document.get('max_observation_window_seconds',21600),
                       'long_window_max_hold_seconds':document.get('long_window_max_hold_seconds',2592000)}
            previous=self.manifest_path
            self.manifest_path=self.root/'data'/'.strategy-validation.json'
            self.manifest_path.write_text(json.dumps(temporary))
            try:
                created=self._open_experiments()
            finally:
                self.manifest_path.unlink(missing_ok=True)
                self.manifest_path=previous
            document['experiments'].append(candidate)
            self.manifest_path.write_text(json.dumps(document,indent=2)+'\n')
            self.experiment_runners.extend(created)
            base=created[0]
            return {'id':base['id'],'label':base['label'],'report':base['runner'].report()}

    def experiment_matrix_view(self):
        funded=sum(isinstance(item['runner'],ResearchRunner) and not item.get('trial') for item in self.experiment_runners)
        return {'catalog_count':len(self.research_catalog),'funded':funded,
                'matched_trial_instances':sum(bool(item.get('trial')) for item in self.experiment_runners),
                'staged':len(self.research_catalog)-funded,'balance_each_usd':500,
                'timing':dict(self.research_timing),'shared_inputs':bool(self.shared)}

    def long_hold_cap_bounds(self):
        configs=[runner.cfg for runner in self.all_long_hold_runners()]
        return min(c['entry_cap_min'] for c in configs),max(c['entry_cap_max'] for c in configs)

    def annotate(self,batch):
        with self.memory.lock:
            for snap in batch['snapshots']:
                gate=self.memory.ownership(snap['mint'],batch['at'])
                snap['entry_guard']={k:gate.get(k) for k in ('mint','checked_at','policy_hash','status')}
        return batch

    def view(self):
        result=copy.deepcopy(self.snapshot) if self.snapshot else {'coins':[],'tracked_count':0,'persistent_count':0,'candidate_count':0,'blocked_count':0,'unknown_count':0,'milestones':[],'as_of':None,'config':self.config,'coverage':'Loading recorded coin history'}
        result['service']=dict(self.status)
        result['research']=self.memory.research_view()
        if self.runner:result['runner']=self.runner.report()
        result['experiments']=self.experiments_view()
        result['ladder_research']=self.ladder_research
        return result

    def watch_priority(self):
        held=set().union(*(runner.held_mints() for runner in self.all_long_hold_runners())) if self.ladder_enabled else set()
        # Graduated positions are followed by the batched PumpSwap worker.
        with self.memory.lock:
            held={mint for mint in held if not (self.memory.get(mint) or {}).get('phase')=='graduated'}
        return held | (self.sampler.held_mints() if self.sampler else set())

    def step_ladder(self,batch):
        if not self.ladder_enabled:return
        tick=time.monotonic()
        # Feed observations drive this test every cycle. Imported history can inform
        # peak context but can never insert old purchases into its new ledger.
        quotes={q['mint']:q for q in batch['snapshots']}
        completed={q['mint'] for q in batch['snapshots'] if q.get('complete') is True and
                   q.get('route')=='pump_bonding_curve_sol' and q.get('observed_at',float('inf'))<=batch['at']}
        with self.memory.lock:
            with self.memory.db:
                for q in batch['snapshots']:
                    self.memory.observe(q,q['observed_at'],'pump',live=True)
                for mint in completed:
                    self.memory.db.execute('INSERT OR IGNORE INTO research_migrations VALUES (?,?)',(mint,batch['at']))
            runners=self.all_long_hold_runners()
            held=set().union(*(runner.held_mints() for runner in runners))
            if self.shared:
                # Include fresh indexed quotes for unheld candidates too, enabling
                # entries above graduation without one provider request per strategy.
                for row in self.memory.db.execute('SELECT data FROM coins'):
                    coin=json.loads(row['data']);q=coin['quote']
                    if q.get('dex_id')=='pumpswap' and 0<=batch['at']-q['observed_at']<=120:
                        current=quotes.get(coin['mint'])
                        if not current or current.get('complete') or q['observed_at']>current['observed_at']:
                            quotes[coin['mint']]=q
            for mint in held:
                coin=self.memory.get(mint)
                if not coin:continue
                q=coin['quote'];current=quotes.get(mint)
                # An indexed AMM quote can replace a completed or unsupported curve.
                if q.get('dex_id')=='pumpswap' and (not current or current.get('complete') or
                        q['observed_at']>current['observed_at']):quotes[mint]=q
            evidence={}
            for mint in set(quotes)|held:
                coin=self.memory.get(mint)
                evidence[mint]={'ownership':self.memory.ownership(mint,batch['at']),
                                'peak_cap':coin['peak_cap'] if coin and coin['last_at']<=batch['at'] else None,
                                'curve_complete':mint in completed,
                                'graduated_observed':bool(self.memory.db.execute(
                                    'SELECT 1 FROM research_migrations WHERE mint=? AND at<=?',(mint,batch['at'])).fetchone())}
            transition={'at':batch['at'],'quotes':list(quotes.values()),'evidence':evidence}
            if self.shared:
                inspector=next(item['runner'] for item in self.experiment_runners if isinstance(item['runner'],ResearchRunner))
                self.shared.prepare(transition,inspector.market)
            for runner in runners:runner.step(transition)
        # Copy committed fills into the research ledger only after the strategy
        # transition succeeds, then schedule comparable outcome windows.
        for item in [self.primary_strategy]+self.experiment_runners:
            self.memory.ingest_strategy_events(item['id'],item['runner'])
        if self.sampler:self.sampler.step(batch)
        elapsed=time.monotonic()-tick
        self.research_timing={'cycles':self.research_timing['cycles']+1,'last_seconds':elapsed,
                              'completed_at':time.time(),
                              'input_observed_at':max((q.get('observed_at',0) for q in batch['snapshots']),default=0),
                              'max_seconds':max(elapsed,self.research_timing['max_seconds'])}

    def _market_loop(self):
        while not self.stop.is_set():
            try:
                with self.memory.lock,self.memory.db:
                    self.memory.import_ledgers(self.root/'data')
                    rows=[json.loads(r[0]) for r in self.memory.db.execute('SELECT data FROM coins')]
                    held=set().union(*(runner.held_mints() for runner in self.all_long_hold_runners()))
                    self.snapshot=self.memory.view(time.time())
                    self.snapshot['runner']=self.runner.report()
                    self.snapshot['experiments']=self.experiments_view()
                now=time.time()
                graduated=[]
                if self.shared and now>=self.next_graduated_discovery:
                    self.next_graduated_discovery=now+300
                    try:
                        discovered=get_json('/coins?offset=0&limit=30&sort=last_trade_timestamp&order=DESC&includeNsfw=false&complete=true')
                        with self.memory.lock,self.memory.db:
                            for coin in discovered if isinstance(discovered,list) else []:
                                mint=coin.get('mint','')
                                if MINT.fullmatch(mint) and coin.get('complete') is True:
                                    self.memory.db.execute('INSERT OR IGNORE INTO research_migrations VALUES (?,?)',(mint,now))
                                    graduated.append(mint)
                    except Exception:pass
                # Held positions first, then the least-recently probed names: no survivor-only deletion.
                rows.sort(key=lambda c:(c['mint'] not in held,self.probed.get(c['mint'],0),-c['peak_cap']))
                limit=min(30,self.config['probe_limit'])
                with self.memory.lock:
                    migrated=[r['mint'] for r in self.memory.db.execute('SELECT mint FROM research_migrations')]
                candidates=sorted(set(graduated+migrated+[c['mint'] for c in rows]),key=lambda m:self.probed.get(m,0))
                due=self.memory.due_outcome_mints(now,max(1,limit//10))
                amm_held={c['mint'] for c in rows if c['mint'] in held and c.get('phase')=='graduated'}
                held_targets=sorted(amm_held,key=lambda m:self.probed.get(m,0))[:max(1,limit//6)]
                panel=self.observation_panel(rows,now,max(1,limit*2//3)) if self.shared else []
                targets=list(dict.fromkeys(due+held_targets+panel+candidates))[:limit]
                try:quotes=dex_quotes(targets,time.time())
                except Exception:
                    self.memory.finish_research_probes(targets,set(),time.time())
                    for mint in targets:self.probed[mint]=now
                    raise
                found={q['mint'] for q in quotes}
                if targets and not quotes:self.status['market']='No follow-up quotes returned; history retained'
                else:self.status['market']='Live follow-up connected'
                # Pump fallback keeps following pre-graduation coins when DEX indexing has no pair yet.
                missing=[m for m in targets if m not in found or
                         any(q['mint']==m and q.get('dex_id')=='pumpfun' for q in quotes)][:3]
                if missing:
                    try:fx=get_json('/sol-price')
                    except Exception:fx=None
                    for mint in missing:
                        if fx is None:break
                        try:
                            raw=get_json('/coins/'+mint);quotes.append(normalize(raw,fx,time.time()))
                        except Exception:pass
                with self.memory.lock,self.memory.db:
                    accepted=set()
                    for q in quotes:
                        source='dex' if 'dex_id' in q else 'pump'
                        if self.memory.observe(q,q['observed_at'],source,live=True):accepted.add(q['mint'])
                    for mint in targets:self.probed[mint]=now
                    # Only this prospective loop can schedule/fill a longer-hold position.
                    if not self.ladder_enabled:self.runner.step(quotes,time.time())
                    self.snapshot=self.memory.view(time.time())
                    self.snapshot['runner']=self.runner.report()
                    self.snapshot['experiments']=self.experiments_view()
                self.memory.finish_research_probes(targets,accepted,time.time())
            except Exception as e:
                detail='HTTP '+str(e.code) if isinstance(e,urllib.error.HTTPError) else type(e).__name__
                self.status['market']='Market follow-up unavailable ('+detail+'); recorded history retained'
            self.stop.wait(self.config['refresh_seconds'])

    def observation_panel(self,rows,now,limit):
        """Keep a cap-balanced panel warm; rotation alone cannot form long windows.

        Slots are selected by coverage, never realized profit. Due measurements
        and held positions retain separate reserved slots in the same request.
        """
        from .experiment_catalog import BANDS
        eligible={c['mint']:c for c in rows if c.get('quote',{}).get('dex_id')=='pumpswap'
                  and 0<=now-c['last_at']<=900 and 8000<=c['last_cap']<1000000}
        panel=[mint for mint in self.research_watch if mint in eligible][:limit]
        for low,high in BANDS:
            quota=max(1,limit//len(BANDS))
            count=sum(low<=eligible[m]['last_cap']<high for m in panel)
            choices=sorted((m for m,c in eligible.items() if m not in panel and low<=c['last_cap']<high),
                           key=lambda m:(eligible[m]['first_at'],m))
            panel.extend(choices[:min(max(0,quota-count),limit-len(panel))])
        self.research_watch=panel
        return panel

    def _risk_loop(self):
        while not self.stop.is_set():
            try:
                with self.engine.lock:
                    snapshots=copy.deepcopy(self.engine.state['snapshots'])
                    candidates=copy.deepcopy(self.engine.state['candidates'])
                now=time.time()
                choices=[]
                cap_min,cap_max=self.long_hold_cap_bounds() if self.ladder_enabled else (10000,250000)
                with self.memory.lock:
                    for mint,s in snapshots.items():
                        if now-s['observed_at']>30 or not cap_min<=s.get('market_cap_usd',0)<=max(250000,cap_max):continue
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
