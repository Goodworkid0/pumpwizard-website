"""Loopback-only, read-only dashboard. No order or wallet endpoints exist."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .dashboard import build_view
from .brief import BriefService


def serve(engine, port, brain=None, account=None):
    root = Path(__file__).parent.parent / 'web'
    settings = json.loads((root.parent / 'dashboard.config.json').read_text())
    if settings['summary_provider'] not in ('openai', 'ollama', 'disabled'):
        raise ValueError('Unknown summary provider')
    if (settings['summary_refresh_seconds'] < 30 or not 0 <= settings['summary_daily_budget_usd'] <= 10
            or not 0 <= settings['summary_daily_request_limit'] <= 1000):
        raise ValueError('Invalid summary refresh or usage limit')
    with engine.lock:
        db_path = Path(engine.db.execute('PRAGMA database_list').fetchone()[2])
    briefs = BriefService(settings, db_path.parent / (db_path.stem + '.briefs.json'))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('Host') not in (f'localhost:{port}', f'127.0.0.1:{port}'):
                self.send_error(403)
                return
            if self.headers.get('Sec-Fetch-Site') == 'cross-site':
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == '/api/dashboard':
                query = parse_qs(urlsplit(self.path).query)
                try:
                    builder=account.build if account else lambda now,**kw:build_view(engine,now,**kw)
                    extra={k:query.get(k,[v])[0] for k,v in {'ledger_page':'1','ledger_strategy':'all',
                        'ledger_action':'all','ledger_search':''}.items()} if account else {}
                    data = builder(time.time(), period=query.get('period', ['live'])[0],
                        outcome=query.get('outcome', ['all'])[0], sort=query.get('sort', ['newest'])[0],
                        page=query.get('page', ['1'])[0], search=query.get('search', [''])[0],**extra)
                    data['brain'] = brain.view() if brain else None
                    data['ownership_required'] = hasattr(engine,'entry_policy')
                    data['brief'] = briefs.get(data)
                    body, mime = json.dumps(data, allow_nan=False).encode(), 'application/json'
                except ValueError:
                    self.send_error(400, 'Invalid dashboard filter')
                    return
            elif path == '/api/coin-memory' and brain:
                query=parse_qs(urlsplit(self.path).query).get('search',[''])[0]
                body,mime=json.dumps({'coins':brain.memory.search(query,time.time())},allow_nan=False).encode(),'application/json'
            elif path == '/api/state':
                data = engine.report()
                data['server_at'] = time.time()
                if account:
                    unified=account.build(data['server_at']);p=unified['portfolio'];f=unified['performance']
                    data={'mode':'PAPER ONLY','source':data['source'],'server_at':data['server_at'],
                        'scope':'unified_account','short_term_report':data,
                        'portfolio':p,'performance':f,'account':unified['account'],
                        'cash_usd':p['cash_usd'],'equity_usd':p['equity_usd'],'total_pnl_usd':p['total_pnl_usd'],
                        'realized_net_pnl_usd':p['realized_net_pnl_usd'],'unrealized_pnl_usd':p['unrealized_pnl_usd'],
                        'closed_trades':f['closed_count'],'positions':unified['positions'],
                        'equity_curve':unified['equity_curve'],'last_at':p['last_at'],
                        'win_rate':f['win_rate'],'modeled_fees_usd':f['fees_usd']}
                body, mime = json.dumps(data, allow_nan=False).encode(), 'application/json'
            elif path == '/api/account' and account:
                data=account.build(time.time())
                body,mime=json.dumps({'account':data['account'],'portfolio':data['portfolio']},allow_nan=False).encode(),'application/json'
            elif path == '/api/account.csv' and account:
                body,mime=account.export_statement().encode(),'text/csv; charset=utf-8'
            elif path == '/api/ladder' and brain and brain.ladder_enabled:
                body,mime=json.dumps(brain.runner.report(),allow_nan=False).encode(),'application/json'
            elif path == '/api/ladder.csv' and brain and brain.ladder_enabled:
                body,mime=brain.runner.export_csv().encode(),'text/csv; charset=utf-8'
            elif path == '/api/trades.csv':
                body, mime = (account.export_trades() if account else engine.export_csv()).encode(), 'text/csv; charset=utf-8'
            elif path in ('/', '/app.js', '/style.css'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css'}[path]
                body = (root / name).read_bytes()
                mime = {'/': 'text/html', '/app.js': 'text/javascript', '/style.css': 'text/css'}[path]
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
