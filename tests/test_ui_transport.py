import json
import unittest

from tests import test_app
from tests.test_runtime import TOKEN, Response
from zhbot.app import Application
from zhbot.runtime import OutboxWorker, bind_bot
from zhbot.telegram import Telegram, TelegramError


class MenuTransportTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown

    def test_outbox_delivers_persistent_markup_and_requests_callback_updates(self):
        bind_bot(self.s, 12345)
        app = Application(self.s)
        app.process_update(test_app.update(0, '/start'))
        calls = []
        def open_request(request, timeout):
            calls.append((request.full_url.rsplit('/', 1)[-1], json.loads(request.data)))
            return Response(json.dumps({'ok': True, 'result': [] if calls[-1][0] == 'getUpdates' else {'message_id': 99}}).encode())
        api = Telegram(TOKEN, opener=open_request)
        OutboxWorker(self.s, api).work_once()
        markup = calls[0][1].get('reply_markup')
        self.assertIsNotNone(markup)
        self.assertTrue(any(b['text'] == '开始报单' for row in markup['inline_keyboard'] for b in row))
        api.get_updates(1)
        self.assertEqual(calls[-1][1]['allowed_updates'], ['message', 'callback_query'])

    def test_callback_answer_is_sent_once_separately_from_business_messages(self):
        self.db.execute("INSERT INTO processed_callbacks(id,actor,at,answer) VALUES ('query-1',101,1000,'已处理')")
        calls = []
        def open_request(request, timeout):
            calls.append((request.full_url.rsplit('/', 1)[-1], json.loads(request.data)))
            return Response(b'{"ok":true,"result":true}')
        worker = OutboxWorker(self.s, Telegram(TOKEN, opener=open_request))
        self.assertTrue(worker.work_once())
        self.assertFalse(worker.work_once())
        self.assertEqual(calls, [('answerCallbackQuery', {'callback_query_id': 'query-1', 'text': '已处理'})])
        self.assertEqual(self.db.one('SELECT answer_status FROM processed_callbacks')['answer_status'], 'done')

    def test_unknown_callback_answer_does_not_retry_or_repeat_business(self):
        self.db.execute("INSERT INTO processed_callbacks(id,actor,at,answer) VALUES ('query-1',101,1000,'已处理')")
        class Uncertain:
            def answer_callback(self, *args): raise TelegramError('unknown')
        worker = OutboxWorker(self.s, Uncertain())
        self.assertTrue(worker.work_once())
        self.s.recover()
        self.assertFalse(worker.work_once())
        self.assertEqual(self.db.one('SELECT answer_status FROM processed_callbacks')['answer_status'], 'unknown')
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])

    def test_callback_ack_backlog_cannot_starve_delivery_messages(self):
        for i in range(50):
            self.db.execute("INSERT INTO processed_callbacks(id,actor,at,answer) VALUES (?,101,1000,'已处理')", ('query-'+str(i),))
        self.s.note(101, '必须公平发送的业务消息')
        class Accepted:
            def answer_callback(self, *args): pass
            def send(self, message): return 99
        worker = OutboxWorker(self.s, Accepted())
        for i in range(3): worker.work_once()
        self.assertEqual(self.db.one('SELECT status FROM messages')['status'], 'sent')
        self.assertGreater(self.db.one("SELECT COUNT(*) n FROM processed_callbacks WHERE answer_status='pending'")['n'], 0)
