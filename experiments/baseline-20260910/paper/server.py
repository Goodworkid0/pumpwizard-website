"""Loopback-only, read-only dashboard. No order or wallet endpoints exist."""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


def serve(engine, port):
    root = Path(__file__).parent.parent / 'web'

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.headers.get('Host') not in (f'localhost:{port}', f'127.0.0.1:{port}'):
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if path == '/api/state':
                data = engine.report()
                data['server_at'] = time.time()
                body, mime = json.dumps(data, allow_nan=False).encode(), 'application/json'
            elif path == '/api/trades.csv':
                body, mime = engine.export_csv().encode(), 'text/csv; charset=utf-8'
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
