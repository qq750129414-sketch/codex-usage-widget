import tempfile
import unittest
from unittest.mock import patch
from reset_cards import ResetCards, account_key, available_count, codex_binary


ACCOUNT = {'account': {'type': 'chatgpt', 'email': 'example@example.invalid'}}


class BinaryDiscoveryTests(unittest.TestCase):
    def test_path_fallback_is_supported(self):
        path = '/example/custom-bin/codex'
        with patch('reset_cards.shutil.which', return_value=path), patch('reset_cards.os.path.isfile', side_effect=lambda p: p == path), patch('reset_cards.os.access', return_value=True):
            self.assertEqual(codex_binary(), path)

    def test_non_executable_is_rejected(self):
        with patch('reset_cards.shutil.which', return_value=None), patch('reset_cards.os.path.isfile', return_value=True), patch('reset_cards.os.access', return_value=False):
            with self.assertRaises(RuntimeError):
                codex_binary()

    def test_known_location_is_preferred(self):
        path = '/opt/homebrew/bin/codex'
        with patch('reset_cards.shutil.which', return_value='/example/other/codex'), patch('reset_cards.os.path.isfile', side_effect=lambda p: p == path), patch('reset_cards.os.access', return_value=True):
            self.assertEqual(codex_binary(), path)


class FakeRPC:
    def __init__(self, count=1, outcomes=None, account=ACCOUNT):
        self.count = count
        self.outcomes = iter(outcomes or ['reset'])
        self.account = account
        self.calls = []

    def call(self, method, params=None):
        self.calls.append((method, params))
        if method == 'account/read':
            return self.account
        if method == 'account/rateLimits/read':
            return {'rateLimitResetCredits': {'availableCount': self.count}}
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return {'outcome': outcome}


class ResetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cards = ResetCards(self.temp.name)
        self.account = account_key(ACCOUNT)

    def tearDown(self):
        self.cards.db.close()
        self.temp.cleanup()

    def test_count_unknown_not_zero(self):
        for value in (None, {}, {'availableCount': True}, {'availableCount': -1}, {'availableCount': '0'}):
            self.assertIsNone(available_count({'rateLimitResetCredits': value}))
        self.assertEqual(available_count({'rateLimitResetCredits': {'availableCount': 0}}), 0)
        self.assertEqual(available_count({'rateLimitResetCredits': {'availableCount': 3, 'credits': []}}), 3)

    def test_account_privacy(self):
        self.assertNotIn('@', self.account)
        self.assertIsNone(account_key({'account': {'type': 'apiKey'}}))
        self.assertIsNone(account_key({}))

    def test_confirmation_required(self):
        rpc = FakeRPC()
        with self.assertRaises(RuntimeError):
            self.cards.consume(rpc, self.account, False)
        self.assertEqual(rpc.calls, [])

    def test_account_change_rejected(self):
        rpc = FakeRPC(account={'account': {'type': 'chatgpt', 'email': 'different@example.invalid'}})
        with self.assertRaises(RuntimeError):
            self.cards.consume(rpc, self.account, True)
        self.assertEqual(len(rpc.calls), 1)

    def test_zero_no_mutation(self):
        rpc = FakeRPC(count=0)
        self.assertEqual(self.cards.consume(rpc, self.account, True), 'noCredit')
        self.assertFalse(any(m.endswith('/consume') for m, _ in rpc.calls))
        self.assertEqual(self.cards.snapshot(self.account, 0)['history'], [])

    def test_unknown_no_mutation(self):
        rpc = FakeRPC(count=None)
        with self.assertRaises(RuntimeError):
            self.cards.consume(rpc, self.account, True)
        self.assertEqual(len(rpc.calls), 2)

    def test_confirmed_history_and_account_isolation(self):
        rpc = FakeRPC()
        self.assertEqual(self.cards.consume(rpc, self.account, True), 'reset')
        state = self.cards.snapshot(self.account, 0)
        self.assertEqual(len(state['history']), 1)
        self.assertFalse(state['pending'])
        self.assertGreaterEqual(state['history'][0]['confirmed'], state['history'][0]['started'])
        self.assertEqual(self.cards.snapshot('other-account', 2)['history'], [])

    def test_unknown_reuses_persisted_key_even_with_zero_count(self):
        rpc = FakeRPC(outcomes=[RuntimeError('timeout')])
        with self.assertRaises(RuntimeError):
            self.cards.consume(rpc, self.account, True)
        key = rpc.calls[-1][1]['idempotencyKey']
        self.assertTrue(self.cards.snapshot(self.account, 0)['pending'])
        self.cards.db.close()
        self.cards = ResetCards(self.temp.name)
        retry = FakeRPC(count=0, outcomes=['alreadyRedeemed'])
        self.cards.consume(retry, self.account, True)
        self.assertEqual(retry.calls[-1][1]['idempotencyKey'], key)
        self.assertEqual(len(self.cards.snapshot(self.account, 0)['history']), 1)

    def test_not_success_is_not_history(self):
        for outcome in ('nothingToReset', 'noCredit'):
            self.cards.consume(FakeRPC(outcomes=[outcome]), self.account, True)
            self.assertFalse(self.cards.snapshot(self.account, 0)['pending'])
        self.assertEqual(self.cards.snapshot(self.account, 0)['history'], [])

    def test_unknown_outcome_remains_pending(self):
        with self.assertRaises(RuntimeError):
            self.cards.consume(FakeRPC(outcomes=['newStatus']), self.account, True)
        self.assertTrue(self.cards.snapshot(self.account, 0)['pending'])
        self.assertEqual(self.cards.snapshot(self.account, 0)['history'], [])

    def test_readonly_snapshots_never_consume(self):
        for _ in range(5):
            self.cards.snapshot(self.account, 2)
        self.assertEqual(self.cards.db.execute('SELECT count(*) FROM attempts').fetchone()[0], 0)

    def test_web_snapshot_does_not_expose_reset_control(self):
        from panel import public_snapshot
        snapshot = public_snapshot({'resetCards': {'account': self.account, 'pending': True, 'history': [{'id': 'private-request'}]}})
        self.assertNotIn('resetCards', snapshot)
        self.assertNotIn(self.account, str(snapshot))


if __name__ == '__main__':
    unittest.main()
