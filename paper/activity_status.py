"""Explain trade inactivity from recorded decisions and ownership checks."""
import json


def activity_status(engine, now, brain=None):
    with engine.lock:
        last_fill=engine.db.execute("SELECT max(at) FROM decisions WHERE action IN ('fill_entry','fill_exit')").fetchone()[0]
        last_buy=engine.db.execute("SELECT max(at) FROM decisions WHERE action='fill_entry'").fetchone()[0]
        reasons=[dict(reason=r[0],evaluations=r[1],coins=r[2]) for r in engine.db.execute(
            "SELECT reason,count(*),count(distinct mint) FROM decisions WHERE action='skip' AND at>? AND at<=? GROUP BY reason ORDER BY count(*) DESC",
            (now-300,now))]
        fresh={m for m,s in engine.state['snapshots'].items() if 0<=now-s['observed_at']<=30 and
               engine.config['market_cap_min_usd']<=s.get('market_cap_usd',0)<=engine.config['market_cap_max_usd']}
        last_at=engine.state['last_at'];policy=getattr(engine,'entry_policy',{}).get('policy_hash')
        feed_alive=last_at is not None and 0<=now-last_at<=max(20,engine.config['poll_seconds']*3)
    checks=[];passed=0
    if brain:
        with brain.memory.lock:
            checks=[json.loads(r[0]) for r in brain.memory.db.execute('SELECT data FROM ownership_audit WHERE at>? AND at<=? ORDER BY at DESC,id DESC',(now-3600,now))]
            passed=sum((g:=brain.memory.ownership(m,now)).get('status')=='clear' and
                g.get('policy_hash')==policy and 0<=now-g.get('checked_at',0)<=120 for m in fresh)
        if brain.ladder_enabled:
            with brain.runner.lock:
                fill,buy=brain.runner.db.execute("SELECT max(CASE WHEN action IN ('fill_entry','fill_exit','fill_partial_exit') THEN at END),max(CASE WHEN action='fill_entry' THEN at END) FROM events").fetchone()
                last_fill=max(x for x in (last_fill,fill) if x is not None) if last_fill is not None or fill is not None else None
                last_buy=max(x for x in (last_buy,buy) if x is not None) if last_buy is not None or buy is not None else None
    latest=checks[0] if checks else None
    rpc_problem=bool(latest and any('RPC' in r for r in latest.get('reasons',[])))
    unverified=next((r['coins'] for r in reasons if r['reason']=='ownership_unverified'),0)
    status='scanning';title='Checking entry rules';warning=False
    message='Fresh observations are being checked for price movement, liquidity and ownership before a paper buy.'
    if not feed_alive:
        status='feed_unavailable';title='Waiting for market data';warning=True
        message='The market collector has no fresh observation. Displayed balances use its last recorded data.'
    elif brain and rpc_problem and unverified and not passed:
        status='ownership_unavailable';title='Entries waiting for ownership data';warning=True
        message='Market prices are updating, but the ownership connection is failing or cooling down. New buys require a current completed screen; existing positions can still exit.'
    elif brain and fresh and unverified and not passed:
        status='ownership_pending';title='Waiting for an eligible ownership screen';warning=True
        message='No currently watched coin in the entry range has a fresh passed ownership screen. Missing, expired or flagged evidence blocks buys.'
    return {'status':status,'title':title,'message':message,'warning':warning,
        'as_of':now,'last_fill_at':last_fill,'last_buy_at':last_buy,'fresh_candidates':len(fresh),
        'fresh_ownership_passes':passed,'window_seconds':300,'reasons':reasons,
        'checks_last_hour':{'total':len(checks),'clear':sum(c['status']=='clear' for c in checks),
            'blocked':sum(c['status']=='blocked' for c in checks),'unknown':sum(c['status']=='unknown' for c in checks),
            'rpc_failures':sum(any('RPC' in r for r in c.get('reasons',[])) for c in checks)},
        'last_ownership_check_at':latest['checked_at'] if latest else None,
        'last_ownership_result':latest['status'] if latest else None,
        'last_ownership_reasons':latest.get('reasons',[]) if latest else []}
