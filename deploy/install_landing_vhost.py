"""Install Pump Wizard as a separate vhost without recreating the shared proxy."""
from pathlib import Path
import tempfile
import time
from prepare_landing_http import run, check, existing_checks


def main():
    root = Path('/opt/pump-paper/deploy')
    source = root / 'pumpwizard.conf'
    compose = Path('/opt/insidedb/docker-compose.platform.yml')
    original = compose.read_text()
    anchor = '      - ./nginx/insidedb.conf:/etc/nginx/conf.d/default.conf:ro\n'
    mount = '      - /opt/pump-paper/deploy/pumpwizard.conf:/etc/nginx/conf.d/pumpwizard.conf:ro\n'
    if original.count(anchor) != 1 or 'pumpwizard.conf' in original or source.exists():
        raise RuntimeError('Unexpected existing vhost configuration; inspect manually')
    run('docker', 'exec', 'insidedb-nginx', 'test', '!', '-e', '/etc/nginx/conf.d/pumpwizard.conf')
    existing_checks()
    backup = Path(tempfile.mkdtemp(prefix='landing-vhost-', dir=root))
    (backup / 'compose.original.yml').write_text(original)
    mounted = run('docker', 'exec', 'insidedb-nginx', 'cat', '/etc/nginx/conf.d/default.conf')
    (backup / 'default.conf').write_text(mounted)
    candidate = backup / 'candidate.conf'
    candidate.write_text(mounted + '\n' + (root / 'pumpwizard-http.conf').read_text())
    run('docker', 'run', '--rm', '--network', 'insidedb_insidedb-network',
        '-v', f'{candidate}:/etc/nginx/conf.d/default.conf:ro',
        '-v', '/etc/letsencrypt:/etc/letsencrypt:ro',
        '-v', '/opt/insidedb/nginx/www:/var/www/certbot:ro',
        'nginx:1.25-alpine', 'nginx', '-t')
    if compose.read_text() != original:
        raise RuntimeError('Compose changed while validating; aborting')
    try:
        source.write_bytes((root / 'pumpwizard-http.conf').read_bytes())
        compose.write_text(original.replace(anchor, anchor + mount))
        run('docker', 'compose', '-f', str(compose), 'config', '--quiet')
        # Copy into the running container now; the Compose mount preserves this
        # vhost on a future recreation. No existing container is recreated here.
        run('docker', 'cp', str(source), 'insidedb-nginx:/etc/nginx/conf.d/pumpwizard.conf')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-t')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-s', 'reload')
        time.sleep(2)
        check('http://pumpwizard.fun/', 'pumpwizard.fun', 80)
        check('http://www.pumpwizard.fun/', 'www.pumpwizard.fun', 80)
        check('http://pumpwizard.fun/style.css', 'pumpwizard.fun', 80)
        body = run('curl', '--max-time', '20', '-fsS', '--resolve',
                   'pumpwizard.fun:80:127.0.0.1', 'http://pumpwizard.fun/')
        if 'Our website and service are in preparation.' not in body:
            raise RuntimeError('Landing content mismatch')
        existing_checks()
    except Exception:
        compose.write_text(original)
        run('docker', 'exec', 'insidedb-nginx', 'rm', '-f', '/etc/nginx/conf.d/pumpwizard.conf')
        if source.exists():
            source.rename(backup / 'failed-pumpwizard.conf')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-t')
        run('docker', 'exec', 'insidedb-nginx', 'nginx', '-s', 'reload')
        raise
    print('Separate HTTP vhost active. Backup:', backup, flush=True)


if __name__ == '__main__':
    main()
