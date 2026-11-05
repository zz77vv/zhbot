import tempfile
import threading
import unittest
from pathlib import Path

from zhbot.core import RuleError, parse_uint
from zhbot.store import Store
from zhbot.service import Service


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / 'bot.sqlite3')
        self.db = Store(self.path)
        self.s = Service(self.db, {9001}, clock=lambda: 1000)
        self.s.register(101)
        self.s.register(102)
        self.db.execute('UPDATE users SET available=10 WHERE tg_id=101')
        self.project = self.s.save_project(9001, None, '测试项目', 6, True)

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def draft(self):
        draft = self.s.new_draft(101, self.project)
        self.s.add_customer_item(101, 'text', '模拟材料')
        return draft

    def preview(self, draft):
        batch = self.s.preview_customer(101, draft)
        while (msg := self.s.claim_message()) is not None:
            self.s.sent(msg['id'], 500 + msg['id'])
        return self.s.draft(101, draft)['revision']

    def test_ascii_bounded_integer_validation(self):
        for value in ['²', '１２', '١٢', '1'*5000, '-1', '+1', '1.0', ' 1', '', True, 1.0]:
            with self.subTest(value=str(value)[:20]), self.assertRaises(RuleError):
                parse_uint(value, maximum=100)
        self.assertEqual(parse_uint('1', maximum=100), 1)
        self.assertEqual(parse_uint(100, maximum=100), 100)
        with self.assertRaises(RuleError):
            parse_uint('101', maximum=100)

    def test_confirmation_freezes_once_only_after_preview(self):
        draft = self.draft()
        with self.assertRaises(RuleError):
            self.s.confirm(101, draft, 1)
        revision = self.preview(draft)
        order = self.s.confirm(101, draft, revision)
        self.assertEqual(self.s.confirm(101, draft, revision), order)
        self.assertEqual(self.s.balance(101), (4, 6))
        self.assertEqual(len(self.db.all('SELECT * FROM ledger')), 1)
        self.assertEqual(self.s.order(101, order)['status'], 'submitted')

    def test_draft_and_media_survive_restart(self):
        draft = self.draft()
        self.s.add_customer_item(101, 'photo', 'fake-photo-id', '模拟图片')
        self.s.add_customer_item(101, 'document', 'fake-doc-id', '模拟文件')
        other = Store(self.path)
        try:
            restored = Service(other, {9001})
            items = restored.draft(101, draft)['items']
            self.assertEqual([x['kind'] for x in items], ['text', 'photo', 'document'])
            self.assertEqual(restored.balance(101), (10, 0))
        finally:
            other.close()

    def test_changed_project_requires_new_confirmation(self):
        draft = self.draft()
        revision = self.preview(draft)
        self.s.save_project(9001, self.project, '新价格', 5, True)
        with self.assertRaises(RuleError):
            self.s.confirm(101, draft, revision)
        self.assertEqual(self.s.balance(101), (10, 0))

    def test_offline_project_and_insufficient_funds(self):
        self.s.save_project(9001, self.project, '测试项目', 11, True)
        draft = self.draft()
        revision = self.preview(draft)
        with self.assertRaises(RuleError):
            self.s.confirm(101, draft, revision)
        self.s.save_project(9001, self.project, '测试项目', 6, False)
        with self.assertRaises(RuleError):
            self.s.new_draft(102, self.project)

    def test_fail_releases_once_and_is_admin_only(self):
        draft = self.draft()
        order = self.s.confirm(101, draft, self.preview(draft))
        with self.assertRaises(RuleError):
            self.s.fail_order(102, order, '拒绝越权')
        self.s.fail_order(9001, order, '模拟失败')
        self.s.fail_order(9001, order, '重复操作')
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM ledger')), 2)

    def test_ownership_and_admin_guards(self):
        draft = self.draft()
        for op in [lambda: self.s.draft(102, draft),
                   lambda: self.s.save_project(102, None, '越权', 1, True),
                   lambda: self.s.new_draft(103, self.project)]:
            with self.assertRaises(RuleError):
                op()

    def test_failure_in_ledger_rolls_back_entire_confirmation(self):
        draft = self.draft()
        revision = self.preview(draft)
        self.db.execute("CREATE TRIGGER ledger_failure BEFORE INSERT ON ledger BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(Exception):
            self.s.confirm(101, draft, revision)
        self.assertEqual(self.s.balance(101), (10, 0))
        self.assertEqual(len(self.db.all('SELECT * FROM orders')), 0)

    def test_two_connections_cannot_double_freeze(self):
        draft = self.draft()
        revision = self.preview(draft)
        barrier = threading.Barrier(2)
        results = []
        def confirm():
            db = Store(self.path)
            try:
                s = Service(db, {9001})
                barrier.wait()
                results.append(s.confirm(101, draft, revision))
            finally:
                db.close()
        threads = [threading.Thread(target=confirm) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 2)
        self.assertEqual(results[0], results[1])
        self.assertEqual(self.s.balance(101), (4, 6))
        self.assertEqual(len(self.db.all('SELECT * FROM ledger')), 1)


if __name__ == '__main__':
    unittest.main()
