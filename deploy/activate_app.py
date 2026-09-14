"""Activate only PumpWizard's vhost after app checks, with proxy rollback."""
import argparse
import hashlib
from pathlib import Path
import subprocess
import tempfile
import time


def run(*args):
    return subprocess.run(args,check=True,capture_output=True,text=True,timeout=120).stdout


def check(url,domain,expected=200,method='GET'):
    status=run('curl','--max-time','20','-sS','--resolve',domain+':443:127.0.0.1',
               '-X',method,'-o','/dev/null','-w','%{http_code}',url)
    if status!=str(expected): raise RuntimeError('Unexpected HTTP status '+status+' for '+url)


def existing():
    for domain,path in [('insidedb.ai','/'),('www.insidedb.ai','/'),('insidedb.ai','/health')]:
        check('https://'+domain+path,domain)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate',type=Path,required=True)
    parser.add_argument('--expected-sha256',required=True)
    parser.add_argument('--apply',action='store_true')
    parser.add_argument('--restore-landing',type=Path)
    args=parser.parse_args()
    source=Path('/opt/pump-paper/deploy/pumpwizard.conf')
    original=source.read_bytes()
    if args.restore_landing:
        restored=args.restore_landing.read_bytes()
        if (original!=args.candidate.read_bytes() or
                hashlib.sha256(restored).hexdigest()!=args.expected_sha256):
            raise RuntimeError('Rollback fingerprints differ; inspect manually')
    elif hashlib.sha256(original).hexdigest()!=args.expected_sha256: raise RuntimeError('Vhost changed; inspect again')
    active=run('docker','exec','insidedb-nginx','cat','/etc/nginx/conf.d/pumpwizard.conf').encode()
    if active!=original: raise RuntimeError('Host and running PumpWizard vhosts differ')
    existing()
    folder=Path(tempfile.mkdtemp(prefix='app-cutover-',dir=source.parent))
    (folder/'original.conf').write_bytes(original)
    shared=run('docker','exec','insidedb-nginx','cat','/etc/nginx/conf.d/default.conf')
    candidate=restored if args.restore_landing else args.candidate.read_bytes()
    (folder/'candidate.conf').write_bytes(shared.encode()+b'\n'+candidate)
    image=run('docker','inspect','insidedb-nginx','--format','{{.Config.Image}}').strip()
    run('docker','run','--rm','--network','insidedb_insidedb-network',
        '-v',str(folder/'candidate.conf')+':/etc/nginx/conf.d/default.conf:ro',
        '-v','/etc/letsencrypt:/etc/letsencrypt:ro',
        '-v','/opt/insidedb/nginx/www:/var/www/certbot:ro',image,'nginx','-t')
    if not args.apply:
        print('Candidate proxy configuration valid; no live routes changed.')
        return
    if not args.restore_landing:
        run('docker','exec','pump-paper','python3','-c',
            "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8765/api/public/health',timeout=10)")
    if source.read_bytes()!=original: raise RuntimeError('Vhost changed during validation')
    try:
        source.write_bytes(candidate)
        run('docker','cp',str(source),'insidedb-nginx:/etc/nginx/conf.d/pumpwizard.conf')
        run('docker','exec','insidedb-nginx','nginx','-t')
        run('docker','exec','insidedb-nginx','nginx','-s','reload')
        time.sleep(2)
        if args.restore_landing:
            check('https://pumpwizard.fun/','pumpwizard.fun')
            check('https://pumpwizard.fun/api/public/health','pumpwizard.fun',404)
            existing()
            print('Landing restored; server ledger left untouched.')
            return
        for path in ('/','/engine','/research','/downloads','/socials','/affiliate','/contact','/legal',
                     '/assets/brand/option-b-orbital-spark-logo-512.png','/assets/pump-logomark.svg',
                     '/assets/raydium-logomark.svg','/api/public/health','/api/public/catalog',
                     '/api/public/history.jsonl.gz','/api/public/research.jsonl.gz','/page-shell.css'):
            check('https://pumpwizard.fun'+path,'pumpwizard.fun')
        for path in ('/data/service.env','/.git/config','/acp','/api/state','/api/public'):
            check('https://pumpwizard.fun'+path,'pumpwizard.fun',404)
        check('https://pumpwizard.fun/api/public/catalog','pumpwizard.fun',403,'POST')
        check('https://www.pumpwizard.fun/','www.pumpwizard.fun',301)
        existing()
    except Exception:
        source.write_bytes(original)
        run('docker','cp',str(source),'insidedb-nginx:/etc/nginx/conf.d/pumpwizard.conf')
        run('docker','exec','insidedb-nginx','nginx','-t')
        run('docker','exec','insidedb-nginx','nginx','-s','reload')
        raise
    print('PumpWizard application vhost verified. Rollback config: '+str(folder/'original.conf'))


if __name__=='__main__': main()
