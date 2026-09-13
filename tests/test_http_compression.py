import unittest
import gzip
import urllib.request
from paper.server import accepts_gzip


class CompressionTests(unittest.TestCase):
    def test_negotiation(self):
        for header in ('gzip','br, gzip','GZIP; q=0.5','*;q=1'):
            self.assertTrue(accepts_gzip(header),header)
        for header in ('','br','gzip;q=0','gzip;q=bad','gzip;q=0, *;q=1','gzip;q=2'):
            self.assertFalse(accepts_gzip(header),header)

    def test_http_body_and_head(self):
        from paper.server import serve
        server=serve(None,0)
        try:
            url='http://127.0.0.1:'+str(server.server_port)+'/research'
            with urllib.request.urlopen(url,timeout=5) as response: plain=response.read()
            request=urllib.request.Request(url,headers={'Accept-Encoding':'gzip'})
            with urllib.request.urlopen(request,timeout=5) as response:
                data=response.read()
                self.assertEqual(response.headers['Content-Encoding'],'gzip')
                self.assertEqual(int(response.headers['Content-Length']),len(data))
                self.assertEqual(gzip.decompress(data),plain)
                self.assertLess(len(data),len(plain))
            request=urllib.request.Request(url,headers={'Accept-Encoding':'gzip'},method='HEAD')
            with urllib.request.urlopen(request,timeout=5) as response:
                self.assertEqual(response.read(),b'')
                self.assertEqual(int(response.headers['Content-Length']),len(data))
        finally: server.shutdown(); server.server_close()
