from dataclasses import replace
import unittest

from tests import test_orders
from zhbot.core import RuleError
from zhbot.payments import Payments, SimulatedGateway, DisabledGateway


class PaymentTests(unittest.TestCase):
    setUp = test_orders.OrderTests.setUp
    tearDown = test_orders.OrderTests.tearDown

    def setup_payments(self):
        self.now = 1000
        self.s.clock = lambda: self.now
        self.gateway = SimulatedGateway()
        self.p = Payments(self.s, self.gateway)

    def payment(self, amount='3'):
        self.setup_payments()
        payment = self.p.request(101, amount)
        self.p.work_once()
        self.assertEqual(self.p.get(101, payment)['status'], 'pending')
        return payment

    def settle(self, payment, **changes):
        row = self.p.get(101, payment)
        evidence = self.gateway.paid(row, paid_at=1001)
        self.gateway.results[payment] = replace(evidence, **changes)
        self.p.request_check(101, payment)
        self.p.work_once()

    def test_real_gateway_disabled_and_no_credentials_supported(self):
        p = Payments(self.s)
        self.assertIsInstance(p.gateway, DisabledGateway)
        with self.assertRaises(RuleError):
            p.request(101, '1')
        self.assertEqual(len(self.db.all('SELECT * FROM payments')), 0)

    def test_minimum_and_integer_amount(self):
        self.setup_payments()
        for amount in ['0', '-1', '1.5', '١', '²', '1'*5000, True, '1000001']:
            with self.subTest(amount=str(amount)[:20]), self.assertRaises(RuleError):
                self.p.request(101, amount)
        payment = self.p.request(101, '1')
        row = self.p.get(101, payment)
        self.assertEqual(row['amount'], 1)
        self.assertEqual(row['expires_at'] - row['created_at'], 600)

    def test_verified_payment_credits_once_and_owner_guard(self):
        payment = self.payment()
        self.now = 1002
        self.settle(payment)
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(self.p.get(101, payment)['status'], 'paid')
        self.p.request_check(101, payment)
        self.p.work_once()
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 1)
        with self.assertRaises(RuleError):
            self.p.get(102, payment)

    def test_missing_or_mismatched_fields_never_credit(self):
        cases = [{'payer': None}, {'payer': 102}, {'amount': '3.5'}, {'amount': '4'},
                 {'coin': 'TRX'}, {'verified': False}, {'merchant': 'other'},
                 {'unique_id': 'wrong'}, {'platform_id': 'wrong'}, {'transaction_id': ''},
                 {'paid_at': None}, {'paid_at': 999}, {'paid_at': 1600},
                 {'status': True}, {'amount': True}, {'payer': True}]
        for change in cases:
            with self.subTest(change=change):
                # A separate user's payment avoids intentionally blocked review orders.
                self.setup_payments()
                actor = 200 + cases.index(change)
                self.s.register(actor)
                payment = self.p.request(actor, '3')
                self.p.work_once()
                row = self.p.get(actor, payment)
                self.gateway.results[payment] = replace(self.gateway.paid(row, paid_at=1001), **change)
                self.now = 1002
                self.p.request_check(actor, payment)
                self.p.work_once()
                self.assertEqual(self.s.balance(actor), (0, 0))
                self.assertEqual(self.p.get(actor, payment)['status'], 'review')

    def test_pending_does_not_credit(self):
        payment = self.payment()
        self.p.request_check(101, payment)
        self.p.work_once()
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(self.p.get(101, payment)['status'], 'pending')

    def test_first_observation_after_deadline_requires_review(self):
        payment = self.payment()
        self.now = 1600
        self.settle(payment)
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(self.p.get(101, payment)['status'], 'review')

    def test_unknown_creation_never_recreates_and_restart_keeps_unknown(self):
        self.setup_payments()
        self.gateway.fail_create = True
        payment = self.p.request(101, '3')
        self.p.work_once()
        self.assertEqual(self.p.get(101, payment)['status'], 'unknown')
        self.p.work_once()
        self.s.recover()
        self.assertEqual(len(self.db.all('SELECT * FROM payment_jobs')), 1)
        with self.assertRaises(RuleError):
            self.p.request(101, '3')

    def test_cross_order_transaction_reuse_cannot_credit(self):
        payment = self.payment()
        self.now = 1002
        self.settle(payment, transaction_id='shared-simulation-tx')
        self.assertEqual(self.s.balance(101), (13, 0))
        second = self.p.request(102, '3')
        self.p.work_once()
        row = self.p.get(102, second)
        self.gateway.results[second] = replace(self.gateway.paid(row, paid_at=1003), transaction_id='shared-simulation-tx')
        self.now = 1004
        self.p.request_check(102, second)
        self.p.work_once()
        self.assertEqual(self.s.balance(102), (0, 0))
        self.assertEqual(self.p.get(102, second)['status'], 'review')

    def test_ledger_failure_rolls_back_payment_and_transaction_claim(self):
        payment = self.payment()
        self.now = 1002
        self.gateway.results[payment] = self.gateway.paid(self.p.get(101, payment), paid_at=1001)
        self.p.request_check(101, payment)
        self.db.execute("CREATE TRIGGER ledger_failure BEFORE INSERT ON ledger BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(Exception):
            self.p.work_once()
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 0)
        self.s.recover()
        self.assertNotEqual(self.p.get(101, payment)['status'], 'paid')

    def test_inflight_check_recovery_requires_human_review(self):
        payment = self.payment()
        self.p.request_check(101, payment)
        self.p.claim_job()
        self.s.recover()
        self.assertEqual(self.p.get(101, payment)['status'], 'unknown')
        self.assertFalse(self.db.one("SELECT * FROM payment_jobs WHERE status='pending'"))


if __name__ == '__main__':
    unittest.main()
