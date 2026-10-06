import tempfile
import unittest
from urllib.parse import parse_qs, urlsplit
from radar import Client, retry_delay, present
from backend import clean_title

class RadarTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.now=10000.;self.calls=[];self.reply=(200,{}, {'naturalCycle':'exclude','platforms':[{'id':'codex'}]})
        def fetch(path,key):
            self.calls.append(path);return self.reply
        self.client=Client(self.tmp.name,fetch,lambda:'test-secret',lambda:self.now)

    def test_cache_survives_restart(self):
        self.assertTrue(self.client.overview()['ok']);self.assertTrue(self.client.overview()['cached'])
        other=Client(self.tmp.name,lambda *a:self.fail('must use cache'),lambda:'changed-key',lambda:self.now)
        self.assertTrue(other.overview()['cached']);self.assertEqual(len(self.calls),1)

    def test_exclude_must_be_confirmed(self):
        self.reply=(200,{}, {'naturalCycle':'include','platforms':[]})
        self.assertFalse(self.client.overview()['ok'])

    def test_401_blocks_all_paths(self):
        self.reply=(401,{}, {'code':'invalid_api_key'})
        self.assertFalse(self.client.overview()['ok']);self.now+=1000
        self.assertFalse(self.client.service_status()['ok']);self.assertEqual(len(self.calls),1)

    def test_429_shared_and_longer_delay(self):
        self.reply=(429,{'Retry-After':'900'},{'retryAfterSeconds':1200})
        self.assertEqual(self.client.overview()['nextAttempt'],11200)
        self.now+=901;self.client.service_status();self.assertEqual(len(self.calls),1)
        self.now=11201;self.client.service_status();self.assertEqual(len(self.calls),2)

    def test_retry_http_date(self):
        self.assertEqual(retry_delay({'Retry-After':'Thu, 01 Jan 1970 00:30:00 GMT'},{'retryAfterSeconds':900},0),1800)

    def test_503_does_not_reuse_previous_success(self):
        self.client.overview();self.now+=601;self.reply=(503,{}, {'error':'snapshot unavailable'})
        result=self.client.overview()
        self.assertFalse(result['ok']);self.assertNotIn('data',result);self.assertEqual(result['error'],'snapshot unavailable')

    def test_cursor_is_opaque(self):
        self.client.overview();self.reply=(200,{}, {'records':[],'hasMore':False})
        cursor='a+/= ?&中'
        self.client.history('codex',before=cursor)
        self.assertEqual(parse_qs(urlsplit(self.calls[-1]).query)['before'],[cursor])
        with self.assertRaises(ValueError):self.client.history('codex',limit=15)
        with self.assertRaises(ValueError):self.client.history('codex',date='2026-02-30')
        with self.assertRaises(ValueError):self.client.history('unknown')

    def test_rolling_limit_shared(self):
        self.client.overview();self.reply=(200,{}, {'records':[]})
        for n in range(9):self.assertTrue(self.client.history('codex',before=str(n))['ok'])
        self.assertEqual(self.client.service_status()['httpStatus'],429);self.assertEqual(len(self.calls),10)

    def test_allowlist_and_redaction(self):
        with self.assertRaises(ValueError):self.client.request('https://elsewhere.test/')
        self.reply=(200,{}, {'naturalCycle':'exclude','platforms':[], 'extra':'test-secret'})
        self.assertNotIn('test-secret',str(self.client.overview()))
        self.assertNotIn('rr_live_',clean_title('rr_live_fake_sample 请接入'))

    def test_no_redirect_follow(self):
        self.reply=(302,{'Location':'https://elsewhere.test/'},{})
        self.assertFalse(self.client.overview()['ok']);self.assertEqual(len(self.calls),1)

    def test_status_stale_and_inference_stopped(self):
        overview={'ok':True,'data':{'platforms':[{'id':'codex','probability':8,'inferenceStatusLabel':'停止更新'}]}}
        status={'ok':True,'data':{'stale':True,'providers':[]}}
        view=present(overview,status,None)
        self.assertIn('停止更新',view['headline']);self.assertIn('已过期',' '.join(view['details']))

if __name__=='__main__':unittest.main()

class TransportTests(unittest.TestCase):
    def test_transport_redirect_and_headers(self):
        from unittest.mock import patch, MagicMock
        from radar import transport, HOST
        response=MagicMock();response.status=302
        response.getheader.return_value=None
        response.getheaders.return_value=[('Location','https://other.example/')]
        response.read1.side_effect=[b'{}',b'']
        conn=MagicMock();conn.getresponse.return_value=response
        with patch('radar.http.client.HTTPSConnection',return_value=conn) as factory:
            status,headers,data=transport('/service-status','test-only')
        self.assertEqual(status,302)
        self.assertEqual(factory.call_count,1)
        self.assertEqual(factory.call_args.args[0],HOST)
        self.assertEqual(conn.request.call_args.args,('GET','/radar-api/member/v1/service-status'))
        self.assertEqual(conn.request.call_args.kwargs['headers']['Accept'],'application/json')
        self.assertEqual(conn.request.call_count,1)

    def test_oversized_response_rejected(self):
        from unittest.mock import patch, MagicMock
        from radar import transport,LIMIT
        response=MagicMock();response.getheader.return_value=str(LIMIT+1)
        conn=MagicMock();conn.getresponse.return_value=response
        with patch('radar.http.client.HTTPSConnection',return_value=conn):
            with self.assertRaises(ValueError):transport('/service-status','test-only')
        conn.close.assert_called_once()
