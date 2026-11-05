import copy
import json
import unittest
from urllib.parse import parse_qs

from tests.okpay_fixtures import TOKEN, PAYMENT, VECTOR_B, response, deposit
from zhbot.core import RuleError
from zhbot.okpay import OfflineOKPayAdapter, ProtocolError, build_base, load_payload, sign, verify


class SigningTests(unittest.TestCase):
    def test_official_request_vector_a(self):
        params = {'id': 10001, 'amount': '100.5', 'coin': 'USDT',
                  'unique_id': 'ORDER-20260628-001', 'timestamp': 1782680000,
                  'nonce': 'a1b2c3d4e5'}
        self.assertEqual(build_base(params), 'amount=100.5&coin=USDT&id=10001&nonce=a1b2c3d4e5&timestamp=1782680000&unique_id=ORDER-20260628-001')
        self.assertEqual(sign(params, TOKEN), '7444ADFD8E4F4DA09D752DDF9345E0EE56DC25090FCFAF675DD042830E5E3F79')

    def test_official_callback_vector_b(self):
        self.assertEqual(build_base(VECTOR_B), 'code=200&data.amount=100.5&data.coin=USDT&data.order_id=abc123def456&data.pay_user_id=123456789&data.status=1&data.type=deposit&data.unique_id=ORDER-20260628-001&id=10001&status=success')
        self.assertEqual(sign(VECTOR_B, TOKEN), VECTOR_B['sign'])
        self.assertTrue(verify(load_payload(json.dumps(VECTOR_B)), TOKEN))
        lower = dict(VECTOR_B, sign=VECTOR_B['sign'].lower())
        self.assertTrue(verify(lower, TOKEN))

    def test_official_boundary_vector_c(self):
        params = {'id': 7, 'a': '0', 'b': 0, 'c': '', 'd': None, 'e': False,
                  'f': 'hello', 'nest': {'x': '1', 'y': '2'}}
        self.assertEqual(build_base(params), 'a=0&b=0&e=false&f=hello&id=7&nest.x=1&nest.y=2')
        self.assertEqual(sign(params, TOKEN), '8BC0AF979075038025DDD51B6F4A2E6CF3FF9B5B5371EB2268D303F89883E92A')

    def test_numeric_lexemes_and_unicode_remain_exact(self):
        p = load_payload('{"a":100.50,"b":1e+20,"c":12345678901234567890,"text":"充值 +%/"}')
        self.assertEqual(build_base(p), 'a=100.50&b=1e+20&c=12345678901234567890&text=充值 +%/')
        self.assertEqual(build_base({'x': {'sign': 'nested'}, 'sign': 'removed'}), 'x.sign=nested')

    def test_ambiguous_or_unsupported_canonicalization_rejected(self):
        for p in [{'data': [1, 2]}, {'data': []}, {'a': {'b': [1]}},
                  {'a.b': '1', 'a': {'b': '2'}}, {'a': 'x&b=y'},
                  {'a&b': 'x'}, {'é': 'x'}, {'x': 1.2},
                  {'x': float('nan')}, {1: 'x'}, {'x': object()}, {'x': '\ud800'}]:
            with self.subTest(payload=repr(p)), self.assertRaises(ProtocolError):
                build_base(p)

    def test_json_duplicates_nonfinite_bad_encoding_and_limits_rejected(self):
        for raw in ['{"id":1,"id":2}', '{"data":{"x":1,"x":2}}',
                    '{"x":NaN}', '{"x":Infinity}', '[]', 'null', '{} trailing',
                    b'\xff', '{' * 2000, '{"x":"' + 'a' * 65536 + '"}',
                    '{"x":' * 20 + '1' + '}' * 20]:
            with self.subTest(raw=repr(raw)[:80]), self.assertRaises(ProtocolError):
                load_payload(raw)

    def test_signature_tampering_missing_bad_type_and_key_fail(self):
        for field, value in [('amount', '100.6'), ('coin', 'TRX'), ('pay_user_id', 1),
                             ('order_id', 'other'), ('unique_id', 'other'), ('status', 0)]:
            p = copy.deepcopy(VECTOR_B)
            p['data'][field] = value
            self.assertFalse(verify(p, TOKEN))
        for sig in [None, True, 1, '', '0' * 64, 'Z' * 64, 'F' * 65]:
            self.assertFalse(verify(dict(VECTOR_B, sign=sig), TOKEN))
        self.assertFalse(verify(VECTOR_B, 'wrong-test-key'))
        self.assertFalse(verify({'data': [1], 'sign': 'F' * 64}, TOKEN))


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.a = OfflineOKPayAdapter(10001, TOKEN)

    def test_default_disabled_operations_cannot_dispatch(self):
        self.assertFalse(self.a.enabled)
        for method in [self.a.create, self.a.check]:
            with self.assertRaises(RuleError):
                method(PAYMENT)

    def test_form_requests_use_documented_parameters_and_exact_signing(self):
        p = dict(PAYMENT, id='ORDER-20260628-001', amount=100)
        req = self.a.prepare_create(p, timestamp=1782680000, nonce='a1b2c3d4e5')
        self.assertEqual(req.path, '/shop/payLink')
        form = parse_qs(req.body.decode(), strict_parsing=True)
        self.assertEqual(form['amount'], ['100'])
        self.assertEqual(form['coin'], ['USDT'])
        self.assertEqual(form['unique_id'], [p['id']])
        self.assertNotIn('token', form)
        self.assertNotIn('pay_user_id', form)
        self.assertNotIn('expires_at', form)
        self.assertTrue(verify({k: v[0] for k, v in form.items()}, TOKEN))
        self.assertEqual(self.a.prepare_query(p, timestamp=1, nonce='abcdefgh').path,
                         '/shop/checkTransferByTxid')
        q = self.a.prepare_query(p, timestamp=1, nonce='abcdefgh')
        self.assertEqual(parse_qs(q.body.decode())['txid'], ['abc123def456'])
        p['platform_id'] = None
        q = self.a.prepare_query(p, timestamp=1, nonce='abcdefgh')
        self.assertEqual(q.path, '/shop/checkDeposit')
        self.assertEqual(parse_qs(q.body.decode())['unique_id'], [p['id']])

    def test_request_auth_shape_and_bounded_inputs(self):
        for timestamp, nonce in [(True, 'abcdefgh'), (-1, 'abcdefgh'), (1, 'short'),
                                 (1, 'a' * 129), (1, 'abc&defg')]:
            with self.subTest(timestamp=timestamp, nonce=nonce), self.assertRaises(ProtocolError):
                self.a.prepare_create(PAYMENT, timestamp=timestamp, nonce=nonce)
        for amount in [True, '3.5', 0, 1000001]:
            with self.subTest(amount=amount), self.assertRaises(ProtocolError):
                self.a.prepare_create(dict(PAYMENT, amount=amount), timestamp=1, nonce='abcdefgh')

    def test_create_context_supplies_local_id_without_fabricated_payment_facts(self):
        e = self.a.parse_create(response({'order_id': 'abc123def456', 'status': 0,
             'pay_url': 'https://t.me/okpay?start=shop_deposit----abc123def456'}), PAYMENT)
        self.assertTrue(e.verified)
        self.assertEqual((e.merchant, e.unique_id, e.platform_id, e.status),
                         ('10001', PAYMENT['id'], 'abc123def456', 0))
        self.assertIsNone(e.payer)
        self.assertIsNone(e.transaction_id)
        self.assertIsNone(e.paid_at)

    def test_query_and_callback_extract_only_documented_facts(self):
        for callback in [False, True]:
            data = deposit(type='deposit') if callback else deposit()
            parse = self.a.parse_callback if callback else self.a.parse_query
            e = parse(response(data), PAYMENT)
            self.assertEqual((e.payer, e.amount, e.coin, e.status), (101, '3', 'USDT', 1))
            self.assertEqual((e.platform_id, e.unique_id), ('abc123def456', PAYMENT['id']))
            self.assertIsNone(e.paid_at)
            self.assertIsNone(e.transaction_id)
        minimal = {'order_id': 'abc123def456', 'unique_id': PAYMENT['id'], 'status': 1, 'amount': '3'}
        e = self.a.parse_query(response(minimal), PAYMENT)
        self.assertIsNone(e.payer)
        self.assertIsNone(e.coin)

    def test_unknown_time_or_transaction_fields_cannot_become_evidence(self):
        e = self.a.parse_query(response(deposit(paid_at=1001, time=1001, timestamp=1001,
             transaction_id='invented', expires_at=1600)), PAYMENT)
        self.assertIsNone(e.paid_at)
        self.assertIsNone(e.transaction_id)

    def test_envelope_authentication_and_order_correlation_required(self):
        for raw in [response(deposit(), merchant=10002), response(deposit(), code=201),
                    response(deposit(), status='error'), '{"status":"warning","msg":"no order"}',
                    response(deposit(unique_id='other'))]:
            with self.subTest(raw=raw), self.assertRaises(ProtocolError):
                self.a.parse_query(raw, PAYMENT)
        p = json.loads(response(deposit()))
        p['data']['amount'] = '4'
        with self.assertRaises(ProtocolError):
            self.a.parse_query(json.dumps(p), PAYMENT)

    def test_signed_mismatches_preserved_for_state_machine_review(self):
        for change in [{'pay_user_id': 102}, {'amount': '4'}, {'coin': 'TRX'},
                       {'status': 2}, {'order_id': 'different-order'}]:
            with self.subTest(change=change):
                e = self.a.parse_query(response(deposit(**change)), PAYMENT)
                self.assertTrue(e.verified)
                attr = {'pay_user_id': 'payer', 'order_id': 'platform_id'}
                for field, value in change.items():
                    self.assertEqual(getattr(e, attr.get(field, field)), value)

    def test_authenticated_identity_survives_invalid_business_facts(self):
        for change in [{'pay_user_id': True}, {'pay_user_id': '١٠١'}, {'amount': True},
                       {'amount': 'NaN'}, {'status': True}, {'status': '1'}, {'coin': False}]:
            with self.subTest(change=change):
                e = self.a.parse_query(response(deposit(**change)), PAYMENT)
                self.assertTrue(e.verified)
                self.assertEqual(e.platform_id, PAYMENT['platform_id'])
                self.assertEqual((e.status, e.payer, e.amount, e.coin), (None, None, None, None))
        for change in [{'order_id': ''}, {'type': 'withdraw'}]:
            with self.subTest(change=change), self.assertRaises(ProtocolError):
                self.a.parse_query(response(deposit(**change)), PAYMENT)
        for data in [deposit(), deposit(type='withdraw')]:
            with self.subTest(data=data), self.assertRaises(ProtocolError):
                self.a.parse_callback(response(data), PAYMENT)
        e = self.a.parse_callback(response(deposit(type='deposit', status=0)), PAYMENT)
        self.assertIsNone(e.status)
        self.assertEqual(e.platform_id, PAYMENT['platform_id'])

    def test_create_status_and_payment_link_validation(self):
        for status, url in [(1, 'https://t.me/okpay?start=x'), (False, 'https://t.me/okpay?start=x'),
                            (0, 'http://t.me/okpay?start=x'), (0, 'https://evil.invalid/x'),
                            (0, 'https://user@t.me/okpay?start=x'), (0, 'https://t.me/other?start=x')]:
            with self.subTest(status=status, url=url):
                e = self.a.parse_create(response({'order_id': 'abc', 'status': status, 'pay_url': url}), PAYMENT)
                self.assertTrue(e.verified)
                self.assertEqual(e.platform_id, 'abc')
                self.assertIsNone(e.status)
                self.assertIsNone(e.pay_url)
