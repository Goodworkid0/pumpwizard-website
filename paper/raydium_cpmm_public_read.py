"""Read-only public projection for one observed Raydium CPMM pilot.

This adapter deliberately has a much smaller contract than the Pump.fun read
model.  It derives its response only from the fixed pilot configuration, the
immutable pool identity, the append-only standard-RPC snapshot log, and the
separate observed paper ledger.  It neither opens a database for writing nor
falls back to a Pump.fun publication when one of those sources is unavailable.
"""
import csv
import gzip
import io
import json
import time
from pathlib import Path

from .market_archive import ArchiveError, MarketArchive, token_id
from .observed_market_log import ObservedLogError, ObservedMarketLog
from .public_read import ReadError, encode
from .raydium_cpmm_observed_ledger import ObservedRaydiumLedger, RaydiumObservedLedgerError
from .raydium_cpmm_pilot import ObservedRaydiumPilot, RaydiumPilotError


class RaydiumCPMMPublicRead:
    """Project the observed pilot into the shared public-dashboard schema."""
    def __init__(self, root):
        root = Path(root)
        self.archive_path = root / 'data/raydium-identities.sqlite'
        self.log_path = root / 'data/raydium-observed.sqlite'
        self.ledger_path = root / 'data/raydium-near-usdc-pilot.sqlite'
        self.config_path = root / 'deploy/raydium-near-usdc-pilot.json'

    def _open(self):
        """Open every evidence source read-only, or provide no publication."""
        if not all(path.is_file() for path in (self.archive_path, self.log_path, self.ledger_path, self.config_path)):
            raise ReadError(503, 'Raydium observed publication is not available yet.')
        try:
            config = json.loads(self.config_path.read_text())
            pilot = ObservedRaydiumPilot(config)
            source = {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only'}
            archive = MarketArchive(self.archive_path, view_only=True)
            log = ObservedMarketLog(self.log_path, view_only=True)
            ledger = ObservedRaydiumLedger(self.ledger_path, {
                'pool_id': pilot.config['pool_id'],
                'initial_quote_amount': pilot.config['initial_quote_amount'],
                'max_positions': 1,
                'max_snapshot_age_seconds': 120,
            }, source, view_only=True)
            return pilot, archive, log, ledger
        except (ArchiveError, ObservedLogError, RaydiumObservedLedgerError, RaydiumPilotError,
                OSError, ValueError, json.JSONDecodeError) as exc:
            raise ReadError(503, 'Raydium observed publication cannot be verified.') from exc

    @staticmethod
    def _denomination(archive, quote_mint):
        """Read the archived token identity; this is not a price oracle."""
        with archive.lock:
            row = archive.db.execute('SELECT data FROM tokens WHERE id=?', (token_id(quote_mint),)).fetchone()
        if row is None:
            raise ReadError(503, 'Raydium quote-token identity is unavailable.')
        token = json.loads(row['data'])
        decimals = token.get('decimals')
        if not isinstance(decimals, int) or not 0 <= decimals <= 18:
            raise ReadError(503, 'Raydium quote-token display is not configured.')
        return token.get('symbol') or 'quote token', 10 ** decimals

    def _snapshot(self):
        pilot, archive, log, ledger = self._open()
        try:
            pool = archive.get_pool(pilot.config['pool_id'])
            if pool is None:
                raise ReadError(503, 'Raydium observed pool identity is unavailable.')
            snapshots = list(reversed(log.recent(pilot.config['pool_id'], 1000)))
            if not snapshots:
                raise ReadError(503, 'Raydium observed publication is warming up.')
            # Verifies sequence/hash continuity before publishing derived state.
            log.verify(); ledger.verify()
            now = time.time()
            state = ledger.summary(archive, log, at=now)
            ledger_proof = ledger.verify()
            events = [dict(row) for row in ledger.db.execute(
                'SELECT id,at,kind,data,content_sha256 FROM observed_ledger_events ORDER BY id')]
            unit, scale = self._denomination(archive, pilot.config['quote_mint'])
            return pilot, pool, snapshots, dict(ledger.state), state, ledger_proof, events, unit, scale, now
        except (ArchiveError, ObservedLogError, RaydiumObservedLedgerError) as exc:
            raise ReadError(503, 'Raydium observed publication cannot be verified.') from exc
        finally:
            ledger.close(); log.close(); archive.close()

    def document(self):
        pilot, pool, snapshots, ledger_state, state, ledger_proof, events, unit, scale, generated_at = self._snapshot()
        config = pilot.config
        latest, first = snapshots[-1], snapshots[0]
        initial = config['initial_quote_amount'] / scale
        equity = state['marked_equity_quote_amount'] / scale
        all_time_pnl = equity - initial
        positions_by_id = {item['id']: item for item in state['positions']}
        all_positions = list(ledger_state['positions'].values())
        open_positions, closed_positions = [], []
        for position in all_positions:
            mark = positions_by_id.get(position['id'], {})
            base_symbol = 'NEAR' if position['base_mint'] == config['base_mint'] else 'Base token'
            common = {'strategy_id': config['id'], 'strategy_label': 'NEAR / USDC observed trend',
                      'symbol': base_symbol, 'mint': position['base_mint'],
                      'entry_cap': position['cost_quote_amount'] / scale,
                      'entry_at': position['entry_at']}
            if position['status'] == 'open':
                current = mark['quote_amount'] / scale if mark.get('status') == 'marked' else None
                open_positions.append({**common, 'current_cap': current,
                    'pnl_usd': None if current is None else current - common['entry_cap'],
                    'status': 'impaired observed mark' if mark.get('status') == 'impaired' else 'observed mark'})
            else:
                exit_value = position['exit_quote_amount'] / scale
                closed_positions.append({**common, 'exit_cap': exit_value,
                    'pnl_usd': position['pnl_quote_amount'] / scale, 'exit_at': position['exit_at'],
                    'exit_reason': 'observed simulation exit'})
        closed_positions.sort(key=lambda item: item['exit_at'], reverse=True)
        today_events = sum(row['at'] >= generated_at - 86400 for row in events)
        closed_today_pnl = sum(item['pnl_usd'] for item in closed_positions if item['exit_at'] >= generated_at - 86400)
        report = {
            'config': {'entry_style': 'observed_trend', 'entry_cap_min': 0, 'entry_cap_max': 0,
                       'order_usd': config['order_quote_amount'] / scale, 'observation_seconds': 60,
                       'max_positions': 1, 'max_hold_seconds': config['max_hold_snapshots'] * 60, 'targets': []},
            'initial_usd': initial, 'equity_usd': equity, 'pnl_usd': all_time_pnl,
            # Intraday marked P/L is intentionally not inferred from one current mark.
            'pnl_24h_usd': closed_today_pnl, 'pnl_7d_usd': closed_today_pnl, 'pnl_30d_usd': closed_today_pnl,
            'realized_net_pnl_usd': state['realized_pnl_quote_amount'] / scale,
            'closed_count': len(closed_positions),
            'closed_win_rate': (sum(item['pnl_usd'] > 0 for item in closed_positions) / len(closed_positions)) if closed_positions else None,
            'open_position_count': len(open_positions), 'pending_count': 0, 'total_entries': len(events),
            'trade_counts': {'today': today_events, 'all': len(events)}, 'activity_counts': {'today': today_events, 'all': len(events)},
        }
        strategy = {'id': config['id'], 'label': 'NEAR / USDC observed trend', 'family': 'observed_trend',
                    'description': 'One isolated observed-simulation pilot: three snapshots, a 0.5% probe trend, and a 10-snapshot maximum hold.',
                    'report': report}
        point = {'at': latest['observed_at'], 'value': equity}
        return {'as_of': latest['observed_at'], 'market_tracking_started_at': first['observed_at'],
                'service': {'market': 'raydium-cpmm', 'assurance': 'observed_simulation_only',
                            'denomination': unit + ' quote token; no USD FX valuation'},
                'strategies': [strategy], 'open_positions': open_positions, 'closed_positions': closed_positions,
                'candidates': [],
                'portfolio': {'initial_usd': initial, 'equity_usd': equity, 'pnl_usd': all_time_pnl,
                              'today_pnl_usd': closed_today_pnl, 'cash_usd': state['cash_quote_amount'] / scale,
                              'open_positions': len(open_positions),
                              'allocations': [{'id': strategy['id'], 'label': strategy['label'], 'initial_usd': initial,
                                               'equity_usd': equity, 'pnl_usd': all_time_pnl, 'allocation_fraction': 1,
                                               'activity_counts': report['activity_counts']}],
                              # One current mark is not a historical curve.
                              'equity_curve': [point], 'equity_curves': {'today': [point], '7d': [point], '30d': [point], 'all': [point]}},
                'community': {'token_status': 'Observed Raydium pilot', 'telegram_url': '', 'developer_url': ''},
                'observed_evidence': {'pool_id': config['pool_id'], 'pool_address': pool['pool_address'],
                                      'latest_sequence': latest['sequence'], 'latest_snapshot_sha256': latest['sha256'],
                                      'snapshot_count': len(snapshots), 'ledger_proof': ledger_proof,
                                      'limitations': state['limitations']},
                '_snapshot': {'generated_at': generated_at, 'source_as_of': latest['observed_at'],
                              'age_seconds': round(max(0, generated_at - latest['observed_at']), 1),
                              'stale': generated_at - latest['observed_at'] > 120,
                              'consistency': 'Read-only observed snapshot log and isolated paper ledger.'}}

    def response(self, path, query):
        document = self.document()
        if path in ('/api/public', '/api/public/summary'):
            return encode(document)
        if path == '/api/public/live-feed':
            return encode({'events': [], '_snapshot': document['_snapshot']})
        raise ReadError(404, 'Raydium public endpoint not found')

    def export(self, path):
        if path not in ('/api/public/history.csv', '/api/public/history.jsonl.gz'):
            raise ReadError(404, 'Raydium export not found')
        _, _, _, _, _, _, rows, _, _, _ = self._snapshot()
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=('event_id', 'at', 'kind', 'data_json', 'content_sha256'))
        writer.writeheader()
        for row in rows:
            writer.writerow({'event_id': row['id'], 'at': row['at'], 'kind': row['kind'],
                             'data_json': row['data'], 'content_sha256': row['content_sha256']})
        body = output.getvalue().encode()
        if path.endswith('.jsonl.gz'):
            parsed = csv.DictReader(io.StringIO(body.decode()))
            return gzip.compress(b''.join(encode(row) + b'\n' for row in parsed), compresslevel=9, mtime=0)
        return body
