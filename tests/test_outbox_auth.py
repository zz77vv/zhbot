import sqlite3
import unittest
from pathlib import Path

from tests import test_app
from zhbot.app import Application
from zhbot.core import RuleError
from zhbot.service import Service
from zhbot.store import Store


class OutboxAuthorizationTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown
    app = test_app.AppTests.app
    drain = test_app.AppTests.drain
    receipt_with_personal_draft = test_app.AppTests.receipt_with_personal_draft

    def test_queued_admin_order_details_are_cancelled_after_revocation_and_restart(self):
        app, order, _ = self.receipt_with_personal_draft()
        app.process_update(test_app.update(10, f'/order {order}', 9001))
        batch = self.db.one('SELECT MAX(id) AS id FROM batches')['id']
        other = Store(self.path)
        try:
            service = Service(other, {9002})
            service.recover()
            self.assertIsNone(service.claim_message())
            self.assertEqual(other.one('SELECT status FROM batches WHERE id=?', (batch,))['status'], 'cancelled')
            self.assertTrue(all(m['status'] == 'cancelled' for m in other.all('SELECT status FROM messages WHERE batch_id=?', (batch,))))
            self.assertEqual(service.balance(101), (4, 6))
            with self.assertRaises(RuleError):
                service.order(9001, order)
        finally:
            other.close()

    def test_partial_receipt_preview_stops_on_revocation_without_unlocking_delivery(self):
        _, order, _ = self.receipt_with_personal_draft()
        self.s.add_receipt_item(9001, 'photo', 'fake-private-photo')
        self.s.add_receipt_item(9001, 'document', 'fake-private-document')
        batch = self.s.preview_receipt(9001, order)
        first = self.s.claim_message()
        self.s.sent(first['id'], 123)
        self.s.admins = frozenset({9002})
        self.assertIsNone(self.s.claim_message())
        self.assertEqual(self.db.one('SELECT status FROM batches WHERE id=?', (batch,))['status'], 'cancelled')
        self.assertEqual(self.db.one('SELECT status FROM messages WHERE id=?', (first['id'],))['status'], 'sent')
        row = self.s.receipt(9002, order)
        self.assertIsNone(row['preview_revision'])
        self.assertIsNone(row['preview_actor'])
        self.assertEqual(self.s.balance(101), (4, 6))
        with self.assertRaises(RuleError):
            self.s.deliver(9002, order, row['revision'])

    def test_current_admin_can_still_receive_details_and_full_receipt_preview(self):
        app, order, _ = self.receipt_with_personal_draft()
        app.process_update(test_app.update(10, f'/order {order}', 9001))
        self.s.add_receipt_item(9001, 'photo', 'fake-private-photo')
        self.s.preview_receipt(9001, order)
        messages = self.drain()
        self.assertTrue(any(m['content'] == '客户模拟材料' for m in messages))
        self.assertTrue(any(m['content'] == 'fake-private-photo' for m in messages))
        self.assertEqual(self.s.receipt(9001, order)['preview_revision'], 2)
        self.assertEqual(self.s.balance(101), (4, 6))

    def test_confirmed_customer_delivery_continues_after_admin_revocation(self):
        _, order, _ = self.receipt_with_personal_draft()
        batch = self.s.deliver(9001, order, 1)
        other = Store(self.path)
        try:
            service = Service(other, {9002})
            service.recover()
            delivery_messages = []
            while (message := service.claim_message()) is not None:
                self.assertNotEqual(message['chat_id'], 9001)
                if message['batch_kind'] == 'delivery':
                    delivery_messages.append(message)
                    self.assertEqual(message['chat_id'], 101)
                service.sent(message['id'], 1000 + message['id'])
            self.assertEqual(len(delivery_messages), 1)
            self.assertEqual(other.one('SELECT status FROM batches WHERE id=?', (batch,))['status'], 'sent')
            self.assertEqual(service.order(101, order)['status'], 'completed')
            self.assertEqual(service.balance(101), (4, 0))
            self.assertEqual(len(other.all("SELECT id FROM ledger WHERE entry_key LIKE 'charge:%'")), 1)
        finally:
            other.close()

    def test_privileged_lists_stop_but_personal_balance_survives(self):
        app, order, _ = self.receipt_with_personal_draft()
        self.s.save_project(9001, None, '模拟下架项目', 1, False)
        app.process_update(test_app.update(10, '/adminprojects', 9001))
        app.process_update(test_app.update(11, '/orders', 9001))
        app.process_update(test_app.update(12, '/review', 9001))
        app.process_update(test_app.update(13, '/balance', 9001))
        self.s.admins = frozenset({9002})
        messages = self.drain()
        self.assertEqual(len(messages), 1)
        self.assertIn('可用 0', messages[0]['content'])
        self.assertEqual(messages[0]['chat_id'], 9001)
        self.assertEqual(self.s.balance(101), (4, 6))

    def test_new_order_notification_is_cancelled_after_admin_revocation(self):
        _, _, _ = self.receipt_with_personal_draft()
        project = self.s.save_project(9001, None, '另一个模拟项目', 1, True)
        draft = self.s.new_draft(101, project)
        self.s.add_customer_item(101, 'text', '另一订单的模拟材料')
        self.s.preview_customer(101, draft)
        self.drain()
        self.s.confirm(101, draft, 1)
        notification = self.db.one('SELECT MAX(id) AS id FROM batches')['id']
        self.s.admins = frozenset({9002})
        self.assertIsNone(self.s.claim_message())
        self.assertEqual(self.db.one('SELECT status FROM batches WHERE id=?', (notification,))['status'], 'cancelled')
        self.assertEqual(self.s.balance(101), (3, 7))

    def test_inflight_preview_ack_after_revocation_does_not_unlock_delivery(self):
        _, order, _ = self.receipt_with_personal_draft()
        self.s.add_receipt_item(9001, 'text', '新模拟回执项')
        batch = self.s.preview_receipt(9001, order)
        for _ in range(2):
            message = self.s.claim_message()
            self.s.sent(message['id'], 3000 + message['id'])
        last = self.s.claim_message()
        self.s.admins = frozenset({9002})
        self.s.sent(last['id'], 4000 + last['id'])
        self.assertEqual(self.db.one('SELECT status FROM batches WHERE id=?', (batch,))['status'], 'cancelled')
        self.assertIsNone(self.s.receipt(9002, order)['preview_revision'])
        self.assertEqual(self.s.balance(101), (4, 6))

    def test_schema_v1_unknown_note_audience_is_cancelled_but_delivery_is_preserved(self):
        app, order, _ = self.receipt_with_personal_draft()
        app.process_update(test_app.update(10, f'/order {order}', 9001))
        admin_batch = self.db.one('SELECT MAX(id) AS id FROM batches')['id']
        self.s.deliver(9001, order, 1)
        legacy = str(Path(self.tmp.name) / 'legacy-v1.sqlite3')
        connection = sqlite3.connect(legacy)
        self.db.connection.backup(connection)
        if 'audience' in {row[1] for row in connection.execute('PRAGMA table_info(batches)')}:
            connection.execute('ALTER TABLE batches DROP COLUMN audience')
        connection.execute('PRAGMA user_version=1')
        connection.commit()
        connection.close()
        other = Store(legacy)
        try:
            service = Service(other, {9002})
            service.recover()
            while (message := service.claim_message()) is not None:
                self.assertNotEqual(message['chat_id'], 9001)
                service.sent(message['id'], 2000 + message['id'])
            self.assertEqual(other.one('SELECT status FROM batches WHERE id=?', (admin_batch,))['status'], 'cancelled')
            self.assertEqual(service.order(101, order)['status'], 'completed')
            self.assertEqual(service.balance(101), (4, 0))
            self.assertIn('audience', {row[1] for row in other.execute('PRAGMA table_info(batches)')})
        finally:
            other.close()


if __name__ == '__main__':
    unittest.main()
