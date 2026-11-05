from dataclasses import replace
import threading
import unittest

from tests import test_payments
from zhbot.core import RuleError
from zhbot.payments import Payments, SimulatedGateway
from zhbot.service import Service
from zhbot.store import Store


class PaymentSafetyTests(unittest.TestCase):
    setUp = test_payments.PaymentTests.setUp
    tearDown = test_payments.PaymentTests.tearDown
    setup_payments = test_payments.PaymentTests.setup_payments
    payment = test_payments.PaymentTests.payment
    settle = test_payments.PaymentTests.settle

    def counted_gateway(self):
        self.setup_payments()
        gateway = self.gateway
        gateway.calls = 0
        original = gateway.create
        def create(row):
            gateway.calls += 1
            return original(row)
        gateway.create = create
        return gateway

    def test_expired_queued_create_never_calls_provider(self):
        gateway = self.counted_gateway()
        payment = self.p.request(101, '3')
        self.now = 1600
        self.p.work_once()
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(self.p.get(101, payment)['status'], 'expired')
        self.assertIsNone(self.p.get(101, payment)['pay_url'])
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_deadline_between_claim_and_dispatch_prevents_create(self):
        gateway = self.counted_gateway()
        payment = self.p.request(101, '3')
        claim = self.p.claim_job
        def delayed_claim():
            job = claim()
            self.now = 1600
            return job
        self.p.claim_job = delayed_claim
        self.p.work_once()
        self.assertEqual(gateway.calls, 0)
        self.assertEqual(self.p.get(101, payment)['status'], 'expired')

    def test_delayed_create_response_is_reviewed_without_publishing_link(self):
        gateway = self.counted_gateway()
        payment = self.p.request(101, '3')
        original = gateway.create
        def delayed(row):
            evidence = original(row)
            self.now = 1600
            return evidence
        gateway.create = delayed
        self.p.work_once()
        row = self.p.get(101, payment)
        self.assertEqual(row['status'], 'review')
        self.assertIsNotNone(row['platform_id'])
        self.assertIsNone(row['pay_url'])
        self.assertTrue(self.db.one("SELECT * FROM audit WHERE action='payment_create_after_deadline'"))
        self.assertFalse(any('剩余' in message['content'] for message in self.db.all('SELECT content FROM messages')))

    def test_unknown_closure_is_admin_only_audited_and_allows_new_local_order(self):
        self.setup_payments()
        self.gateway.fail_create = True
        payment = self.p.request(101, '3')
        self.p.work_once()
        with self.assertRaises(RuleError):
            self.p.close_no_credit(102, payment, '不能越权')
        with self.assertRaises(RuleError):
            self.p.close_no_credit(9001, payment, '')
        self.p.close_no_credit(9001, payment, '模拟人工核实，关闭本地异常单，不发放积分')
        self.p.close_no_credit(9001, payment, '重复关闭')
        self.assertEqual(self.p.get(101, payment)['status'], 'closed')
        self.assertEqual(len(self.db.all("SELECT * FROM audit WHERE action='payment_closed_no_credit'")), 1)
        self.assertEqual(self.s.balance(101), (10, 0))
        second = self.p.request(101, '3')
        self.assertNotEqual(second, payment)

    def test_inflight_job_must_finish_or_recover_before_admin_closure(self):
        payment = self.payment()
        self.p.request_check(101, payment)
        self.p.claim_job()
        with self.assertRaises(RuleError):
            self.p.close_no_credit(9001, payment, '任务尚在执行，不能覆盖其结果')
        self.s.recover()
        self.p.close_no_credit(9001, payment, '恢复后人工关闭未知结果')
        self.assertEqual(self.p.get(101, payment)['status'], 'closed')

    def test_distinct_verified_order_and_transaction_ids_credit_once(self):
        payment = self.payment()
        self.now = 1002
        self.settle(payment, transaction_id='independent-verified-transaction')
        self.assertEqual(self.p.get(101, payment)['status'], 'paid')
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_identity_claims WHERE payment_id=?', (payment,))), 2)

    def test_same_transaction_with_different_platform_orders_cannot_credit_twice(self):
        payment = self.payment()
        self.now = 1002
        self.settle(payment, transaction_id='shared-verified-transaction')
        second = self.p.request(102, '3')
        self.p.work_once()
        row = self.p.get(102, second)
        self.gateway.results[second] = replace(self.gateway.paid(row, paid_at=1003), transaction_id='shared-verified-transaction')
        self.now = 1004
        self.p.request_check(102, second)
        self.p.work_once()
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(self.s.balance(102), (0, 0))
        self.assertEqual(self.p.get(102, second)['status'], 'review')

    def test_late_discovery_keeps_actual_paid_time_without_declaring_late_payment(self):
        payment = self.payment()
        self.now = 1601
        self.settle(payment, paid_at=1001)
        row = self.p.get(101, payment)
        self.assertEqual(row['status'], 'review')
        self.assertIn('发现', row['reason'])
        self.assertNotIn('晚付', row['reason'])
        observation = self.db.one("SELECT * FROM payment_observations WHERE payment_id=? ORDER BY id DESC", (payment,))
        self.assertEqual(observation['observed_at'], 1601)
        self.assertIn('1001', observation['evidence'])
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_actual_deadline_paid_time_requires_review_even_without_discovery_delay(self):
        payment = self.payment()
        self.now = 1600
        self.settle(payment, paid_at=1600)
        self.assertEqual(self.p.get(101, payment)['status'], 'review')
        self.assertIn('付款时间', self.p.get(101, payment)['reason'])
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_closed_order_late_paid_evidence_never_reopens_automatic_credit(self):
        payment = self.payment()
        self.now = 1002
        self.settle(payment, payer=None)
        self.p.close_no_credit(9001, payment, '身份待核实，本地关闭不发放')
        self.now = 1601
        self.settle(payment, transaction_id='closed-order-transaction')
        self.assertEqual(self.p.get(101, payment)['status'], 'closed_review')
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 0)
        self.p.close_no_credit(9001, payment, '新证据另行核实，继续不自动入账')
        self.assertEqual(self.p.get(101, payment)['status'], 'closed')

    def test_replayed_job_observation_is_idempotent(self):
        payment = self.payment()
        self.now = 1002
        self.p.request_check(101, payment)
        job = self.p.claim_job()
        evidence = replace(self.gateway.paid(job['payment'], paid_at=1001), transaction_id='replay-transaction')
        self.p.finish_job(job, evidence, observed_at=1002)
        self.p.finish_job(job, evidence, observed_at=1003)
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_observations WHERE job_id=?', (job['id'],))), 1)

    def test_stale_create_response_after_recovery_and_closure_is_never_pending(self):
        self.setup_payments()
        payment = self.p.request(101, '3')
        job = self.p.claim_job()
        evidence = self.gateway.create(job['payment'])
        self.s.recover()
        self.p.close_no_credit(9001, payment, '中断任务已核实，本地关闭')
        self.p.finish_job(job, evidence, observed_at=1001)
        self.assertIn(self.p.get(101, payment)['status'], {'closed', 'closed_review'})
        self.assertIsNone(self.p.get(101, payment)['pay_url'])
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_restart_expires_unsent_create_and_never_retries_uncertain_create(self):
        self.setup_payments()
        payment = self.p.request(101, '3')
        other = Store(self.path)
        try:
            service = Service(other, {9001}, clock=lambda: 1600)
            service.recover()
            p = Payments(service, SimulatedGateway())
            p.work_once()
            self.assertEqual(p.get(101, payment)['status'], 'expired')
            self.assertIsNone(p.get(101, payment)['platform_id'])
        finally:
            other.close()

    def test_two_connections_can_create_only_one_active_payment_for_same_user(self):
        self.setup_payments()
        barrier = threading.Barrier(2)
        outcomes = []
        def run():
            db = Store(self.path)
            try:
                payments = Payments(Service(db, {9001}, clock=lambda: 1000), SimulatedGateway())
                barrier.wait()
                try:
                    outcomes.append(('created', payments.request(101, '3')))
                except RuleError:
                    outcomes.append(('blocked', None))
            finally:
                db.close()
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(x[0] for x in outcomes), ['blocked', 'created'])
        self.assertEqual(len(self.db.all('SELECT * FROM payments')), 1)

    def test_cross_connection_transaction_race_credits_exactly_one_order(self):
        first = self.payment()
        second = self.p.request(102, '3')
        self.p.work_once()
        self.now = 1002
        for actor, payment in [(101, first), (102, second)]:
            self.gateway.results[payment] = replace(self.gateway.paid(self.p.get(actor, payment), paid_at=1001), transaction_id='raced-transaction')
            self.p.request_check(actor, payment)
        barrier = threading.Barrier(2)
        errors = []
        def run():
            db = Store(self.path)
            try:
                p = Payments(Service(db, {9001}, clock=lambda: 1002), self.gateway)
                barrier.wait()
                p.work_once()
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                db.close()
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(self.s.balance(101)[0] + self.s.balance(102)[0], 13)
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 1)
        self.assertEqual(sorted(r['status'] for r in self.db.all('SELECT status FROM payments')), ['paid', 'review'])

    def test_admin_closure_command_is_private_authorized_and_preserves_disabled_gateway(self):
        from tests.test_app import update
        from zhbot.app import Application
        self.setup_payments()
        self.s.register(9001)
        self.gateway.fail_create = True
        payment = self.p.request(101, '3')
        self.p.work_once()
        app = Application(self.s, self.p)
        app.process_update(update(0, f'/payment_close {payment} 模拟核实', 9001, chat_type='group'))
        app.process_update(update(1, f'/payment_close {payment} 越权核实', 102))
        self.assertEqual(self.p.get(101, payment)['status'], 'unknown')
        app.process_update(update(2, f'/payment_close {payment} 已核实，仅本地关闭不入账', 9001))
        self.assertEqual(self.p.get(101, payment)['status'], 'closed')
        self.assertEqual(self.db.one("SELECT actor FROM audit WHERE action='payment_closed_no_credit'")['actor'], 9001)
        self.assertEqual(self.s.balance(101), (10, 0))
        with self.assertRaises(RuleError):
            Payments(self.s).request(101, '3')

    def test_paid_order_cannot_be_closed_or_credited_again(self):
        payment = self.payment()
        self.now = 1002
        self.settle(payment)
        with self.assertRaises(RuleError):
            self.p.close_no_credit(9001, payment, '不得关闭已入账单')
        self.p.request_check(101, payment)
        self.p.work_once()
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(self.p.get(101, payment)['status'], 'paid')

    def test_two_workers_dispatch_create_only_once(self):
        gateway = self.counted_gateway()
        payment = self.p.request(101, '3')
        barrier = threading.Barrier(2)
        errors = []
        def run():
            db = Store(self.path)
            try:
                p = Payments(Service(db, {9001}, clock=lambda: 1000), gateway)
                barrier.wait()
                p.work_once()
            except Exception as error:
                errors.append(type(error).__name__)
            finally:
                db.close()
        threads = [threading.Thread(target=run) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(gateway.calls, 1)
        self.assertEqual(self.p.get(101, payment)['status'], 'pending')

    def test_closure_racing_inflight_check_cannot_overwrite_result(self):
        payment = self.payment()
        self.now = 1002
        self.settle(payment, payer=None)
        row = self.p.get(101, payment)
        evidence = self.gateway.paid(row, paid_at=1001)
        attempts = []
        def check(_):
            other = Store(self.path)
            try:
                p = Payments(Service(other, {9001}, clock=lambda: 1002), self.gateway)
                try:
                    p.close_no_credit(9001, payment, '与查单并发的核实关闭')
                except RuleError:
                    attempts.append('blocked')
            finally:
                other.close()
            return evidence
        self.gateway.check = check
        self.p.request_check(101, payment)
        self.p.work_once()
        self.assertEqual(attempts, ['blocked'])
        self.assertEqual(self.p.get(101, payment)['status'], 'review')
        self.assertEqual(self.s.balance(101), (10, 0))
        self.p.close_no_credit(9001, payment, '查单完成后核实关闭')
        self.assertEqual(self.p.get(101, payment)['status'], 'closed')

    def test_repeated_late_evidence_is_retained_without_credit(self):
        payment = self.payment()
        self.now = 1601
        self.settle(payment)
        self.now = 1602
        self.p.request_check(101, payment)
        self.p.work_once()
        self.assertEqual(self.p.get(101, payment)['status'], 'review')
        self.assertEqual(self.s.balance(101), (10, 0))
        observations = self.db.all('SELECT observed_at FROM payment_observations WHERE payment_id=? ORDER BY id', (payment,))
        self.assertEqual([row['observed_at'] for row in observations], [1000, 1601, 1602])
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 0)

    def test_closed_uncertain_creation_later_payment_reserves_transaction_against_new_order(self):
        self.setup_payments()
        self.gateway.fail_create = True
        old = self.p.request(101, '3')
        self.p.work_once()
        self.p.close_no_credit(9001, old, '创建结果不明，经人工核实本地关闭')
        self.gateway.fail_create = False
        new = self.p.request(101, '3')
        self.p.work_once()
        # Simulate an upstream bill that existed despite the lost create response.
        old_row = self.p.get(101, old)
        creation = self.gateway.create(old_row)
        old_row = dict(old_row, platform_id=creation.platform_id)
        old_paid = self.gateway.paid(old_row, paid_at=1001)
        self.gateway.results[old] = old_paid
        self.now = 1002
        self.p.request_check(101, old)
        self.p.work_once()
        self.assertEqual(self.p.get(101, old)['status'], 'closed_review')
        new_row = self.p.get(101, new)
        self.gateway.results[new] = replace(self.gateway.paid(new_row, paid_at=1001), transaction_id=old_paid.transaction_id)
        self.p.request_check(101, new)
        self.p.work_once()
        self.assertEqual(self.p.get(101, new)['status'], 'review')
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 0)
        self.assertTrue(self.db.one("SELECT * FROM audit WHERE action='payment_closed_no_credit' AND ref=?", (old,)))

    def test_schema_two_paid_transaction_still_blocks_reuse_after_migration(self):
        import sqlite3
        from pathlib import Path
        payment = self.payment()
        self.now = 1002
        self.settle(payment)
        transaction = self.db.one('SELECT transaction_id FROM payment_transactions')['transaction_id']
        legacy = str(Path(self.tmp.name) / 'payment-v2.sqlite3')
        connection = sqlite3.connect(legacy)
        self.db.connection.backup(connection)
        connection.execute('DROP TABLE payment_observations')
        connection.execute('DROP TABLE payment_identity_claims')
        connection.execute('PRAGMA user_version=2')
        connection.commit()
        connection.close()
        other = Store(legacy)
        try:
            service = Service(other, {9001}, clock=lambda: 1002)
            p = Payments(service, self.gateway)
            second = p.request(102, '3')
            p.work_once()
            evidence = self.gateway.paid(p.get(102, second), paid_at=1003)
            self.gateway.results[second] = replace(evidence, transaction_id=transaction)
            service.clock = lambda: 1004
            p.request_check(102, second)
            p.work_once()
            self.assertEqual(service.balance(101), (13, 0))
            self.assertEqual(service.balance(102), (0, 0))
            self.assertEqual(p.get(102, second)['status'], 'review')
            self.assertEqual(len(other.all('SELECT * FROM payment_transactions')), 1)
        finally:
            other.close()

    def test_timely_authenticated_observation_can_commit_after_db_delay(self):
        payment = self.payment()
        self.now = 1599
        self.p.request_check(101, payment)
        job = self.p.claim_job()
        evidence = self.gateway.paid(job['payment'], paid_at=1001)
        self.now = 1601  # Simulated delay between response observation and DB commit.
        self.p.finish_job(job, evidence, observed_at=1599)
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(self.p.get(101, payment)['status'], 'paid')
        self.assertEqual(self.db.one('SELECT observed_at FROM payment_observations WHERE job_id=?', (job['id'],))['observed_at'], 1599)

    def test_unverifiable_time_order_never_credits(self):
        payment = self.payment()
        self.now = 1002
        self.settle(payment, paid_at=1003)
        self.assertEqual(self.p.get(101, payment)['status'], 'review')
        self.assertEqual(self.s.balance(101), (10, 0))


if __name__ == '__main__':
    unittest.main()
