import http.client
import json
import unittest
from panel import PanelServer


class PanelTests(unittest.TestCase):
    def test_public_plan_label_is_preserved(self):
        self.server.publish({'buckets':[{'id':'codex','planType':'prolite','planLabel':'PRO 10X','secret':'private'}]})
        snapshot=json.loads(self.request('/snapshot',{'X-Usage-Widget':'read'})[1])
        self.assertEqual(snapshot['buckets'][0]['planLabel'],'PRO 10X')
        self.assertEqual(snapshot['buckets'][0]['planType'],'prolite')
        self.assertNotIn('secret',snapshot['buckets'][0])
    @classmethod
    def setUpClass(cls):
        cls.server = PanelServer(port=0)
        cls.server.start()
        cls.port = cls.server.http.server_port

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def request(self, path, headers=None, method='GET'):
        client = http.client.HTTPConnection('127.0.0.1', self.port, timeout=3)
        client.request(method, path, headers=headers or {})
        response = client.getresponse()
        data = response.read()
        result = response.status, data, dict(response.getheaders())
        client.close()
        return result

    def test_reject_foreign_origins_hosts_and_unmarked_reads(self):
        for headers in ({}, {'X-Usage-Widget': 'read', 'Origin': 'https://example.org'},
                        {'X-Usage-Widget': 'read', 'Host': 'evil.example'},
                        {'X-Usage-Widget': 'read', 'Sec-Fetch-Site': 'cross-site'}):
            self.assertEqual(403, self.request('/snapshot', headers)[0])

    def test_live_snapshot_redacts_secrets_and_excludes_internal_fields(self):
        data = {'checked': 123, 'api_key': 'private', 'tasks': [{'secret': 'private'}],
                'runs': [{'id': 'a', 'thread': 't', 'title': r'key rr\_live\_fake_TEST123',
                          'status': '进行中', 'usage': {'total_tokens': 12}, 'raw_log': 'private'}]}
        self.server.publish(data)
        status, body, headers = self.request('/snapshot', {'X-Usage-Widget': 'read'})
        self.assertEqual(status, 200)
        self.assertNotIn(b'private', body)
        self.assertNotIn(b'fake_TEST123', body)
        self.assertEqual(json.loads(body)['runs'][0]['tokens'], 12)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        data['runs'][0]['status'] = '已完成'
        data['runs'][0]['usage']['total_tokens'] = 24
        self.server.publish(data)
        snapshot = json.loads(self.request('/snapshot', {'X-Usage-Widget': 'read'})[1])
        self.assertEqual(snapshot['runs'][0]['status'], '已完成')
        self.assertEqual(snapshot['runs'][0]['tokens'], 24)

    def test_assets_and_unknown_paths(self):
        for path in ('/', '/panel.js', '/panel.css'):
            self.assertEqual(self.request(path)[0], 200)
        self.assertEqual(self.request('/../../src/backend.py')[0], 404)
        self.assertEqual(self.request('/snapshot', method='POST')[0], 501)


if __name__ == '__main__':
    unittest.main()
