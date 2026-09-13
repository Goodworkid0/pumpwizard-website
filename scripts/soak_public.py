"""Bounded, low-rate HTTP observation during real collector and backup activity."""
import argparse
import json
from pathlib import Path
import time
import urllib.request
import urllib.error
from urllib.parse import urlsplit


def run(url, seconds, interval=5, host=None):
    if urlsplit(url).scheme not in ('http','https'): raise ValueError('HTTP URL required')
    if not 10<=seconds<=3600 or not 2<=interval<=60: raise ValueError('Invalid observation bounds')
    rows=[]; end=time.monotonic()+seconds; index=0
    while time.monotonic()<end:
        path='/api/public/health' if index%2==0 else '/api/public/catalog?page_size=24'
        started=time.monotonic(); result={'at':time.time(),'path':path}
        headers={'User-Agent':'PumpWizard-ReleaseCheck/1.0','Accept':'application/json'}
        if host: headers['Host']=host
        request=urllib.request.Request(url.rstrip('/')+path,headers=headers)
        try:
            try: response=urllib.request.urlopen(request,timeout=15)
            except urllib.error.HTTPError as error:
                result['status']=error.code
                if error.code!=503: raise
                response=error
            with response:
                raw=response.read(2_000_001)
                if len(raw)>2_000_000: raise ValueError('Oversized response')
                body=json.loads(raw)
                result.update(status=response.status,healthy=body.get('status')=='ok' if index%2==0 else not body['_snapshot']['stale'])
                if not result['healthy']:
                    result['checks']=body.get('checks',{})
                    result['snapshot']=body.get('_snapshot',{})
        except (OSError,ValueError,KeyError): result.update(status=result.get('status',0),healthy=False)
        result['milliseconds']=round((time.monotonic()-started)*1000,2); rows.append(result); index+=1
        time.sleep(max(0,min(interval-(time.monotonic()-started),end-time.monotonic())))
    times=sorted(r['milliseconds'] for r in rows)
    return {'schema':'pumpwizard.public-soak/v1','seconds':seconds,'interval_seconds':interval,'samples':rows,
            'failures':sum(not r['healthy'] for r in rows),'p95_ms':times[min(len(times)-1,int(len(times)*.95))],
            'scope':'Low-rate real collector/backup coexistence check; not a saturation or 1000-instance capacity test.'}


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__); p.add_argument('--url',required=True)
    p.add_argument('--seconds',type=int,default=300); p.add_argument('--output',type=Path,required=True)
    p.add_argument('--host',help='Explicit Host header for a loopback staging listener')
    args=p.parse_args()
    if args.output.exists(): p.error('Output must be new')
    result=run(args.url,args.seconds,host=args.host)
    with args.output.open('x') as stream: json.dump(result,stream,indent=2)
    print(json.dumps({k:v for k,v in result.items() if k!='samples'},indent=2))
    raise SystemExit(bool(result['failures']))
