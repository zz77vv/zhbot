from dataclasses import replace
from pathlib import Path
import sqlite3
import threading
import unittest

from tests import test_payment_safety
from zhbot.payments import Payments
from zhbot.service import Service
from zhbot.store import Store


class PaymentQuarantineTests(unittest.TestCase):
    setUp = test_payment_safety.PaymentSafetyTests.setUp
    tearDown = test_payment_safety.PaymentSafetyTests.tearDown
    setup_payments = test_payment_safety.PaymentSafetyTests.setup_payments
    payment = test_payment_safety.PaymentSafetyTests.payment
    settle = test_payment_safety.PaymentSafetyTests.settle

    def old_closed(self):
        old = self.payment()
        self.now = 1002
        self.settle(old, transaction_id='verified-T1', payer=None)
        self.p.close_no_credit(9001, old, '付款人未知，本地关闭不入账')
        return old

    def check_for(self, actor, payment, transaction):
        row = self.p.get(actor, payment)
        self.gateway.results[payment] = replace(self.gateway.paid(row, paid_at=self.now), transaction_id=transaction)
        self.p.request_check(actor, payment)
        self.p.work_once()

    def test_conflicting_transaction_is_quarantined_and_cannot_credit_another_user(self):
        old = self.old_closed()
        self.settle(old, transaction_id='verified-T2')
        self.p.close_no_credit(9001, old, '新冲突已登记，本地再次关闭仍保留隔离')
        new = self.p.request(102, '3')
        self.p.work_once()
        self.check_for(102, new, 'verified-T2')
        self.assertEqual(self.s.balance(102), (0, 0))
        self.assertEqual(self.p.get(102, new)['status'], 'review')
        self.assertEqual(self.s.balance(101), (10, 0))
        claim = self.db.one("SELECT value FROM payment_identity_claims WHERE payment_id=? AND kind='transaction'", (old,))
        self.assertEqual(claim['value'], 'verified-T1')
        sightings = self.db.all("SELECT value,quarantined FROM payment_identity_sightings WHERE payment_id=? AND kind='transaction' ORDER BY value", (old,))
        self.assertEqual(sightings, [{'value': 'verified-T1', 'quarantined': 1}, {'value': 'verified-T2', 'quarantined': 1}])
        self.assertTrue(self.db.one("SELECT * FROM audit WHERE action='payment_identity_quarantined' AND ref=?", (old,)))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 0)

    def test_conflicting_platform_and_transaction_bundle_are_both_preserved(self):
        old = self.old_closed()
        self.settle(old, platform_id='conflicting-platform', transaction_id='verified-T2')
        new = self.p.request(102, '3')
        original = self.gateway.create
        self.gateway.create = lambda row: replace(original(row), platform_id='conflicting-platform')
        self.p.work_once()
        self.assertEqual(self.p.get(102, new)['status'], 'review')
        self.assertIsNone(self.p.get(102, new)['pay_url'])
        records = self.db.all("SELECT kind,value FROM payment_identity_sightings WHERE payment_id=? AND quarantined=1", (old,))
        self.assertIn({'kind': 'platform', 'value': 'conflicting-platform'}, records)
        self.assertIn({'kind': 'transaction', 'value': 'verified-T2'}, records)
        self.assertNotEqual(self.p.get(101, old)['platform_id'], 'conflicting-platform')

    def test_untrusted_or_foreign_order_evidence_cannot_poison_identity_quarantine(self):
        cases = [{'verified': False}, {'merchant': 'foreign-merchant'}, {'unique_id': 'foreign-order'}]
        for index, change in enumerate(cases):
            with self.subTest(change=change):
                if index:
                    self.tearDown()
                    self.setUp()
                old = self.old_closed()
                self.settle(old, transaction_id='not-authenticated-T2', **change)
                new = self.p.request(102, '3')
                self.p.work_once()
                self.check_for(102, new, 'not-authenticated-T2')
                self.assertEqual(self.s.balance(102), (3, 0))
                self.assertEqual(self.p.get(102, new)['status'], 'paid')

    def test_conflicting_replay_for_same_job_is_quarantined_without_recredit(self):
        old = self.payment()
        self.now = 1002
        self.p.request_check(101, old)
        job = self.p.claim_job()
        first = replace(self.gateway.paid(job['payment'], paid_at=1001), transaction_id='verified-T1')
        self.p.finish_job(job, first, observed_at=1002)
        self.p.finish_job(job, replace(first, transaction_id='verified-T2'), observed_at=1003)
        new = self.p.request(102, '3')
        self.p.work_once()
        self.check_for(102, new, 'verified-T2')
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(self.s.balance(102), (0, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 1)
        self.assertTrue(self.db.one("SELECT * FROM payment_identity_sightings WHERE observation_id IN (SELECT id FROM payment_observations WHERE job_id=?) AND value='verified-T2' AND quarantined=1", (job['id'],)))

    def test_two_connections_cannot_credit_a_quarantined_transaction_after_restart(self):
        old = self.old_closed()
        self.settle(old, transaction_id='verified-T2')
        self.s.register(103)
        pending = []
        for actor in [102, 103]:
            payment = self.p.request(actor, '3')
            self.p.work_once()
            row = self.p.get(actor, payment)
            self.gateway.results[payment] = replace(self.gateway.paid(row, paid_at=self.now), transaction_id='verified-T2')
            pending.append((actor, payment))
        for actor, payment in pending:
            self.p.request_check(actor, payment)
        barrier = threading.Barrier(2)
        errors = []
        def run():
            db = Store(self.path)
            try:
                service = Service(db, {9001}, clock=lambda: 1002)
                service.recover()
                p = Payments(service, self.gateway)
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
        self.assertEqual(self.s.balance(102), (0, 0))
        self.assertEqual(self.s.balance(103), (0, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 0)

    def test_schema_three_history_rebuilds_quarantine_before_new_credit(self):
        old = self.old_closed()
        self.settle(old, transaction_id='verified-T2')
        legacy = str(Path(self.tmp.name) / 'payment-v3.sqlite3')
        connection = sqlite3.connect(legacy)
        self.db.connection.backup(connection)
        connection.execute('DROP TABLE IF EXISTS payment_identity_sightings')
        connection.execute('PRAGMA user_version=3')
        connection.commit()
        connection.close()
        other = Store(legacy)
        try:
            service = Service(other, {9001}, clock=lambda: 1002)
            p = Payments(service, self.gateway)
            new = p.request(102, '3')
            p.work_once()
            evidence = replace(self.gateway.paid(p.get(102, new), paid_at=1002), transaction_id='verified-T2')
            self.gateway.results[new] = evidence
            p.request_check(102, new)
            p.work_once()
            self.assertEqual(service.balance(102), (0, 0))
            self.assertEqual(p.get(102, new)['status'], 'review')
            self.assertTrue(other.one("SELECT * FROM payment_identity_sightings WHERE value='verified-T2' AND payment_id=? AND quarantined=1", (old,)))
            self.assertTrue(other.one("SELECT * FROM audit WHERE action='payment_identity_migration_quarantine' AND ref=?", (old,)))
        finally:
            other.close()

    def test_exact_conflicting_replay_is_idempotent_for_quarantine_and_audit(self):
        old = self.old_closed()
        self.p.request_check(101, old)
        job = self.p.claim_job()
        evidence = replace(self.gateway.paid(job['payment'], paid_at=1001), transaction_id='verified-T2')
        self.p.finish_job(job, evidence, observed_at=1002)
        before = tuple(self.db.one(f'SELECT COUNT(*) AS n FROM {table}')['n']
                       for table in ['payment_identity_sightings', 'audit', 'messages'])
        self.p.finish_job(job, evidence, observed_at=1003)
        after = tuple(self.db.one(f'SELECT COUNT(*) AS n FROM {table}')['n']
                      for table in ['payment_identity_sightings', 'audit', 'messages'])
        self.assertEqual(after, before)
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_storage_failure_rolls_back_observation_and_quarantine_then_recovery_retains_conflict(self):
        old = self.old_closed()
        self.p.request_check(101, old)
        job = self.p.claim_job()
        evidence = replace(self.gateway.paid(job['payment'], paid_at=1001), transaction_id='verified-T2')
        self.db.execute("CREATE TRIGGER quarantine_failure BEFORE UPDATE ON payment_identity_sightings WHEN NEW.quarantined=1 BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.Error):
            self.p.finish_job(job, evidence, observed_at=1002)
        self.assertIsNone(self.db.one('SELECT * FROM payment_observations WHERE job_id=?', (job['id'],)))
        self.assertIsNone(self.db.one("SELECT * FROM payment_identity_sightings WHERE value='verified-T2'"))
        self.assertEqual(self.db.one('SELECT status FROM payment_jobs WHERE id=?', (job['id'],))['status'], 'sending')
        self.db.execute('DROP TRIGGER quarantine_failure')
        self.s.recover()
        self.p.finish_job(job, evidence, observed_at=1002)
        new = self.p.request(102, '3')
        self.p.work_once()
        self.check_for(102, new, 'verified-T2')
        self.assertEqual(self.s.balance(102), (0, 0))
        self.assertTrue(self.db.one("SELECT * FROM payment_identity_sightings WHERE value='verified-T2' AND quarantined=1"))

    def test_conflict_discovered_after_prior_credit_is_flagged_without_reversing_ledger(self):
        from tests.test_app import update
        from zhbot.app import Application
        old = self.old_closed()
        new = self.p.request(102, '3')
        self.p.work_once()
        self.check_for(102, new, 'verified-T2')
        self.assertEqual(self.s.balance(102), (3, 0))
        self.settle(old, transaction_id='verified-T2')
        self.assertEqual(self.s.balance(102), (3, 0))
        self.assertEqual(self.p.get(102, new)['status'], 'paid')
        self.assertEqual(len(self.db.all('SELECT * FROM payment_transactions')), 1)
        self.assertTrue(self.db.one("SELECT * FROM payment_identity_sightings WHERE payment_id=? AND value='verified-T2' AND quarantined=1", (new,)))
        self.s.register(9001)
        Application(self.s, self.p).process_update(update(10, '/review', 9001))
        review = self.db.one('SELECT MAX(id) AS id FROM batches')['id']
        content = '\n'.join(row['content'] for row in self.db.all('SELECT content FROM messages WHERE batch_id=?', (review,)))
        self.assertIn(new, content)
        self.assertIn('隔离', content)


if __name__ == '__main__':
    unittest.main()
