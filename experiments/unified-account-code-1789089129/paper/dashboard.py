"""Read-only portfolio views over the full ledger; independent of trading rules."""
import copy
import json
import math
from collections import Counter
from datetime import datetime
from zoneinfo import ZoneInfo

ZONE = ZoneInfo('Europe/Belgrade')
PERIODS = {'live': 'Live', 'today': 'Today', '7d': '7 days', '30d': '30 days'}
EVENT_ACTIONS = {'fill_entry', 'fill_exit', 'failed_entry', 'failed_exit',
                 'signal_entry', 'signal_exit', 'exit_unfilled', 'cancel_entry', 'run_status'}


def period_start(period, now, started):
    if period not in PERIODS:
        raise ValueError('Unknown period')
    if period == 'live':
        return started if started is not None else now
    if period == 'today':
        return datetime.fromtimestamp(now, ZONE).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    return now - (7 if period == '7d' else 30) * 86400


def compact_curve(points, limit=480):
    """Keep real extrema and endpoints, never invent samples for missing history."""
    if len(points) <= limit:
        return points
    width = math.ceil(len(points) / ((limit - 2) // 2))
    selected = {0, len(points)-1}
    for start in range(0, len(points), width):
        indexes = range(start, min(start + width, len(points)))
        selected.add(min(indexes, key=lambda i: points[i]['value']))
        selected.add(max(indexes, key=lambda i: points[i]['value']))
    return [points[i] for i in sorted(selected)]


def build_view(engine, now, period='live', outcome='all', sort='newest', page=1, search=''):
    if outcome not in ('all', 'winners', 'losers'):
        raise ValueError('Unknown trade filter')
    if sort not in ('newest', 'oldest', 'best', 'worst'):
        raise ValueError('Unknown trade ordering')
    page = max(1, min(int(page), 100000))
    search = str(search).strip()[:100]
    with engine.lock:
        state = engine.state
        cfg = engine.config
        end = min(state['last_at'] if state['last_at'] is not None else now, now)
        start = period_start(period, now, state['started_at'])
        points = [dict(r) for r in engine.db.execute(
            'SELECT at,value,cash,impaired FROM equity WHERE at>=? AND at<=? ORDER BY at,id', (start, end))]
        previous = engine.db.execute('SELECT at,value FROM equity WHERE at<? ORDER BY at DESC,id DESC LIMIT 1', (start,)).fetchone()
        baseline = previous['value'] if previous else cfg['virtual_balance_usd']
        closed = []
        for row in engine.db.execute('SELECT id,data FROM trades WHERE json_extract(data,\'$.exit_at\')>=? AND json_extract(data,\'$.exit_at\')<=?', (start, end)):
            trade = json.loads(row['data'])
            trade['id'] = row['id']
            # Keep payload compact and avoid exposing irrelevant internal snapshots.
            closed.append({k: trade.get(k) for k in ('id', 'mint', 'symbol', 'quantity', 'entry_at',
                'exit_at', 'entry_price_usd', 'exit_price_usd', 'entry_market_cap_usd', 'cost_usd',
                'proceeds_usd', 'pnl_usd', 'return_fraction', 'fees_usd', 'hold_seconds', 'exit_reason')})
        events = []
        for row in engine.db.execute("SELECT id,at,mint,action,reason,data FROM decisions WHERE at>=? AND at<=? AND action!='skip' ORDER BY id DESC", (start, end)):
            if row['action'] in EVENT_ACTIONS:
                event = dict(row)
                event['details'] = json.loads(event.pop('data'))
                events.append(event)
        positions = []
        symbols = {t['mint']: t['symbol'] for t in closed}
        symbols.update({m: s.get('symbol', m[:8]) for m, s in state['snapshots'].items()})
        for p in state['positions'].values():
            item = {k: copy.deepcopy(p.get(k)) for k in ('mint', 'symbol', 'quantity', 'entry_at',
                'entry_price_usd', 'cost_usd', 'mark_value_usd', 'mark_reason', 'mark_at',
                'unrealized_pnl_usd', 'status', 'exit_reason', 'entry_market_cap_usd')}
            item['return_fraction'] = item['unrealized_pnl_usd'] / item['cost_usd']
            item['exit_price_usd'] = item['mark_value_usd'] / item['quantity'] if not item['mark_reason'] else None
            item['hold_seconds'] = max(0, end - item['entry_at'])
            item['sparkline'] = [{'at': h['t'], 'value': h['price'] * h['fx']}
                                for h in state['histories'].get(p['mint'], [])]
            positions.append(item)
        positions.sort(key=lambda p: (p['entry_at'], p['mint']), reverse=True)
        latest_candidates = sorted(copy.deepcopy(list(state['candidates'].values())), key=lambda c: c['at'], reverse=True)
        candidates = [c for c in latest_candidates if end - c['at'] <= cfg['discovery_seconds'] + cfg['poll_seconds'] + 5]
        alive = state['last_at'] is not None and now - state['last_at'] <= max(20, cfg['poll_seconds'] * 3)
        halt_reason = engine._entry_block_reason()
        halted = bool(halt_reason)
        recent_buys = engine.db.execute("SELECT count(*) FROM decisions WHERE action='fill_entry' AND at>? AND at<=?", (now-60,now)).fetchone()[0]
        recent_attempts = engine.db.execute("SELECT count(*) FROM decisions WHERE action IN ('fill_entry','failed_entry') AND at>? AND at<=?", (now-60,now)).fetchone()[0]
        current_equity = state['cash_usd'] + sum(p['mark_value_usd'] for p in positions)
        portfolio = {
            'equity_usd': current_equity, 'cash_usd': state['cash_usd'],
            'initial_usd': cfg['virtual_balance_usd'], 'total_pnl_usd': current_equity-cfg['virtual_balance_usd'],
            'unrealized_pnl_usd': sum(p['unrealized_pnl_usd'] for p in positions),
            'open_count': len(positions), 'pending_count': len(state['pending_entries']),
            'exposure_usd': sum(p['cost_usd'] for p in positions),
            'impaired_count': sum(bool(p['mark_reason']) for p in positions),
            'halted': halted, 'halt_reason': halt_reason,
            'status': 'complete' if state['run_result'] else 'offline' if not alive else 'paused' if halted else 'active',
            'run_result': state['run_result'], 'profit_exit_pending': state['profit_exit_pending'],
            'buys_last_minute': recent_buys, 'attempts_last_minute': recent_attempts,
            'feed_status': state['last_source_status'], 'feed_alive': alive,
            'last_at': state['last_at'], 'started_at': state['started_at'],
            'observations': state['observations'], 'discovered_count': state['discovered_count'],
            'issues': copy.deepcopy(state['last_issues']),
        }
        source = engine.source
        config_hash = engine.implementation_hash[:12]
    wins = [t for t in closed if t['pnl_usd'] > 0]
    losses = [t for t in closed if t['pnl_usd'] < 0]
    actions = Counter(e['action'] for e in events)
    fee_total = 0
    for e in events:
        details = e['details']
        if e['action'] == 'fill_entry':
            fee_total += details.get('entry_fees_usd', 0)
            symbols[e['mint']] = details.get('symbol', e['mint'][:8])
        elif e['action'] == 'fill_exit':
            fee_total += details.get('exit_fees_usd', 0)
        elif e['action'] in ('failed_entry', 'failed_exit'):
            fee_total += details.get('fee_usd', 0)
    groups = {}
    for t in closed:
        group = groups.setdefault(t['mint'], {'mint': t['mint'], 'symbol': t['symbol'], 'pnl_usd': 0,
                                               'trades': 0, 'cost_usd': 0})
        group['pnl_usd'] += t['pnl_usd']
        group['cost_usd'] += t['cost_usd']
        group['trades'] += 1
    for g in groups.values():
        g['return_fraction'] = g['pnl_usd'] / g['cost_usd']
    leaders = sorted(groups.values(), key=lambda g: g['pnl_usd'], reverse=True)
    selected = [t for t in closed if (outcome == 'all' or outcome == 'winners' and t['pnl_usd'] > 0
                 or outcome == 'losers' and t['pnl_usd'] < 0) and
                search.casefold() in (t['symbol'] + ' ' + t['mint']).casefold()]
    selected.sort(key=lambda t: (t['exit_at'], t['id']) if sort in ('newest', 'oldest') else
                  (t['pnl_usd'], t['exit_at'], t['id']), reverse=sort in ('newest', 'best'))
    pages = max(1, math.ceil(len(selected) / 12))
    page = min(page, pages)
    peak, drawdown = baseline, 0
    for point in points:
        peak = max(peak, point['value'])
        drawdown = max(drawdown, 1 - point['value']/peak if peak else 0)
    change = points[-1]['value'] - baseline if points else None
    activity = []
    for e in events[:24]:
        details = e['details']
        activity.append({k: e[k] for k in ('id', 'at', 'mint', 'action', 'reason')} |
                        {'symbol': symbols.get(e['mint'], e['mint'][:8]),
                         'amount_usd': details.get('cost_usd') if e['action'] == 'fill_entry' else
                         details.get('proceeds_usd') if e['action'] == 'fill_exit' else details.get('fee_usd')})
    return {
        'server_at': now, 'source': source, 'implementation': config_hash,
        'range': {'id': period, 'label': PERIODS[period], 'timezone': 'Europe/Belgrade',
                  'requested_start': start, 'end': end, 'has_data': bool(points),
                  'first_observation': points[0]['at'] if points else None,
                  'partial_history': bool(state['started_at'] is not None and state['started_at'] > start + 1),
                  'baseline_usd': baseline, 'baseline_at': previous['at'] if previous else state['started_at']},
        'portfolio': portfolio,
        'performance': {'net_pnl_usd': change, 'return_fraction': change/baseline if change is not None and baseline else None,
                        'closed_trade_pnl_usd': sum(t['pnl_usd'] for t in closed),
                        'closed_count': len(closed), 'wins': len(wins), 'losses': len(losses),
                        'breakeven': len(closed)-len(wins)-len(losses),
                        'win_rate': len(wins)/len(closed) if closed else None,
                        'buys': actions['fill_entry'], 'sells': actions['fill_exit'],
                        'failed_attempts': actions['failed_entry']+actions['failed_exit'],
                        'fees_usd': fee_total, 'max_drawdown_fraction': drawdown,
                        'profit_factor': sum(t['pnl_usd'] for t in wins)/-sum(t['pnl_usd'] for t in losses) if losses else None},
        'equity_curve': compact_curve(points), 'positions': positions,
        'journal': {'rows': selected[(page-1)*12:page*12], 'total': len(selected), 'pages': pages,
                    'page': page, 'outcome': outcome, 'sort': sort, 'search': search},
        'winners': [g for g in leaders if g['pnl_usd'] > 0][:4],
        'losers': [g for g in reversed(leaders) if g['pnl_usd'] < 0][:4],
        'activity': activity, 'candidates': candidates[:40], 'config': copy.deepcopy(cfg),
    }
