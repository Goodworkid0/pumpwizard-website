#!/usr/bin/env python3
"""Combine active paper ledgers and rebalance their allocations once, while stopped."""
import argparse
import fcntl
import json
from pathlib import Path
import sqlite3
import time

from paper.engine import canonical
from paper.funding import adjusted_state, implementation_hash


def migrate(root, at=None, apply=False):
    root = Path(root)
    paths = {'scalp': root/'data/cadence.sqlite', 'ladder': root/'data/ladder.sqlite'}
    locks, connections = [], []
    try:
        for path in paths.values():
            lock = open(str(path)+'.lock', 'a'); locks.append(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError('Stop the collector before changing account allocations') from None
        members = {}
        for name, path in paths.items():
            con = sqlite3.connect(f'file:{path}?mode=ro', uri=True); connections.append(con)
            meta = dict(con.execute('SELECT key,value FROM meta'))
            state, contract = json.loads(meta['state']), json.loads(meta['contract'])
            if contract['source'] != 'public_index_live':
                raise ValueError('Only actual live-observation portfolios can join this account')
            capital = contract['config']['virtual_balance_usd' if name == 'scalp' else 'budget_usd']
            members[name] = {'cash_usd': state['cash_usd'], 'equity_usd': state['cash_usd']+
                sum(p['mark_value_usd'] for p in state['positions'].values()),
                'capital_usd': capital, 'state': state, 'meta': meta}
        account_path = root/'data/account.sqlite'
        if account_path.exists():
            existing = sqlite3.connect(account_path); connections.append(existing)
            row = existing.execute("SELECT value FROM meta WHERE key='migration'").fetchone()
            if row:
                saved = json.loads(row[0])
                for name in members:
                    if 'account_funding' not in members[name]['meta']:
                        raise ValueError('Incomplete account migration; preserve databases for recovery')
                return dict(saved, already_applied=True)
        if any('account_funding' in m['meta'] for m in members.values()):
            raise ValueError('Funding exists without its account ledger; refusing a second transfer')
        total = sum(m['equity_usd'] for m in members.values())
        delta = total/2 - members['ladder']['equity_usd']
        at = time.time() if at is None else at
        if at <= max(m['state']['last_at'] for m in members.values()):
            raise ValueError('Migration must follow the last recorded market input')
        deltas = {'scalp': -delta, 'ladder': delta}
        for name, m in members.items():
            pending = m['state'].get('pending_entries', m['state'].get('pending', {}))
            cfg = json.loads(m['meta']['contract'])['config']
            order = cfg['position_usd' if name == 'scalp' else 'order_usd']
            if m['cash_usd']+deltas[name] < len(pending)*order:
                raise ValueError('Cannot rebalance cash reserved by existing pending entries')
        summary = {'version': 'unified-paper-account-v1', 'at': at,
            'combined_equity_usd': total, 'combined_cash_usd': sum(m['cash_usd'] for m in members.values()),
            'original_capital_usd': sum(m['capital_usd'] for m in members.values()),
            'allocations': {name: {'before_equity_usd': m['equity_usd'], 'transfer_usd': deltas[name],
                'cash_usd': m['cash_usd']+deltas[name], 'equity_usd': m['equity_usd']+deltas[name]}
                for name, m in members.items()}, 'applied': apply}
        if not apply:
            return summary
        # SQLite backups while both application writer locks are held.
        backup = root/'experiments'/('before-unified-account-'+str(int(at)))
        backup.mkdir(parents=True, exist_ok=False)
        for name, path in paths.items():
            with sqlite3.connect(backup/path.name) as target:
                connections[list(paths).index(name)].backup(target)
        for con in connections:
            con.close()
        connections.clear()
        con = sqlite3.connect(account_path); connections.append(con)
        con.execute('PRAGMA journal_mode=DELETE')
        for name, path in paths.items():
            con.execute(f'ATTACH DATABASE ? AS {name}', (str(path),))
            con.execute(f'PRAGMA {name}.journal_mode=DELETE')
        # A single rollback-journal transaction covers both source ledgers and
        # the transfer record; no half-applied cross-database funding operation.
        with con:
            con.execute('CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT)')
            con.execute('CREATE TABLE IF NOT EXISTS transfers(id TEXT PRIMARY KEY,at REAL,source TEXT,destination TEXT,amount REAL)')
        con.execute('BEGIN IMMEDIATE')
        try:
            for name, m in members.items():
                state = adjusted_state(m['state'], deltas[name], name)
                funding = {'version': 'account-funding-v1', 'strategy': name,
                    'base_capital_usd': m['capital_usd'], 'code_hash': implementation_hash(),
                    'events': [{'id': 'initial-equal-allocation', 'at': at, 'amount_usd': deltas[name]}]}
                con.execute(f'INSERT INTO {name}.meta VALUES (?,?)', ('account_funding', canonical(funding)))
                con.execute(f'UPDATE {name}.meta SET value=? WHERE key=?', (canonical(state), 'state'))
                values = (at, m['equity_usd']+deltas[name], state['cash_usd'])
                if name == 'scalp':
                    impaired = sum(bool(p.get('mark_reason')) for p in state['positions'].values())
                    con.execute('INSERT INTO scalp.equity(at,value,cash,impaired) VALUES (?,?,?,?)', values+(impaired,))
                else:
                    con.execute('INSERT INTO ladder.equity(at,value,cash) VALUES (?,?,?)', values)
            source, destination = ('scalp', 'ladder') if delta >= 0 else ('ladder', 'scalp')
            con.execute('INSERT INTO transfers VALUES (?,?,?,?,?)', ('initial-equal-allocation', at, source, destination, abs(delta)))
            summary['backup'] = str(backup)
            con.execute('INSERT INTO meta VALUES (?,?)', ('migration', canonical(summary)))
            con.commit()
        except Exception:
            con.rollback()
            raise
        return summary
    finally:
        for con in connections:
            con.close()
        for lock in locks:
            lock.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        print(json.dumps(migrate(args.root, apply=args.apply), indent=2))
    except (ValueError, sqlite3.Error) as exc:
        raise SystemExit(str(exc))
