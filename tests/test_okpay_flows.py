"""Entirely offline protocol-to-ledger flows; no downloaded SDK is imported."""
import json
import unittest
from urllib.parse import parse_qs

from tests import test_orders
from tests.okpay_fixtures import TOKEN, response, deposit
from zhbot.core import RuleError
from zhbot.okpay import OfflineOKPayAdapter
from zhbot.payments import Payments


class FixtureGateway:
    """Test-only bridge; production adapter and runner remain disabled."""
    enabled = True
    merchant = '10001'

    def __init__(self):
        self.adapter = OfflineOKPayAdapter(10001, TOKEN)
        self.requests = []
        self.results = {}
        self.create_timeout = False
        self.raw_response = None

    def create(self, payment):
        req = self.adapter.prepare_create(payment, timestamp=1000, nonce='fixture-create-0001')
        self.requests.append(req)
        if self.create_timeout:
            raise TimeoutError('fixture only')
        raw = response({'order_id': 'platform-' + payment['id'], 'status': 0,
                        'pay_url': 'https://t.me/okpay?start=shop_deposit----' + payment['id']})
        return self.adapter.parse_create(raw, payment)

    def check(self, payment):
        req = self.adapter.prepare_query(payment, timestamp=1001,
                                         nonce='fixture-check-' + str(len(self.requests)))
        self.requests.append(req)
        data = self.results.get(payment['id'], deposit(payment))
        return self.adapter.parse_query(self.raw_response or response(data), payment)


class OfflineFlowTests(unittest.TestCase):
    setUp = test_orders.OrderTests.setUp
    tearDown = test_orders.OrderTests.tearDown

    def setup_protocol(self):
        self.now = 1000
        self.s.clock = lambda: self.now
        self.gateway = FixtureGateway()
        self.p = Payments(self.s, self.gateway)

    def create(self, actor=101):
        payment = self.p.request(actor, '3')
        self.assertTrue(self.p.work_once())
        return payment

    def check(self, payment, actor=101):
        self.now += 1
        self.p.request_check(actor, payment)
        self.assertTrue(self.p.work_once())
        return self.p.get(actor, payment)

    def assert_no_credit(self, actor=101, balance=10):
        self.assertEqual(self.s.balance(actor), (balance, 0))
        self.assertEqual(self.db.all('SELECT * FROM payment_transactions'), [])
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])

    def test_simulated_create_and_paid_txid_query_require_review(self):
        self.setup_protocol()
        payment = self.create()
        row = self.p.get(101, payment)
        self.assertEqual(row['status'], 'pending')
        self.assertEqual(row['expires_at'], 1600)
        row = self.check(payment)
        self.assertEqual(row['status'], 'review')
        self.assert_no_credit()
        self.assertEqual([req.path for req in self.gateway.requests],
                         ['/shop/payLink', '/shop/checkTransferByTxid'])
        self.assertEqual(parse_qs(self.gateway.requests[1].body.decode())['txid'], [row['platform_id']])
        observation = json.loads(self.db.all('SELECT evidence FROM payment_observations')[-1]['evidence'])
        self.assertEqual((observation['payer'], observation['amount'], observation['coin']), (101, '3', 'USDT'))
        self.assertIsNone(observation['paid_at'])
        self.assertIsNone(observation['transaction_id'])
        claims = self.db.all('SELECT * FROM payment_identity_claims')
        self.assertEqual([(claim['kind'], claim['value']) for claim in claims], [('platform', row['platform_id'])])

    def test_lost_create_uses_unique_id_then_known_platform_without_recreation(self):
        self.setup_protocol()
        self.gateway.create_timeout = True
        payment = self.create()
        self.assertEqual(self.p.get(101, payment)['status'], 'unknown')
        self.gateway.results[payment] = {'order_id': 'known-platform', 'unique_id': payment,
                                         'status': 1, 'amount': '3'}  # checkDeposit subset
        self.assertEqual(self.check(payment)['status'], 'review')
        self.assertEqual(self.gateway.requests[1].path, '/shop/checkDeposit')
        self.assertEqual(parse_qs(self.gateway.requests[1].body.decode())['unique_id'], [payment])
        self.assertEqual(self.check(payment)['status'], 'review')
        self.assertEqual(self.gateway.requests[2].path, '/shop/checkTransferByTxid')
        self.assertEqual(len([req for req in self.gateway.requests if req.path == '/shop/payLink']), 1)
        self.assert_no_credit()

    def test_pending_query_preserves_pending_and_does_not_credit(self):
        self.setup_protocol()
        payment = self.create()
        self.gateway.results[payment] = {'order_id': self.p.get(101, payment)['platform_id'],
                                         'unique_id': payment, 'status': 0}
        self.assertEqual(self.check(payment)['status'], 'pending')
        self.assert_no_credit()

    def test_each_signed_business_mismatch_requires_review(self):
        for change in [{'pay_user_id': 102}, {'pay_user_id': None}, {'amount': '4'},
                       {'amount': '3.5'}, {'coin': 'TRX'}, {'coin': None}, {'status': 2}]:
            with self.subTest(change=change):
                self.setup_protocol()
                actor = 200 + len(self.db.all('SELECT * FROM users'))
                self.s.register(actor)
                payment = self.create(actor)
                self.gateway.results[payment] = deposit(self.p.get(actor, payment), **change)
                self.assertEqual(self.check(payment, actor)['status'], 'review')
                self.assert_no_credit(actor, 0)

    def test_callback_replay_is_idempotent_and_does_not_infer_time(self):
        self.setup_protocol()
        payment = self.create()
        self.p.request_check(101, payment)
        job = self.p.claim_job()
        raw = response(deposit(job['payment'], type='deposit', timestamp=1001, paid_at=1001))
        evidence = self.gateway.adapter.parse_callback(raw, job['payment'])
        self.now = 1001
        self.p.finish_job(job, evidence, observed_at=self.now)
        self.p.finish_job(job, evidence, observed_at=self.now)
        self.assertEqual(self.p.get(101, payment)['status'], 'review')
        self.assertEqual(len(self.db.all('SELECT * FROM payment_observations')), 2)
        self.assert_no_credit()

    def test_conflicting_platform_identity_is_durably_quarantined(self):
        self.setup_protocol()
        payment = self.create()
        original = self.p.get(101, payment)['platform_id']
        self.gateway.results[payment] = deposit(self.p.get(101, payment), order_id='conflicting-platform')
        self.assertEqual(self.check(payment)['status'], 'review')
        sightings = self.db.all('SELECT value,quarantined FROM payment_identity_sightings WHERE payment_id=?', (payment,))
        self.assertEqual({r['value'] for r in sightings}, {original, 'conflicting-platform'})
        self.assertTrue(all(r['quarantined'] for r in sightings))
        self.s.recover()
        self.assert_no_credit()

    def test_malformed_signed_business_fields_still_quarantine_platform_conflicts(self):
        for changes in [{'amount': 'NaN'}, {'status': True}, {'pay_user_id': True}, {'coin': False}]:
            with self.subTest(changes=changes):
                self.setup_protocol()
                actor = 400 + len(self.db.all('SELECT * FROM users'))
                self.s.register(actor)
                payment = self.create(actor)
                self.gateway.results[payment] = deposit(self.p.get(actor, payment),
                    order_id='conflict-' + payment, **changes)
                self.assertEqual(self.check(payment, actor)['status'], 'review')
                sighting = self.db.one('SELECT * FROM payment_identity_sightings WHERE value=?',
                                       ('conflict-' + payment,))
                self.assertIsNotNone(sighting)
                self.assertEqual(sighting['quarantined'], 1)
                self.assert_no_credit(actor, 0)

    def test_malformed_signed_create_link_retains_platform_identity_for_review(self):
        self.setup_protocol()
        payment = self.p.request(101, '3')
        job = self.p.claim_job()
        raw = response({'order_id': 'known-bad-link', 'status': 0, 'pay_url': 'https://evil.invalid/x'})
        evidence = self.gateway.adapter.parse_create(raw, job['payment'])
        self.p.finish_job(job, evidence, observed_at=self.now)
        row = self.p.get(101, payment)
        self.assertEqual(row['status'], 'review')
        self.assertEqual(row['platform_id'], 'known-bad-link')
        self.assertIsNone(row['pay_url'])
        self.assertIsNotNone(self.db.one("SELECT * FROM payment_identity_sightings WHERE value='known-bad-link'"))
        self.assert_no_credit()

    def test_wrong_merchant_or_local_order_or_unsigned_response_cannot_claim_id(self):
        for failure in ['merchant', 'unique_id', 'unsigned', 'tampered']:
            with self.subTest(failure=failure):
                self.setup_protocol()
                actor = 300 + len(self.db.all('SELECT * FROM users'))
                self.s.register(actor)
                payment = self.create(actor)
                data = deposit(self.p.get(actor, payment), order_id='untrusted')
                if failure == 'merchant':
                    raw = response(data, merchant=10002)
                elif failure == 'unique_id':
                    raw = response(dict(data, unique_id='other'))
                elif failure == 'unsigned':
                    raw = '{"status":"warning","msg":"no order"}'
                else:
                    tampered = json.loads(response(data))
                    tampered['data']['pay_user_id'] = 999
                    raw = json.dumps(tampered)
                self.gateway.raw_response = raw
                self.assertEqual(self.check(payment, actor)['status'], 'unknown')
                self.assertFalse(self.db.one("SELECT * FROM payment_identity_sightings WHERE value='untrusted'"))
                self.assert_no_credit(actor, 0)

    def test_late_paid_query_does_not_extend_local_expiry_or_credit(self):
        self.setup_protocol()
        payment = self.create()
        self.now = 2000
        row = self.check(payment)
        self.assertEqual(row['status'], 'review')
        self.assertEqual(row['expires_at'], 1600)
        self.assertIsNone(row['pay_url'])
        self.assert_no_credit()

    def test_production_adapter_cannot_create_payment_rows(self):
        self.setup_protocol()
        p = Payments(self.s, self.gateway.adapter)
        with self.assertRaises(RuleError):
            p.request(101, '3')
        self.assertFalse(p.work_once())
        self.assertEqual(self.db.all('SELECT * FROM payments'), [])
