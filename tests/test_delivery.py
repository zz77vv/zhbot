import unittest

from zhbot.core import RuleError
from zhbot.service import Service
from zhbot.store import Store
from tests import test_orders


class DeliveryTests(unittest.TestCase):
    setUp = test_orders.OrderTests.setUp
    tearDown = test_orders.OrderTests.tearDown
    draft = test_orders.OrderTests.draft
    preview = test_orders.OrderTests.preview

    def ready(self):
        draft = self.draft()
        order = self.s.confirm(101, draft, self.preview(draft))
        self.s.open_receipt(9001, order)
        self.s.add_receipt_item(9001, 'text', '模拟回执')
        self.s.add_receipt_item(9001, 'photo', 'fake-photo-1')
        self.s.add_receipt_item(9001, 'photo', 'fake-photo-2')
        self.s.add_receipt_item(9001, 'document', 'fake-file-1')
        self.s.add_receipt_item(9001, 'document', 'fake-file-2')
        self.s.preview_receipt(9001, order)
        while (msg := self.s.claim_message()) is not None:
            self.s.sent(msg['id'], 1000 + msg['id'])
        return order, self.s.receipt(9001, order)['revision']

    def test_complete_mixed_delivery_charges_once_after_last_item(self):
        order, revision = self.ready()
        batch = self.s.deliver(9001, order, revision)
        self.assertEqual(self.s.deliver(9001, order, revision), batch)
        kinds = []
        for i in range(5):
            msg = self.s.claim_message()
            kinds.append(msg['kind'])
            self.assertEqual(self.s.balance(101), (4, 6))
            self.s.sent(msg['id'], 2000 + i)
        self.assertEqual(kinds, ['text', 'photo', 'photo', 'document', 'document'])
        self.assertEqual(self.s.balance(101), (4, 0))
        self.assertEqual(self.s.order(101, order)['status'], 'completed')
        self.assertEqual(self.s.deliver(9001, order, revision), batch)
        self.assertEqual(len(self.db.all("SELECT * FROM ledger WHERE entry_key LIKE 'charge:%'")), 1)
        with self.assertRaises(RuleError):
            self.s.fail_order(9001, order, '不能退回已完成冻结')

    def test_changed_receipt_invalidates_preview(self):
        order, revision = self.ready()
        self.s.add_receipt_item(9001, 'text', '修改')
        with self.assertRaises(RuleError):
            self.s.deliver(9001, order, revision)
        with self.assertRaises(RuleError):
            self.s.deliver(9001, order, revision + 1)
        self.assertEqual(self.s.balance(101), (4, 6))

    def test_receipt_and_selection_survive_restart(self):
        order, revision = self.ready()
        other = Store(self.path)
        try:
            service = Service(other, {9001})
            self.assertEqual(len(service.receipt(9001, order)['items']), 5)
            service.add_receipt_item(9001, 'text', '重启继续编辑')
            self.assertEqual(service.receipt(9001, order)['revision'], revision + 1)
        finally:
            other.close()

    def test_preview_requires_all_parts_acknowledged(self):
        order, _ = self.ready()
        self.s.add_receipt_item(9001, 'text', '新版本')
        self.s.preview_receipt(9001, order)
        msg = self.s.claim_message()
        self.s.uncertain(msg['id'], '模拟超时')
        with self.assertRaises(RuleError):
            self.s.deliver(9001, order, 6)

    def test_uncertain_send_retains_freeze_and_never_retries(self):
        order, revision = self.ready()
        self.s.deliver(9001, order, revision)
        msg = self.s.claim_message()
        self.s.uncertain(msg['id'], '模拟丢失响应')
        self.assertEqual(self.s.order(101, order)['status'], 'delivery_review')
        self.assertEqual(self.s.balance(101), (4, 6))
        for _ in range(10):
            next_msg = self.s.claim_message()
            if next_msg is None:
                break
            self.assertNotEqual(next_msg['batch_kind'], 'delivery')
            self.s.sent(next_msg['id'], 3000 + next_msg['id'])
        with self.assertRaises(RuleError):
            self.s.fail_order(9001, order, '不允许跳过核实')

    def test_restart_converts_inflight_to_review(self):
        order, revision = self.ready()
        self.s.deliver(9001, order, revision)
        msg = self.s.claim_message()
        other = Store(self.path)
        try:
            restored = Service(other, {9001})
            restored.recover()
            restored.recover()
            self.assertEqual(restored.order(101, order)['status'], 'delivery_review')
            self.assertEqual(restored.balance(101), (4, 6))
            self.assertEqual(other.one('SELECT status FROM messages WHERE id=?', (msg['id'],))['status'], 'unknown')
        finally:
            other.close()

    def test_human_resolution_can_finish_without_duplicate_send(self):
        order, revision = self.ready()
        self.s.deliver(9001, order, revision)
        msg = self.s.claim_message()
        self.s.uncertain(msg['id'], '未知')
        with self.assertRaises(RuleError):
            self.s.resolve(102, msg['id'], True, '越权核实')
        with self.assertRaises(RuleError):
            self.s.resolve(9001, msg['id'], True, '')
        self.s.resolve(9001, msg['id'], True, '已人工核对模拟聊天记录')
        seen = []
        while (next_msg := self.s.claim_message()) is not None:
            seen.append(next_msg['id'])
            self.s.sent(next_msg['id'], 4000 + next_msg['id'])
        self.assertNotIn(msg['id'], seen)
        self.assertEqual(self.s.balance(101), (4, 0))
        self.assertEqual(self.s.order(101, order)['status'], 'completed')
        self.assertTrue(self.db.one("SELECT * FROM audit WHERE action='resolve_sent'"))

    def test_verified_failure_releases_and_cancels_rest(self):
        order, revision = self.ready()
        batch = self.s.deliver(9001, order, revision)
        msg = self.s.claim_message()
        self.s.uncertain(msg['id'], '未知')
        self.s.resolve(9001, msg['id'], False, '人工核对确认交付失败')
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(self.s.order(101, order)['status'], 'failed')
        self.assertFalse(self.db.one("SELECT * FROM messages WHERE batch_id=? AND status='pending'", (batch,)))
        with self.assertRaises(RuleError):
            self.s.resolve(9001, msg['id'], False, '重复核实')

    def test_definite_first_failure_releases_but_partial_requires_review(self):
        order, revision = self.ready()
        self.s.deliver(9001, order, revision)
        msg = self.s.claim_message()
        self.s.rejected(msg['id'], '模拟明确拒绝')
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_partial_delivery_definite_rejection_requires_review(self):
        order, revision = self.ready()
        self.s.deliver(9001, order, revision)
        first = self.s.claim_message()
        self.s.sent(first['id'], 5001)
        second = self.s.claim_message()
        self.s.rejected(second['id'], '模拟文件被拒绝')
        self.assertEqual(self.s.balance(101), (4, 6))
        self.assertEqual(self.s.order(101, order)['status'], 'delivery_review')
        self.s.resolve(9001, second['id'], False, '确认部分交付失败，整单不扣费')
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_inflight_receipt_cannot_be_edited(self):
        order, revision = self.ready()
        self.s.deliver(9001, order, revision)
        with self.assertRaises(RuleError):
            self.s.add_receipt_item(9001, 'text', '交付中修改')
        with self.assertRaises(RuleError):
            self.s.clear_selected(9001)

    def test_undo_and_clear_invalidate_preview(self):
        order, revision = self.ready()
        self.s.undo_selected(9001)
        self.assertEqual(len(self.s.receipt(9001, order)['items']), 4)
        self.assertIsNone(self.s.receipt(9001, order)['preview_revision'])
        self.s.clear_selected(9001)
        self.assertEqual(len(self.s.receipt(9001, order)['items']), 0)

    def test_admin_rotation_does_not_break_committed_delivery_or_recovery(self):
        order, revision = self.ready()
        self.s.deliver(9001, order, revision)
        msg = self.s.claim_message()
        self.s.admins = frozenset({9002})
        self.s.recover()
        self.assertEqual(self.s.order(101, order)['status'], 'delivery_review')
        self.s.resolve(9002, msg['id'], True, '新管理员核实已发送')
        while (msg := self.s.claim_message()) is not None:
            self.s.sent(msg['id'], 5000 + msg['id'])
        self.assertEqual(self.s.order(101, order)['status'], 'completed')
        self.assertEqual(self.s.balance(101), (4, 0))

    def test_each_receipt_part_crash_requires_exactly_one_human_resolution(self):
        for index in range(5):
            with self.subTest(index=index):
                # Independent temporary database for every crash position.
                self.tearDown()
                self.setUp()
                order, revision = self.ready()
                self.s.deliver(9001, order, revision)
                for _ in range(index):
                    msg = self.s.claim_message()
                    self.s.sent(msg['id'], 6000 + msg['id'])
                inflight = self.s.claim_message()
                self.s.recover()
                self.assertEqual(self.s.balance(101), (4, 6))
                self.s.resolve(9001, inflight['id'], True, '模拟记录核实送达')
                while (msg := self.s.claim_message()) is not None:
                    self.assertNotEqual(msg['id'], inflight['id'])
                    self.s.sent(msg['id'], 7000 + msg['id'])
                self.assertEqual(self.s.balance(101), (4, 0))
                self.assertEqual(self.s.order(101, order)['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
