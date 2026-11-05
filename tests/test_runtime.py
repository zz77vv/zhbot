from io import BytesIO
import json
import tempfile
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError

from tests import test_orders
from tests.test_app import update
from zhbot.app import Application
from zhbot.telegram import Telegram, TelegramError
from zhbot.runtime import OutboxWorker, Poller, SingleInstance, bind_bot


TOKEN = '12345:FAKE_TOKEN_FOR_OFFLINE_TESTING_ONLY'


class Response(BytesIO):
    pass


class RuntimeTests(unittest.TestCase):
    setUp = test_orders.OrderTests.setUp
    tearDown = test_orders.OrderTests.tearDown

    def test_telegram_payload_and_file_id_reuse(self):
        calls = []
        def open_request(request, timeout):
            calls.append((request, timeout))
            return Response(json.dumps({'ok': True, 'result': {'message_id': 77}}).encode())
        api = Telegram(TOKEN, opener=open_request)
        for kind in ['text', 'photo', 'document']:
            result = api.send({'kind': kind, 'content': '模拟' if kind == 'text' else 'fake-file-id',
                               'caption': '模拟附件', 'chat_id': 101})
            self.assertEqual(result, 77)
        methods = [r.full_url.rsplit('/', 1)[-1] for r, _ in calls]
        self.assertEqual(methods, ['sendMessage', 'sendPhoto', 'sendDocument'])
        body = json.loads(calls[1][0].data)
        self.assertEqual(body['photo'], 'fake-file-id')
        self.assertEqual(body['chat_id'], 101)

    def test_network_and_5xx_are_ambiguous_without_secret_in_error(self):
        for error in [URLError(TOKEN), HTTPError('https://example.invalid/' + TOKEN, 500, TOKEN, {}, BytesIO(b'bad'))]:
            def open_request(request, timeout):
                raise error
            with self.assertRaises(TelegramError) as caught:
                Telegram(TOKEN, opener=open_request).send({'kind': 'text', 'content': '模拟', 'chat_id': 101})
            self.assertFalse(caught.exception.definite)
            self.assertNotIn(TOKEN, str(caught.exception))

    def test_explicit_rejection_and_rate_limit_are_distinguished(self):
        for code in [400, 403, 429]:
            def open_request(request, timeout):
                raise HTTPError(request.full_url, code, 'ignored', {}, BytesIO(json.dumps({
                    'ok': False, 'error_code': code, 'parameters': {'retry_after': 5}}).encode()))
            with self.assertRaises(TelegramError) as caught:
                Telegram(TOKEN, opener=open_request).send({'kind': 'text', 'content': '模拟', 'chat_id': 101})
            self.assertTrue(caught.exception.definite)
            self.assertEqual(caught.exception.retry_after, 5 if code == 429 else None)

    def test_bad_success_response_is_not_delivery_confirmation(self):
        for result in [{'ok': True, 'result': {}}, {'ok': 'true', 'result': {'message_id': 1}},
                       {'ok': True, 'result': {'message_id': True}}]:
            with self.subTest(result=result), self.assertRaises(TelegramError):
                Telegram(TOKEN, opener=lambda *a, **k: Response(json.dumps(result).encode())).send(
                    {'kind': 'text', 'content': '模拟', 'chat_id': 101})

    def test_worker_marks_timeout_unknown_without_retry(self):
        self.s.note(101, '模拟通知')
        class TimeoutTransport:
            def send(self, message):
                raise TelegramError('发送结果未知', definite=False)
        worker = OutboxWorker(self.s, TimeoutTransport())
        self.assertTrue(worker.work_once())
        self.assertFalse(worker.work_once())
        self.assertEqual(self.db.one('SELECT status FROM messages')['status'], 'unknown')

    def test_rate_limit_defers_same_message_then_sends(self):
        self.s.note(101, '模拟通知')
        now = [1000]
        self.s.clock = lambda: now[0]
        class LimitedTransport:
            count = 0
            def send(self, message):
                self.count += 1
                if self.count == 1:
                    raise TelegramError('限流', definite=True, retry_after=5)
                return 77
        worker = OutboxWorker(self.s, LimitedTransport())
        worker.work_once()
        self.assertFalse(worker.work_once())
        now[0] = 1005
        self.assertTrue(worker.work_once())
        self.assertEqual(self.db.one('SELECT status FROM messages')['status'], 'sent')

    def test_send_success_before_record_crash_recovers_to_unknown(self):
        self.s.note(101, '模拟通知')
        class AcceptedTransport:
            def send(self, message):
                return 77
        original = self.s.sent
        def crash(*args):
            raise RuntimeError('simulated crash after network')
        self.s.sent = crash
        with self.assertRaises(RuntimeError):
            OutboxWorker(self.s, AcceptedTransport()).work_once()
        self.s.sent = original
        self.s.recover()
        self.assertEqual(self.db.one('SELECT status FROM messages')['status'], 'unknown')

    def test_long_poll_uses_persisted_cursor_and_duplicate_delivery_is_harmless(self):
        app = Application(self.s)
        class Source:
            def get_updates(self, offset):
                self.offset = offset
                return [update(10, '/balance'), update(10, '/balance'), update(11, '/new ²'), update(12, '/balance')]
        source = Source()
        poller = Poller(app, source)
        poller.poll_once()
        self.assertEqual(app.offset(), 13)
        self.assertEqual(len(self.db.all('SELECT * FROM processed_updates')), 3)
        poller.poll_once()
        self.assertEqual(source.offset, 13)
        self.assertEqual(len(self.db.all('SELECT * FROM messages')), 3)

    def test_same_database_cannot_have_two_active_runners(self):
        with SingleInstance(self.path):
            with self.assertRaises(RuntimeError):
                with SingleInstance(self.path):
                    pass
        with SingleInstance(self.path):
            pass

    def test_database_cannot_be_reused_with_different_bot_identity(self):
        bind_bot(self.s, 12345)
        bind_bot(self.s, 12345)
        with self.assertRaises(RuntimeError):
            bind_bot(self.s, 23456)

    def test_http_error_cannot_be_overridden_by_success_shaped_body(self):
        def open_request(request, timeout):
            raise HTTPError(request.full_url, 500, 'ignored', {}, BytesIO(json.dumps({
                'ok': True, 'result': {'message_id': 77}}).encode()))
        with self.assertRaises(TelegramError) as caught:
            Telegram(TOKEN, opener=open_request).send({'kind': 'text', 'content': '模拟', 'chat_id': 101})
        self.assertFalse(caught.exception.definite)


if __name__ == '__main__':
    unittest.main()
