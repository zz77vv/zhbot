import sqlite3
import threading
import unittest

from tests import test_orders
from zhbot.core import RuleError
from zhbot.service import Service
from zhbot.store import Store


class AdjustmentTests(unittest.TestCase):
    setUp = test_orders.OrderTests.setUp
    tearDown = test_orders.OrderTests.tearDown

    def adjust(self, delta=3, reason='人工核实补分', request='a' * 32, **kw):
        return self.s.adjust_points(9001, 101, delta, reason, request, **kw)

    def test_positive_negative_adjustments_are_audited_and_preserve_frozen(self):
        self.db.execute('UPDATE users SET frozen=6 WHERE tg_id=101')
        self.adjust()
        self.adjust(-2, '人工纠正多计积分', 'b' * 32)
        self.assertEqual(self.s.balance(101), (11, 6))
        entries = self.db.all('SELECT * FROM ledger ORDER BY id')
        self.assertEqual([(x['available_delta'], x['frozen_delta'], x['available_after']) for x in entries],
                         [(3, 0, 13), (-2, 0, 11)])
        records = self.db.all('SELECT * FROM point_adjustments ORDER BY rowid')
        self.assertEqual([(r['actor'], r['user_id'], r['reason']) for r in records],
                         [(9001, 101, '人工核实补分'), (9001, 101, '人工纠正多计积分')])
        self.assertEqual(len(self.db.all("SELECT * FROM audit WHERE action='points_adjusted'")), 2)
        self.assertEqual(len(self.db.all('SELECT * FROM messages')), 2)
        self.assertEqual(self.db.all('SELECT * FROM payments'), [])

    def test_request_is_idempotent_and_cannot_be_rebound(self):
        first = self.adjust()
        self.assertEqual(self.adjust(), first)
        self.assertEqual(self.s.balance(101), (13, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM ledger')), 1)
        for delta, reason in [(4, '人工核实补分'), (3, '改变原因')]:
            with self.assertRaises(RuleError):
                self.adjust(delta, reason)
        self.s.admins = frozenset({9002})
        with self.assertRaises(RuleError):
            self.adjust()

    def test_invalid_authority_target_amount_reason_and_key_have_no_effect(self):
        for actor, target, delta, reason, key in [(101, 101, 3, '原因', 'a'*32),
                (9001, 999, 3, '原因', 'a'*32), (9001, 101, True, '原因', 'a'*32),
                (9001, 101, 0, '原因', 'a'*32), (9001, 101, 1000001, '原因', 'a'*32),
                (9001, 101, -11, '原因', 'a'*32), (9001, 101, 1, '  ', 'a'*32),
                (9001, 101, 1, '原因'*501, 'a'*32), (9001, 101, 1, '原因', '../bad')]:
            with self.subTest(actor=actor, delta=delta, key=key), self.assertRaises(RuleError):
                self.s.adjust_points(actor, target, delta, reason, key)
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])

    def test_confirmation_snapshot_and_balance_ceiling_rechecked(self):
        with self.assertRaises(RuleError):
            self.adjust(expected=(9, 0))
        self.db.execute('UPDATE users SET available=1000000000000 WHERE tg_id=101')
        with self.assertRaises(RuleError):
            self.adjust()
        self.assertEqual(self.db.all('SELECT * FROM ledger'), [])

    def test_audit_or_notification_failure_rolls_back_everything(self):
        for table in ['audit', 'messages']:
            self.db.execute(f"CREATE TRIGGER broken BEFORE INSERT ON {table} BEGIN SELECT RAISE(ABORT,'injected'); END")
            with self.assertRaises(sqlite3.Error):
                self.adjust()
            self.assertEqual(self.s.balance(101), (10, 0))
            self.assertEqual(self.db.all('SELECT * FROM ledger'), [])
            self.assertEqual(self.db.all('SELECT * FROM point_adjustments'), [])
            self.db.execute('DROP TRIGGER broken')
        self.adjust()
        self.assertEqual(self.s.balance(101), (13, 0))

    def test_two_connections_cannot_overdraw_available_or_repeat_request(self):
        barrier = threading.Barrier(2)
        outcomes = []
        def run(key):
            db = Store(self.path)
            try:
                s = Service(db, {9001})
                barrier.wait()
                try:
                    s.adjust_points(9001, 101, -7, '两管理员竞争扣减', key)
                    outcomes.append('done')
                except RuleError:
                    outcomes.append('rejected')
            finally:
                db.close()
        threads = [threading.Thread(target=run, args=(key*32,)) for key in ['c', 'd']]
        for t in threads: t.start()
        for t in threads: t.join()
        self.assertCountEqual(outcomes, ['done', 'rejected'])
        self.assertEqual(self.s.balance(101), (3, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM ledger')), 1)

    def test_positive_adjustment_reserves_headroom_for_refund_of_frozen_points(self):
        self.db.execute('UPDATE users SET available=1000000000000 WHERE tg_id=101')
        draft = self.s.new_draft(101, self.project)
        self.s.add_customer_item(101, 'text', '材料')
        self.s.preview_customer(101, draft)
        while (m := self.s.claim_message()) is not None: self.s.sent(m['id'], 1000+m['id'])
        order = self.s.confirm(101, draft, 1)
        with self.assertRaises(RuleError): self.adjust(6)
        self.s.fail_order(9001, order, '人工确认终止，退回冻结')
        self.assertEqual(self.s.balance(101), (1000000000000, 0))
