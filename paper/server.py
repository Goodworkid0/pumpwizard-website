"""Public read-only dashboard; operator APIs are not exposed by this listener."""
import json
import gzip
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

from .public_read import PublicReadService, ReadError
from .package_registry import Packages, byte_range


def accepts_gzip(header):
    choices={}
    for item in header.lower().split(','):
        parts=item.strip().split(';'); quality=1.0
        for parameter in parts[1:]:
            if parameter.strip().startswith('q='):
                try: quality=float(parameter.strip()[2:])
                except ValueError: quality=0
        choices[parts[0].strip()]=quality
    return 0<choices.get('gzip',choices.get('*',0))<=1


def serve(engine, port, brain=None, account=None, bind_host='127.0.0.1'):
    root = Path(__file__).parent.parent / 'web'
    public = PublicReadService(brain) if brain and brain.ladder_enabled else None
    packages = Packages((brain.root if brain else root.parent)/'data/public-packages')
    package_slots = threading.BoundedSemaphore(2)

    class Handler(BaseHTTPRequestHandler):
        server_version = 'PumpWizard'
        sys_version = ''

        def end_headers(self):
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'strict-origin-when-cross-origin')
            self.send_header('X-Frame-Options', 'DENY')
            super().end_headers()

        def do_GET(self):
            if self.headers.get('Host') not in (f'localhost:{self.server.server_port}', f'127.0.0.1:{self.server.server_port}'):
                self.send_error(403)
                return
            if self.headers.get('Sec-Fetch-Site') == 'cross-site' and self.headers.get('Sec-Fetch-Mode') != 'navigate':
                self.send_error(403)
                return
            path = urlsplit(self.path).path
            if len(self.path) > 2048:
                self.send_error(414)
                return
            # Fail closed even if a proxy accidentally forwards an operator URL.
            if ((path.startswith('/api/') and not path.startswith('/api/public/'))
                    or path == '/acp' or path.startswith('/acp.')):
                self.send_error(404)
                return
            if path == '/api/public/health':
                data = public.health() if public else {'status': 'unavailable', 'checks': {'snapshot': False}}
                body = json.dumps(data, allow_nan=False).encode()
                self.send_response(200 if data['status'] == 'ok' else 503)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(body)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                try:
                    if self.command != 'HEAD': self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError): pass
                return
            # Engine is the public route. Preserve old links, including filters
            # and instance IDs, without serving a second canonical URL.
            if path == '/strategies' or path.startswith('/strategies/'):
                target = '/engine' + path[len('/strategies'):]
                query = urlsplit(self.path).query
                if query:
                    target += '?' + query
                self.send_response(308)
                self.send_header('Location', target)
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                return
            if path.startswith('/api/public') and public:
                try:
                    query = parse_qs(urlsplit(self.path).query)
                    if path == '/api/public/packages':
                        body, mime = packages.response(query), 'application/json'
                    elif path.startswith('/api/public/packages/') and path.endswith('.zip'):
                        self.send_package(path[len('/api/public/packages/'):-4])
                        return
                    elif path in ('/api/public/history.csv', '/api/public/research.csv'):
                        body, mime = public.export(path), 'text/csv; charset=utf-8'
                    elif path.startswith('/api/public/strategies/') and path.endswith('/trades'):
                        body = public.history(path[len('/api/public/strategies/'):-len('/trades')], query)
                        mime = 'application/json'
                    else:
                        body, mime = public.response(path, query), 'application/json'
                except ReadError as exc:
                    body = json.dumps({'error': str(exc)}).encode()
                    self.send_response(exc.status)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.send_header('Cache-Control', 'no-store')
                    self.send_header('Retry-After', '15')
                    self.end_headers()
                    try:
                        if self.command != 'HEAD': self.wfile.write(body)
                    except (BrokenPipeError, ConnectionResetError): pass
                    return
            elif path in ('/downloads', '/downloads.css', '/socials', '/assets/brand/option-b-orbital-spark-logo-512.png',
                          '/assets/pump-logomark.svg', '/assets/raydium-logomark.svg'):
                name = {'/downloads': 'downloads.html', '/downloads.css': 'downloads.css', '/socials': 'contact.html',
                        '/assets/brand/option-b-orbital-spark-logo-512.png': 'assets/brand/option-b-orbital-spark-logo-512.png',
                        '/assets/pump-logomark.svg': 'assets/pump-logomark.svg', '/assets/raydium-logomark.svg': 'assets/raydium-logomark.svg'}[path]
                body = (root / name).read_bytes()
                mime = {'/downloads': 'text/html', '/downloads.css': 'text/css', '/socials': 'text/html',
                        '/assets/brand/option-b-orbital-spark-logo-512.png': 'image/png',
                        '/assets/pump-logomark.svg': 'image/svg+xml', '/assets/raydium-logomark.svg': 'image/svg+xml'}[path]
            elif path in ('/', '/app.js', '/nav.js', '/activity-feed.css', '/activity-controls.css', '/activity-token.css', '/leaderboard.css', '/leaderboard-fixes.css', '/market-radar.css', '/market-context.css', '/agent-live.css', '/agent-share.css', '/agent-hero.css', '/style.css', '/home-extra.css', '/acp', '/acp.js', '/acp.css',
                          '/strategy', '/strategy.js', '/strategy.css', '/engine', '/strategies.js',
                          '/strategies.css', '/legal', '/legal.css', '/hero-refresh.css',
                          '/positions-refresh.css', '/research', '/research.js', '/research.css', '/research-conditions.css',
                          '/affiliate', '/affiliate.css', '/contact', '/contact.css', '/contact.js', '/page-shell.css', '/strategy-analysis.js', '/strategy-comparison.js') or path.startswith('/engine/'):
                name = {'/': 'index.html', '/app.js': 'app.js', '/nav.js': 'nav.js', '/activity-feed.css': 'activity-feed.css', '/activity-controls.css': 'activity-controls.css', '/activity-token.css': 'activity-token.css', '/leaderboard.css': 'leaderboard.css', '/leaderboard-fixes.css': 'leaderboard-fixes.css', '/market-radar.css': 'market-radar.css', '/market-context.css': 'market-context.css', '/agent-live.css': 'agent-live.css', '/agent-share.css': 'agent-share.css', '/agent-hero.css': 'agent-hero.css', '/style.css': 'style.css',
                        '/home-extra.css': 'home-extra.css', '/acp': 'acp.html', '/acp.js': 'acp.js', '/acp.css': 'acp.css',
                        '/strategy': 'strategy.html', '/strategy.js': 'strategy.js',
                        '/strategy.css': 'strategy.css', '/engine': 'strategies.html',
                        '/strategies.js': 'strategies.js', '/strategies.css': 'strategies.css',
                        '/legal': 'legal.html', '/legal.css': 'legal.css',
                        '/hero-refresh.css': 'hero-refresh.css',
                        '/positions-refresh.css': 'positions-refresh.css', '/research': 'research.html',
                        '/research.js': 'research.js', '/research.css': 'research.css', '/research-conditions.css': 'research-conditions.css',
                        '/affiliate': 'affiliate.html', '/affiliate.css': 'affiliate.css',
                        '/contact': 'contact.html', '/contact.css': 'contact.css', '/contact.js': 'contact.js', '/page-shell.css': 'page-shell.css', '/strategy-analysis.js': 'strategy-analysis.js', '/strategy-comparison.js': 'strategy-comparison.js'}.get(path, 'strategy.html')
                body = (root / name).read_bytes()
                mime = {'/': 'text/html', '/app.js': 'text/javascript', '/nav.js': 'text/javascript', '/activity-feed.css': 'text/css', '/activity-controls.css': 'text/css', '/activity-token.css': 'text/css', '/leaderboard.css': 'text/css', '/leaderboard-fixes.css': 'text/css', '/market-radar.css': 'text/css', '/market-context.css': 'text/css', '/agent-live.css': 'text/css', '/agent-share.css': 'text/css', '/agent-hero.css': 'text/css', '/style.css': 'text/css',
                        '/home-extra.css': 'text/css', '/acp': 'text/html', '/acp.js': 'text/javascript', '/acp.css': 'text/css',
                        '/strategy': 'text/html', '/strategy.js': 'text/javascript',
                        '/strategy.css': 'text/css', '/engine': 'text/html',
                        '/strategies.js': 'text/javascript', '/strategies.css': 'text/css',
                        '/legal': 'text/html', '/legal.css': 'text/css',
                        '/hero-refresh.css': 'text/css',
                        '/positions-refresh.css': 'text/css', '/research': 'text/html',
                        '/research.js': 'text/javascript', '/research.css': 'text/css', '/research-conditions.css': 'text/css',
                        '/affiliate': 'text/html', '/affiliate.css': 'text/css',
                        '/contact': 'text/html', '/contact.css': 'text/css', '/contact.js': 'text/javascript', '/page-shell.css': 'text/css', '/strategy-analysis.js': 'text/javascript', '/strategy-comparison.js': 'text/javascript'}.get(path, 'text/html')
            else:
                self.send_error(404)
                return
            compressed=len(body)>=1024 and accepts_gzip(self.headers.get('Accept-Encoding',''))
            if compressed: body=gzip.compress(body,compresslevel=1,mtime=0)
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Vary','Accept-Encoding')
            if compressed: self.send_header('Content-Encoding','gzip')
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            self.end_headers()
            try:
                if self.command != 'HEAD': self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

        def send_package(self, ident):
            if not package_slots.acquire(blocking=False):
                raise ReadError(429, 'Package downloads are busy. Retry shortly.')
            try:
                row, stream = packages.open(ident)
                with stream:
                    start,end = byte_range(self.headers.get('Range'), row['bytes'])
                    partial = self.headers.get('Range') is not None
                    self.send_response(206 if partial else 200)
                    self.send_header('Content-Type','application/zip')
                    self.send_header('Content-Length',str(end-start+1))
                    self.send_header('Content-Disposition','attachment; filename="'+row['instance']+'-replay.zip"')
                    self.send_header('ETag','"'+ident+'"')
                    self.send_header('Accept-Ranges','bytes')
                    self.send_header('Cache-Control','public, max-age=31536000, immutable')
                    if partial: self.send_header('Content-Range',f'bytes {start}-{end}/{row["bytes"]}')
                    self.end_headers()
                    if self.command == 'HEAD': return
                    stream.seek(start); remaining=end-start+1; deadline=time.monotonic()+900
                    try:
                        while remaining and time.monotonic()<deadline:
                            block=stream.read(min(1024*1024,remaining))
                            if not block: break
                            self.wfile.write(block); remaining-=len(block)
                    except (OSError, TimeoutError): pass
            finally: package_slots.release()

        do_HEAD = do_GET

        def do_POST(self):
            self.send_error(405, 'This listener is read-only')

        do_PUT = do_DELETE = do_PATCH = do_POST

    class PublicServer(ThreadingHTTPServer):
        request_queue_size = 32
        slots = threading.BoundedSemaphore(32)

        def get_request(self):
            request, address = super().get_request()
            request.settimeout(10)
            return request, address

        def process_request(self, request, client_address):
            if not self.slots.acquire(blocking=False):
                self.shutdown_request(request)
                return
            try: super().process_request(request, client_address)
            except Exception:
                self.slots.release()
                raise

        def process_request_thread(self, request, client_address):
            try: super().process_request_thread(request, client_address)
            finally: self.slots.release()

        def server_close(self):
            if public: public.close()
            super().server_close()
    server = PublicServer((bind_host, port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
