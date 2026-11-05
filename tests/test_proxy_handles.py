"""Synthetic opaque handles only. No environment secrets or network are used."""
from http.client import HTTPSConnection
from io import BytesIO, StringIO
import contextlib
import json
from pathlib import Path
import string
import tempfile
import traceback
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from tests.test_runtime import Response
from zhbot.__main__ import main
from zhbot.core import RuleError
from zhbot.telegram import Telegram, TelegramError


DENIED = '/\\?#%'
PUNCTUATION = ''.join(c for c in string.punctuation if c not in DENIED)
LONG_REAL_TOKEN = '12345678901234567890:' + 'aB_-0' * 20  # Synthetic format fixture.


class OpaqueHandleTests(unittest.TestCase):
    def setUp(self):
        self.socket_guard = patch('socket.socket.connect', side_effect=AssertionError('no network allowed'))
        self.socket_guard.start()
        self.addCleanup(self.socket_guard.stop)

    def identity_with_handle(self, value):
        requests = []
        def offline(request, timeout):
            requests.append(request)
            return Response(b'{"ok":true,"result":{"id":12345,"is_bot":true}}')
        self.assertEqual(Telegram(value, opener=offline).identity(), 12345)
        self.assertEqual(len(requests), 1)
        request = requests[0]
        expected = '/bot' + value + '/getMe'
        self.assertEqual(request.selector, expected)
        self.assertEqual(request.full_url, 'https://api.telegram.org' + expected)
        self.assertEqual((request.type, request.host), ('https', 'api.telegram.org'))
        parts = urlsplit(request.full_url)
        self.assertEqual((parts.scheme, parts.netloc, parts.path, parts.query, parts.fragment),
                         ('https', 'api.telegram.org', expected, '', ''))
        self.assertIsNone(parts.username)
        self.assertIsNone(parts.password)
        self.assertEqual(parts.path.split('/'), ['', 'bot' + value, 'getMe'])
        return request

    def test_every_supported_ascii_character_and_combined_punctuation_are_exact(self):
        for code in range(33, 127):
            char = chr(code)
            if char not in DENIED:
                with self.subTest(code=code): self.identity_with_handle('h' + char + 'x')
        self.identity_with_handle('h' + PUNCTUATION + 'x')

    def test_dot_segments_and_reserved_delimiters_cannot_change_origin_or_route(self):
        for value in ['.', '..', '...', '.hidden', '..;x=1', ';..', '..@host:443',
                      '[::1]', 'user:pass@elsewhere.invalid', ';x=1&y=2', '<{value}>',
                      '"quoted"', '`wrapped`', '^|opaque', 'a=b+c,d!$*()']:
            with self.subTest(value=value): self.identity_with_handle(value)

    def test_block_separators_encodings_whitespace_controls_and_non_ascii(self):
        values = [None, True, 0, b'opaque', [], {}, '', 'h' * 65, '!' * 65,
                  '../getMe', '/getMe', '..\\getMe', 'value?x=1', 'value#fragment',
                  '%2FgetMe', '%2e%2e', '%252f', 'https://elsewhere.invalid/',
                  'a\u00a0b', 'a\u200bb', 'a\ufeffb', 'a\ud800b', '占位']
        values += ['a' + chr(i) + 'b' for i in list(range(33)) + [127]]
        values += ['a' + char + 'b' for char in DENIED]
        for value in values:
            with self.subTest(value=repr(value)[:40]), self.assertRaises(RuleError):
                Telegram(value, opener=lambda *a, **kw: self.fail('unsafe input reached transport'))

    def test_managed_handle_boundary_and_real_token_shape_remain_distinct(self):
        for value in ['!', 'x' * 64, '~' * 64, LONG_REAL_TOKEN, '12345:' + 'z' * 20]:
            with self.subTest(length=len(value)): self.identity_with_handle(value)
        for value in ['123456789012345678901:' + 'z' * 70, '12345:' + 'z' * 101,
                      '12345:' + '!' * 70, 'x' * 65 + ':bad', 'x:' + '!' * 511]:
            with self.subTest(length=len(value)), self.assertRaises(RuleError): Telegram(value)

    def test_wire_request_target_keeps_punctuation_and_has_one_fixed_host(self):
        class Wire:
            def __init__(self): self.data = b''
            def sendall(self, data): self.data += bytes(data)
            def close(self): pass
        for value in ['h' + PUNCTUATION + 'x', '.', '..', '[::1]', ';..', 'user:pass@host']:
            with self.subTest(value=value):
                request = self.identity_with_handle(value)
                connection = HTTPSConnection('api.telegram.org')
                wire = Wire(); connection.sock = wire
                try:
                    connection.request(request.get_method(), request.selector, body=request.data,
                                       headers=dict(request.header_items()))
                finally: connection.close()
                self.assertEqual(wire.data.split(b'\r\n', 1)[0],
                                 b'POST /bot' + value.encode('ascii') + b'/getMe HTTP/1.1')
                headers = wire.data.split(b'\r\n\r\n', 1)[0]
                self.assertEqual(headers.count(b'\r\nHost:'), 1)
                self.assertIn(b'\r\nHost: api.telegram.org\r\n', headers + b'\r\n')

    def test_url_construction_normalization_or_origin_change_fails_before_transport(self):
        for change in ['https://elsewhere.invalid/botx/', 'http://api.telegram.org/botx/',
                       'https://api.telegram.org/botx/../', 'https://api.telegram.org/botx/?q=']:
            with self.subTest(change=change):
                calls = []
                def offline(request, timeout):
                    calls.append(request.full_url)
                    return Response(b'{"ok":true,"result":{"id":12345,"is_bot":true}}')
                api = Telegram('x', opener=offline)
                api._base = change  # Guard future internal construction mistakes too.
                with self.assertRaises(TelegramError): api.identity()
                self.assertEqual(calls, [])

    def test_unrelated_http_errors_with_punctuation_keep_classification_and_redaction(self):
        handle = 'h' + PUNCTUATION + 'x'
        for http, code, definite, fatal, retry in [(400, 400, True, False, None),
                (401, 401, False, True, None), (403, 403, True, False, None),
                (429, 429, True, False, 5), (500, 500, False, False, None),
                (500, 400, False, False, None), (400, 429, False, False, None)]:
            def offline(request, timeout):
                body = {'ok': False, 'error_code': code, 'description': request.full_url,
                        'parameters': {'retry_after': 5}}
                raise HTTPError(request.full_url, http, request.full_url, {}, BytesIO(json.dumps(body).encode()))
            with self.subTest(http=http, code=code):
                try: Telegram(handle, opener=offline).call('getMe', {})
                except TelegramError as error:
                    self.assertEqual((error.definite, error.fatal, error.retry_after), (definite, fatal, retry))
                    output = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
                    self.assertNotIn(handle, output)
                    self.assertNotIn('https://api.telegram.org/bot', output)
                else: self.fail('HTTP error accepted')

    def test_main_preserves_punctuation_and_still_requires_successful_identity(self):
        handle = 'h' + PUNCTUATION + 'x'
        seen = []
        def offline(request, timeout):
            seen.append(request.selector)
            return Response(b'{"ok":false,"error_code":401,"description":"Unauthorized"}')
        def run(database, admins, api, **kw): api.identity()
        with tempfile.TemporaryDirectory() as tmp, patch('zhbot.__main__.Telegram', lambda token: Telegram(token, opener=offline)), \
                patch('zhbot.__main__.run_bot', run), patch('zhbot.__main__.signal.signal'), \
                contextlib.redirect_stderr(StringIO()) as output:
            result = main(['run', '--db', str(Path(tmp)/'db.sqlite3')], environ={
                'TELEGRAM_BOT_TOKEN': handle, 'TELEGRAM_ADMIN_IDS': '9001'})
            self.assertFalse((Path(tmp)/'db.sqlite3').exists())
        self.assertEqual(result, 1)
        self.assertEqual(seen, ['/bot' + handle + '/getMe'])
        self.assertNotIn(handle, output.getvalue())
        self.assertNotIn('https://api.telegram.org/bot', output.getvalue())

    def test_request_construction_failure_redacts_punctuation_handle_and_url(self):
        handle = 'h' + PUNCTUATION + 'x'
        calls = []
        def offline(request, timeout):
            calls.append(request)
            return Response(b'{"ok":true,"result":true}')
        api = Telegram(handle, opener=offline)
        with patch('zhbot.telegram.Request', side_effect=ValueError('https://api.telegram.org/bot' + handle + '/getMe')):
            try: api.identity()
            except TelegramError as error:
                output = ''.join(traceback.format_exception(type(error), error, error.__traceback__))
                self.assertNotIn(handle, output)
                self.assertNotIn('https://api.telegram.org/bot', output)
            else: self.fail('request construction failure accepted')
        self.assertEqual(calls, [])
