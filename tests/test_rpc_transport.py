import io
import unittest
import urllib.error
from unittest.mock import patch
from paper.ownership import Rpc, EvidenceUnavailable


class RpcTransportTests(unittest.TestCase):
    config={'rpc_env':'SOLANA_RPC_URL','request_spacing_seconds':.35}

    def test_public_endpoint_is_current_and_paced_without_changing_private_setting(self):
        with patch.dict('os.environ',{},clear=True):
            rpc=Rpc(self.config)
            self.assertEqual(rpc.url,'https://api.mainnet.solana.com')
            self.assertEqual(rpc.spacing,1.1)
        with patch.dict('os.environ',{'SOLANA_RPC_URL':'https://rpc.example.test/key'}):
            self.assertEqual(Rpc(self.config).spacing,.35)

    def test_http_rate_limit_respects_retry_after_without_sending_more_requests(self):
        with patch.dict('os.environ',{},clear=True),patch('paper.ownership.time.monotonic',return_value=100),patch('paper.ownership.time.sleep'),patch('paper.ownership.urllib.request.urlopen') as request:
            rpc=Rpc(self.config)
            request.side_effect=urllib.error.HTTPError(rpc.url,429,'rate limited',{'Retry-After':'180'},None)
            with self.assertRaisesRegex(EvidenceUnavailable,'429'):rpc('getAccountInfo',[])
            self.assertEqual(rpc.retry_at,280)
            with self.assertRaisesRegex(EvidenceUnavailable,'cooling down'):rpc('getAccountInfo',[])
            self.assertEqual(request.call_count,1)

    def test_json_rate_limit_also_cools_down_and_never_authorizes_writes(self):
        with patch.dict('os.environ',{},clear=True),patch('paper.ownership.time.monotonic',return_value=100),patch('paper.ownership.time.sleep'),patch('paper.ownership.urllib.request.urlopen',return_value=io.BytesIO(b'{"error":{"code":-32005}}')) as request:
            rpc=Rpc(self.config)
            with self.assertRaisesRegex(EvidenceUnavailable,'rate limited'):rpc('getTransaction',[])
            self.assertEqual(rpc.retry_at,160)
            with self.assertRaises(ValueError):rpc('sendTransaction',[])
            self.assertEqual(request.call_count,1)

    def test_retry_after_http_date(self):
        with patch.dict('os.environ',{},clear=True),patch('paper.ownership.time.monotonic',return_value=100),patch('paper.ownership.time.time',return_value=0):
            rpc=Rpc(self.config);rpc._backoff({'Retry-After':'Thu, 01 Jan 1970 00:02:00 GMT'})
            self.assertEqual(rpc.retry_at,220)
