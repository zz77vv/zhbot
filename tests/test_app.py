import sqlite3
import unittest

from tests import test_orders
from zhbot.app import Application
from zhbot.core import RuleError
from zhbot.service import Service
from zhbot.store import Store


def update(number, text=None, actor=101, *, chat_type='private', **fields):
    message = {'message_id': number + 1, 'from': {'id': actor, 'is_bot': False},
               'chat': {'id': actor, 'type': chat_type}}
    if text is not None:
        message['text'] = text
    message.update(fields)
    return {'update_id': number, 'message': message}


class AppTests(unittest.TestCase):
    setUp = test_orders.OrderTests.setUp
    tearDown = test_orders.OrderTests.tearDown

    def app(self):
        self.s.register(9001)
        return Application(self.s)

    def drain(self):
        result = []
        while (msg := self.s.claim_message()) is not None:
            result.append(msg)
            self.s.sent(msg['id'], 10000 + msg['id'])
        return result

    def test_unicode_and_oversized_numbers_do_not_block_next_update(self):
        app = self.app()
        samples = ['/new ²', '/new １２', '/recharge ١', '/confirm 1 ' + '1'*5000,
                   '/project new ² on 标题', '/deliver ² 1', '/orders ١', '/resolve ² sent 原因']
        for number, text in enumerate(samples):
            app.process_update(update(number, text, actor=9001 if number >= 4 else 101))
        app.process_update(update(20, '/balance'))
        self.assertEqual(app.offset(), 21)
        self.assertEqual(len(self.db.all('SELECT * FROM orders')), 0)
        self.assertIn('可用 10', self.drain()[-1]['content'])

    def test_duplicate_update_does_not_append_material_twice(self):
        app = self.app()
        app.process_update(update(0, f'/new {self.project}'))
        event = update(1, '模拟材料')
        app.process_update(event)
        app.process_update(event)
        self.assertEqual(len(self.s.draft(101)['items']), 1)
        self.assertEqual(app.offset(), 2)

    def test_group_admin_and_forged_private_identity_are_ignored(self):
        app = self.app()
        app.process_update(update(1, '/project new 1 on 越权项目', 9001, chat_type='group'))
        event = update(2, '/project new 1 on 伪造身份', 9001)
        event['message']['chat']['id'] = 101
        app.process_update(event)
        app.process_update(update(3, '/project new 1 on 普通用户', 101))
        self.assertEqual(len(self.db.all('SELECT * FROM projects')), 1)

    def test_non_admin_cannot_see_order_material(self):
        app = self.app()
        draft = self.s.new_draft(101, self.project)
        self.s.add_customer_item(101, 'text', '模拟私有材料')
        self.s.preview_customer(101, draft)
        self.drain()
        order = self.s.confirm(101, draft, 1)
        self.drain()
        app.process_update(update(1, f'/order {order}', 102))
        messages = self.drain()
        self.assertFalse(any('模拟私有材料' in m['content'] for m in messages))

    def test_restart_keeps_admin_attachment_target_and_preview_gate(self):
        app = self.app()
        app.process_update(update(0, f'/new {self.project}'))
        app.process_update(update(1, '模拟材料'))
        app.process_update(update(2, '/draft'))
        self.drain()
        app.process_update(update(3, '/confirm 1 1'))
        app.process_update(update(4, '/receipt 1', 9001))
        other = Store(self.path)
        try:
            restored = Application(Service(other, {9001}))
            restored.process_update(update(5, actor=9001, photo=[{'file_id': 'small'}, {'file_id': 'large'}], caption='模拟图片'))
            restored.process_update(update(6, actor=9001, document={'file_id': 'fake-doc'}, caption='模拟文件'))
            restored.process_update(update(7, '/deliver 1 2', 9001))
            self.assertEqual(restored.s.order(9001, 1)['status'], 'submitted')
            self.assertEqual([i['content'] for i in restored.s.receipt(9001, 1)['items']], ['large', 'fake-doc'])
            self.assertEqual(restored.offset(), 8)
        finally:
            other.close()

    def test_malformed_update_shapes_are_quarantined_or_ignored(self):
        app = self.app()
        events = [{'update_id': 0, 'message': []},
                  {'update_id': 1, 'message': {'chat': None}},
                  update(2, actor=101, photo=[]),
                  update(3, actor=101, document={'file_id': []}),
                  {'update_id': 4, 'callback_query': {'data': '/deliver 1 1'}},
                  update(5, actor=101, text=['bad'])]
        for event in events:
            app.process_update(event)
        app.process_update(update(6, '/balance'))
        self.assertEqual(app.offset(), 7)
        self.assertEqual(len(self.db.all('SELECT * FROM orders')), 0)

    def test_domain_and_reply_and_cursor_rollback_on_storage_failure(self):
        app = self.app()
        self.db.execute("CREATE TRIGGER cursor_failure BEFORE INSERT ON processed_updates BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.Error):
            app.process_update(update(1, f'/new {self.project}'))
        self.assertEqual(len(self.db.all('SELECT * FROM drafts')), 0)
        self.assertEqual(len(self.db.all('SELECT * FROM messages')), 0)
        self.assertEqual(app.offset(), 0)

    def test_internal_dispatch_bug_isolated_without_partial_mutation(self):
        app = self.app()
        original = app.dispatch
        def broken(actor, message):
            self.s.new_draft(actor, self.project)
            raise RuntimeError('sensitive example that must not be echoed')
        app.dispatch = broken
        app.process_update(update(1, '/new 1'))
        self.assertEqual(len(self.db.all('SELECT * FROM drafts')), 0)
        self.assertEqual(self.db.one('SELECT status FROM processed_updates WHERE id=1')['status'], 'quarantined')
        self.assertFalse(any('sensitive' in m['content'] for m in self.drain()))
        app.dispatch = original
        app.process_update(update(2, '/balance'))
        self.assertEqual(app.offset(), 3)

    def test_confirm_replay_after_failure_reports_current_state_truthfully(self):
        app = self.app()
        app.process_update(update(0, f'/new {self.project}'))
        app.process_update(update(1, '模拟材料'))
        app.process_update(update(2, '/draft'))
        self.drain()
        app.process_update(update(3, '/confirm 1 1'))
        self.s.fail_order(9001, 1, '模拟失败')
        self.drain()
        app.process_update(update(4, '/confirm 1 1'))
        messages = self.drain()
        self.assertIn('failed', messages[-1]['content'])
        self.assertNotIn('积分已冻结', messages[-1]['content'])
        self.assertEqual(self.s.balance(101), (10, 0))

    def receipt_with_personal_draft(self, *, personal=True):
        app = self.app()
        customer = self.s.new_draft(101, self.project)
        self.s.add_customer_item(101, 'text', '客户模拟材料')
        self.s.preview_customer(101, customer)
        self.drain()
        order = self.s.confirm(101, customer, 1)
        own = self.s.new_draft(9001, self.project) if personal else None
        self.s.open_receipt(9001, order)
        self.s.add_receipt_item(9001, 'text', '客户模拟回执')
        self.s.preview_receipt(9001, order)
        self.drain()
        return app, order, own

    def test_empty_personal_draft_switch_routes_materials_without_touching_receipt(self):
        app, order, own = self.receipt_with_personal_draft()
        receipt_before = self.s.receipt(9001, order)
        ledger_before = self.db.all('SELECT * FROM ledger ORDER BY id')
        app.process_update(update(10, '/draft', 9001))
        self.assertEqual(self.db.one('SELECT kind,ref FROM selections WHERE actor=9001'),
                         {'kind': 'customer', 'ref': own})
        reply = self.drain()[-1]['content']
        self.assertIn('客户草稿', reply)
        self.assertIn('添加材料', reply)
        self.assertIsNone(self.s.draft(9001, own)['preview_revision'])
        with self.assertRaises(RuleError):
            self.s.confirm(9001, own, 1)
        # Routing must survive a restart, not just an in-memory correction.
        other = Store(self.path)
        try:
            restored = Application(Service(other, {9001}))
            text = update(11, '个人模拟文字', 9001)
            restored.process_update(text)
            restored.process_update(text)
            restored.process_update(update(12, actor=9001, photo=[{'file_id': 'personal-photo'}]))
            restored.process_update(update(13, actor=9001, document={'file_id': 'personal-file'}))
        finally:
            other.close()
        self.assertEqual([item['content'] for item in self.s.draft(9001, own)['items']],
                         ['个人模拟文字', 'personal-photo', 'personal-file'])
        self.assertEqual(self.s.receipt(9001, order), receipt_before)
        self.assertEqual(self.s.balance(101), (4, 6))
        self.assertEqual(self.s.balance(9001), (0, 0))
        self.assertEqual(self.db.all('SELECT * FROM ledger ORDER BY id'), ledger_before)
        self.assertEqual(self.s.order(101, order)['status'], 'submitted')
        self.assertFalse(self.db.one("SELECT id FROM batches WHERE kind='delivery'"))

    def test_missing_personal_draft_switch_exits_receipt_editing_safely(self):
        app, order, _ = self.receipt_with_personal_draft(personal=False)
        receipt_before = self.s.receipt(9001, order)
        app.process_update(update(10, '/draft', 9001))
        self.assertIsNone(self.db.one('SELECT * FROM selections WHERE actor=9001'))
        app.process_update(update(11, '个人内容不得写进客户回执', 9001))
        self.assertEqual(self.s.receipt(9001, order), receipt_before)
        self.assertEqual(self.db.one('SELECT status FROM processed_updates WHERE id=11')['status'], 'rejected')
        self.assertEqual(self.s.balance(101), (4, 6))

    def test_nonempty_personal_draft_switch_does_not_bypass_preview_ack(self):
        app, order, own = self.receipt_with_personal_draft()
        self.s.add_customer_item(9001, 'text', '已有个人材料')
        receipt_before = self.s.receipt(9001, order)
        app.process_update(update(10, '/draft', 9001))
        self.assertEqual(self.db.one('SELECT kind,ref FROM selections WHERE actor=9001'),
                         {'kind': 'customer', 'ref': own})
        self.assertIsNone(self.s.draft(9001, own)['preview_revision'])
        with self.assertRaises(RuleError):
            self.s.confirm(9001, own, 1)
        self.drain()
        self.assertEqual(self.s.draft(9001, own)['preview_revision'], 1)
        self.assertEqual(self.s.receipt(9001, order), receipt_before)

    def test_customer_switch_rolls_back_with_unacknowledged_update_on_storage_failure(self):
        app, order, own = self.receipt_with_personal_draft()
        before = self.db.one('SELECT kind,ref FROM selections WHERE actor=9001')
        self.db.execute("CREATE TRIGGER cursor_failure BEFORE INSERT ON processed_updates BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.Error):
            app.process_update(update(10, '/draft', 9001))
        self.assertEqual(self.db.one('SELECT kind,ref FROM selections WHERE actor=9001'), before)
        self.assertEqual(app.offset(), 0)
        self.db.execute('DROP TRIGGER cursor_failure')
        app.process_update(update(10, '/draft', 9001))
        self.assertEqual(self.db.one('SELECT kind,ref FROM selections WHERE actor=9001'),
                         {'kind': 'customer', 'ref': own})
        self.assertEqual(app.offset(), 11)


if __name__ == '__main__':
    unittest.main()
