"""Read-only access to explicitly reviewed, prebuilt public research archives."""
import json
from pathlib import Path
import re
from .public_read import ID, ReadError, encode

HASH = re.compile(r'[a-f0-9]{64}\Z')


class Packages:
    def __init__(self, root): self.root = Path(root)

    def records(self):
        path = self.root/'registry.json'
        if not path.exists(): return []
        if path.is_symlink() or path.stat().st_size > 1_000_000: raise ReadError(503,'Package registry unavailable')
        try:
            data = json.loads(path.read_text())
            if data['schema'] != 'pumpwizard.public-packages/v1': raise ValueError()
            for row in data['packages']:
                if not ID.fullmatch(row['instance']) or not HASH.fullmatch(row['sha256']): raise ValueError()
                if type(row['bytes']) is not int or not 0 < row['bytes'] <= 2*1024**3: raise ValueError()
            return data['packages']
        except (KeyError, TypeError, ValueError): raise ReadError(503,'Package registry unavailable') from None

    def response(self, query):
        instance = query.get('instance',[''])[0]
        if instance and not ID.fullmatch(instance): raise ReadError(400,'Invalid instance')
        return encode({'packages': [{**row, 'url':'/api/public/packages/'+row['sha256']+'.zip'}
                                   for row in self.records() if not instance or row['instance']==instance]})

    def open(self, ident):
        if not HASH.fullmatch(ident): raise ReadError(404,'Package not found')
        row = next((r for r in self.records() if r['sha256']==ident),None)
        path = self.root/(ident+'.zip')
        if row is None or path.is_symlink() or not path.is_file(): raise ReadError(404,'Package not found')
        stream = path.open('rb')
        import os
        if os.fstat(stream.fileno()).st_size != row['bytes']:
            stream.close(); raise ReadError(503,'Package size differs from published record')
        return row, stream


def byte_range(header, size):
    if header is None: return 0,size-1
    match = re.fullmatch(r'bytes=(\d*)-(\d*)',header)
    if not match or not any(match.groups()): raise ReadError(416,'Unsupported byte range')
    a,b = match.groups()
    start,end = (int(a),int(b) if b else size-1) if a else (max(0,size-int(b)),size-1)
    if not 0 <= start <= end or start >= size: raise ReadError(416,'Range outside package')
    return start,min(end,size-1)
