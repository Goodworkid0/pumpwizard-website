"""Enable the landing origin's TLS, preserving HTTP until edge TLS is ready.

Run on idb-host after issuing the pumpwizard.fun certificate. Pass --redirect
only after Cloudflare uses Full (strict) and its public certificate is active.
"""
from pathlib import Path
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, '/opt/pump-paper/deploy')
from prepare_landing_http import run, check, existing_checks


def main():
    root = Path('/opt/pump-paper/deploy')
    source = root / 'pumpwizard.conf'
    original = source.read_bytes()
    running = run('docker', 'exec', 'insidedb-nginx', 'cat',
                  '/etc/nginx/conf.d/pumpwizard.conf').encode()
    if original != running:
        raise RuntimeError('Host and running vhost differ; inspect first')
    redirect = sys.argv[1:] == ['--redirect']
    if sys.argv[1:] and not redirect:
        raise RuntimeError('Only --redirect is accepted')
    template = (root / 'nginx-landing.conf.template').read_text()
    rendered = template.replace('__DOMAIN__', 'pumpwizard.fun')
    if not redirect:
        # Leave the current public HTTP route usable during edge issuance.
        rendered = (root / 'pumpwizard-http.conf').read_text() + '\nserver {\n' + rendered.split('\nserver {\n', 2)[2]
    else:
        for domain in ('pumpwizard.fun', 'www.pumpwizard.fun'):
            run('curl', '--max-time', '20', '-fsS', f'https://{domain}/')
    existing_checks()
    backup = Path(tempfile.mkdtemp(prefix='landing-https-', dir=root))
    (backup / 'original.conf').write_bytes(original)
    default = run('docker', 'exec', 'insidedb-nginx', 'cat',
                  '/etc/nginx/conf.d/default.conf')
    candidate = backup / 'candidate.conf'
    candidate.write_text(default + '\n' + rendered)
    run('docker', 'run', '--rm', '--network', 'insidedb_insidedb-network',
        '-v', f'{candidate}:/etc/nginx/conf.d/default.conf:ro',
        '-v', '/etc/letsencrypt:/etc/letsencrypt:ro',
        '-v', '/opt/insidedb/nginx/www:/var/www/certbot:ro',
        'nginx:1.25-alpine', 'nginx', '-t')
    if source.read_bytes() != original:
        raise RuntimeError('Vhost changed during validation')
    try:
        source.write_text(rendered)
        run('docker', 'cp', str(source),
            'insidedb-nginx:/etc/nginx/conf.d/pumpwizard.conf')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-t')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-s', 'reload')
        time.sleep(2)
        for domain in ('pumpwizard.fun', 'www.pumpwizard.fun'):
            check(f'https://{domain}/', domain, 443)
            check(f'https://{domain}/style.css', domain, 443)
            check(f'http://{domain}/', domain, 80, 301 if redirect else 200)
            if redirect:
                for scheme in ('http', 'https'):
                    body = run('curl', '--max-time', '20', '--max-redirs', '4',
                               '-fsSL', f'{scheme}://{domain}/')
                    if not any(marker in body for marker in ('Our website and service are in preparation.', 'THE LAB IS BREWING')):
                        raise RuntimeError('Public landing content mismatch')
        existing_checks()
    except Exception:
        source.write_bytes(original)
        run('docker', 'cp', str(source),
            'insidedb-nginx:/etc/nginx/conf.d/pumpwizard.conf')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-t')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-s', 'reload')
        raise
    hook = Path('/etc/letsencrypt/renewal-hooks/deploy/pumpwizard-nginx')
    hook_text = '''#!/bin/sh
set -eu
[ "${RENEWED_LINEAGE:-}" = /etc/letsencrypt/live/pumpwizard.fun ] || exit 0
/usr/bin/docker exec insidedb-nginx nginx -t
/usr/bin/docker exec insidedb-nginx nginx -s reload
'''
    if hook.exists() and hook.read_text() != hook_text:
        raise RuntimeError('Unexpected renewal hook; inspect manually')
    hook.write_text(hook_text)
    hook.chmod(0o755)
    print('Origin HTTPS active; HTTP redirect:', redirect, 'Backup:', backup)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError as exc:
        print(exc.stdout, exc.stderr, file=sys.stderr)
        raise
