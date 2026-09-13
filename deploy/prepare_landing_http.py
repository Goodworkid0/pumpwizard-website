"""Add only the landing HTTP vhost, with candidate validation and rollback.

Run on idb-host with the SHA256 of the reviewed existing proxy configuration.
Does not issue certificates or modify DNS. No shared containers are restarted.
"""
import hashlib
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def run(*args):
    return subprocess.run(args, check=True, capture_output=True, text=True).stdout


def check(url, domain, port, expected=200):
    status = run('curl', '--max-time', '20', '-sS', '--resolve',
                 f'{domain}:{port}:127.0.0.1', '-o', '/dev/null',
                 '-w', '%{http_code}', url)
    if status != str(expected):
        raise RuntimeError(f'{url} returned {status}, expected {expected}')
    print(f'{url}: {status}', flush=True)


def existing_checks():
    check('https://insidedb.ai/', 'insidedb.ai', 443)
    check('https://www.insidedb.ai/', 'www.insidedb.ai', 443)
    check('https://insidedb.ai/health', 'insidedb.ai', 443)


def main():
    config = Path('/opt/insidedb/nginx/insidedb.conf')
    original = config.read_bytes()
    if len(sys.argv) != 2 or hashlib.sha256(original).hexdigest() != sys.argv[1]:
        raise RuntimeError('Proxy differs from reviewed configuration; inspect again')
    if b'pumpwizard.fun' in original:
        raise RuntimeError('Domain already configured; inspect instead of duplicating')
    existing_checks()
    folder = Path(tempfile.mkdtemp(prefix='landing-http-', dir='/opt/pump-paper/deploy'))
    backup = folder / 'original.conf'
    backup.write_bytes(original)
    candidate = folder / 'candidate.conf'
    candidate.write_bytes(original + b'\n' + Path(__file__).with_name('pumpwizard-http.conf').read_bytes())
    print('Backup:', backup, flush=True)
    run('docker', 'run', '--rm', '--network', 'insidedb_insidedb-network',
        '-v', f'{candidate}:/etc/nginx/conf.d/default.conf:ro',
        '-v', '/etc/letsencrypt:/etc/letsencrypt:ro',
        '-v', '/opt/insidedb/nginx/www:/var/www/certbot:ro',
        'nginx:1.25-alpine', 'nginx', '-t')
    if config.read_bytes() != original:
        raise RuntimeError('Proxy changed during validation; no changes installed')
    try:
        # Preserve the inode of the existing single-file Docker bind mount.
        config.write_bytes(candidate.read_bytes())
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-t')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-s', 'reload')
        time.sleep(1)
        check('http://pumpwizard.fun/', 'pumpwizard.fun', 80)
        check('http://www.pumpwizard.fun/', 'www.pumpwizard.fun', 80)
        check('http://pumpwizard.fun/style.css', 'pumpwizard.fun', 80)
        body = run('curl', '--max-time', '20', '-fsS', '--resolve',
                   'pumpwizard.fun:80:127.0.0.1', 'http://pumpwizard.fun/')
        if 'Our website and service are in preparation.' not in body:
            raise RuntimeError('Landing page content mismatch')
        existing_checks()
    except Exception:
        config.write_bytes(original)
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-t')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-s', 'reload')
        print('Restored original proxy configuration', flush=True)
        raise
    print('HTTP landing route installed; DNS and HTTPS still pending.', flush=True)


if __name__ == '__main__':
    main()
