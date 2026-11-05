"""Offline proxy-placeholder and expired-ack regression fixtures; no live HTTP."""
import contextlib
from io import BytesIO, StringIO
import json
from pathlib import Path
import tempfile
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from tests.test_runtime import Response, TOKEN
from tests.test_ui import UIHelpers
from zhbot.__main__ import main
from zhbot.core import RuleError
from zhbot.runtime import OutboxWorker
from zhbot.telegram import Telegram, TelegramError


PLACEHOLDER = 'OFFLINE_NETWORK_SECRET_HANDLE_123'  # Synthetic; not a credential or official prefix.
UNAVAILABLE = 'Bad Request: query is too old and response timeout expired or query ID is invalid'


def rejection(http=400, code=400, description=UNAVAILABLE, ok=False):
    def open_request(request, timeout):
        raise HTTPError(request.full_url, http, 'private transport reason', {},
                        BytesIO(json.dumps({'ok': ok, 'error_code': code, 'description': description}).encode()))
    return open_request


class PlaceholderTests(unittest.TestCase):
    def test_safe_opaque_handle_reaches_fixed_official_getme_without_reformatting(self):
        calls = []
        def offline(request, timeout):
            calls.append(request.full_url)
            return Response(b'{"ok":true,"result":{"id":12345,"is_bot":true}}')
        self.assertEqual(Telegram(PLACEHOLDER, opener=offline).identity(), 12345)
        self.assertEqual(calls, ['https://api.telegram.org/bot' + PLACEHOLDER + '/getMe'])
        self.assertEqual(Telegram(TOKEN, opener=offline).identity(), 12345)

    def test_empty_unsafe_or_unbounded_handle_is_rejected_before_transport(self):
        for value in [None, True, 123, [], '', ' ', ' value', 'value ', 'a\nb', 'a\rb',
                      'a/b', 'a\\b', 'a?b', 'a#b', 'a%2fb', 'a\x00b', '密钥',
                      'a' * 513, 'https://private.invalid/token']:
            with self.subTest(value=repr(value)[:40]), self.assertRaises(RuleError):
                Telegram(value, opener=lambda *a, **kw: self.fail('unsafe value reached transport'))

    def test_main_startup_accepts_safe_handle_with_offline_identity_gate(self):
        checked = []
        def run(database, admins, api, **kwargs):
            checked.append((api.identity(), admins))
        opener = lambda *a, **kw: Response(b'{"ok":true,"result":{"id":12345,"is_bot":true}}')
        factory = lambda token: Telegram(token, opener=opener)
        with tempfile.TemporaryDirectory() as tmp, patch('zhbot.__main__.Telegram', factory), \
                patch('zhbot.__main__.run_bot', run), patch('zhbot.__main__.signal.signal'):
            result = main(['run', '--db', str(Path(tmp)/'bot.sqlite3')], environ={
                'TELEGRAM_BOT_TOKEN': PLACEHOLDER, 'TELEGRAM_ADMIN_IDS': '9001'})
        self.assertEqual(result, 0)
        self.assertEqual(checked, [(12345, {9001})])

    def test_transport_and_startup_errors_never_expose_handle_or_token_url(self):
        for token in [PLACEHOLDER, TOKEN]:
            for failure in ['transport', 'response']:
                def broken(request, timeout):
                    if failure == 'transport':
                        raise URLError(request.full_url)
                    raise HTTPError(request.full_url, 400, request.full_url, {}, BytesIO(json.dumps({
                        'ok': False, 'error_code': 400, 'description': request.full_url}).encode()))
                try:
                    Telegram(token, opener=broken).identity()
                except TelegramError as error:
                    output = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
                    self.assertNotIn(token, output)
                    self.assertNotIn('https://api.telegram.org/bot', output)
                else: self.fail('transport failure was accepted')
        with patch('zhbot.__main__.Telegram', lambda token: Telegram(token, opener=broken)), \
                patch('zhbot.__main__.run_bot', lambda db, admins, api, **kw: api.identity()), \
                patch('zhbot.__main__.signal.signal'), contextlib.redirect_stderr(StringIO()) as output:
            result = main(['run'], environ={'TELEGRAM_BOT_TOKEN': PLACEHOLDER, 'TELEGRAM_ADMIN_IDS': '9001'})
        self.assertEqual(result, 1)
        self.assertNotIn(PLACEHOLDER, output.getvalue())


class CallbackClassificationTests(unittest.TestCase):
    def test_only_known_consistent_unavailable_ack_returns_false(self):
        api = Telegram(TOKEN, opener=rejection())
        self.assertIs(api.answer_callback('query-1', '已处理'), False)

    def test_other_400s_conflicting_statuses_and_transport_errors_are_not_expiry(self):
        for opener in [rejection(description='Bad Request: chat not found'),
                       rejection(description='Bad Request: query ID is invalid'),
                       rejection(description=UNAVAILABLE + ' private-extra'),
                       rejection(http=500), rejection(code=429), rejection(ok=True),
                       lambda *a, **kw: Response(json.dumps({'ok': False, 'error_code': 400, 'description': UNAVAILABLE}).encode())]:
            with self.subTest(opener=opener), self.assertRaises(TelegramError):
                Telegram(TOKEN, opener=opener).answer_callback('query-1', '已处理')
        with self.assertRaises(TelegramError):
            Telegram(TOKEN, opener=rejection()).send({'kind': 'text', 'content': '业务消息', 'chat_id': 101})

    def test_success_ack_is_positive_and_malformed_success_remains_unknown(self):
        api = Telegram(TOKEN, opener=lambda *a, **kw: Response(b'{"ok":true,"result":true}'))
        self.assertIs(api.answer_callback('query-1', '已处理'), True)
        with self.assertRaises(TelegramError):
            Telegram(TOKEN, opener=lambda *a, **kw: Response(b'{"ok":true,"result":false}')).answer_callback('query-1', '已处理')


class CallbackActionSafetyTests(UIHelpers, unittest.TestCase):
    def test_obsolete_ack_is_terminal_without_reapplying_confirmed_points(self):
        self.setup_ui()
        self.click(self.text('/admin', 9001), '积分管理', 9001)
        card = self.text('101', 9001)
        self.click(card, '增加积分', 9001)
        self.text('2', 9001)
        confirm = self.text('人工核实补分', 9001)
        event = self.event(self.button(confirm, '确认调整积分'), 9001)
        self.app.process_update(event)
        query = event['callback_query']['id']
        self.db.execute("UPDATE processed_callbacks SET answer_status='done' WHERE id!=?", (query,))
        ledger = self.db.all('SELECT * FROM ledger')
        calls = []
        def offline(request, timeout):
            calls.append(request.full_url.rsplit('/', 1)[-1])
            return rejection()(request, timeout) if calls[-1] == 'answerCallbackQuery' else Response(b'{"ok":true,"result":{"message_id":99}}')
        worker = OutboxWorker(self.s, Telegram(TOKEN, opener=offline))
        self.assertTrue(worker.work_once())
        self.assertEqual(self.db.one('SELECT answer_status FROM processed_callbacks WHERE id=?', (query,))['answer_status'], 'unavailable')
        self.s.recover()
        event['update_id'] += 1
        self.app.process_update(event)
        while worker.work_once(): pass
        self.assertEqual(calls.count('answerCallbackQuery'), 1)
        self.assertEqual(self.db.all('SELECT * FROM ledger'), ledger)
        self.assertEqual(self.s.balance(101), (12, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM point_adjustments')), 1)
        self.assertFalse(self.app.payments.gateway.enabled)

    def test_fatal_ack_authentication_error_is_recorded_unknown_then_stops(self):
        self.setup_ui()
        self.db.execute("INSERT INTO processed_callbacks(id,actor,at,answer) VALUES ('fatal-query',101,1000,'已处理')")
        worker = OutboxWorker(self.s, Telegram(TOKEN, opener=rejection(http=401, code=401, description='Unauthorized')))
        with self.assertRaises(TelegramError) as caught: worker.work_once()
        self.assertTrue(caught.exception.fatal)
        self.assertEqual(self.db.one('SELECT answer_status FROM processed_callbacks')['answer_status'], 'unknown')
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])

    def test_non_boolean_transport_ack_result_is_not_recorded_as_success(self):
        self.setup_ui()
        for index, result in enumerate([None, 0, 'true', {}, []]):
            with self.subTest(result=result):
                query = 'shape-' + str(index)
                self.db.execute("INSERT INTO processed_callbacks(id,actor,at,answer) VALUES (?,101,1000,'已处理')", (query,))
                class Malformed:
                    def answer_callback(self, *args): return result
                self.assertTrue(OutboxWorker(self.s, Malformed()).work_once())
                self.assertEqual(self.db.one('SELECT answer_status FROM processed_callbacks WHERE id=?', (query,))['answer_status'], 'unknown')
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])
