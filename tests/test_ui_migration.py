from pathlib import Path
import sqlite3
import unittest

from tests import test_app
from zhbot.service import Service
from zhbot.store import Store


class UIMigrationTests(unittest.TestCase):
    setUp = test_app.AppTests.setUp
    tearDown = test_app.AppTests.tearDown

    def test_schema4_upgrade_keeps_balance_ledger_and_existing_outbox(self):
        self.s.note(101, '既有通知')
        path = str(Path(self.tmp.name) / 'schema4.sqlite3')
        raw = sqlite3.connect(path)
        self.db.connection.backup(raw)
        raw.execute('ALTER TABLE messages DROP COLUMN reply_markup')
        for table in ['ui_actions', 'ui_sessions', 'processed_callbacks', 'point_adjustments']:
            raw.execute('DROP TABLE ' + table)
        raw.execute('PRAGMA user_version=4')
        raw.commit(); raw.close()
        other = Store(path)
        try:
            service = Service(other, {9001})
            self.assertEqual(other.one('PRAGMA user_version')['user_version'], 5)
            self.assertEqual(service.balance(101), (10, 0))
            service.adjust_points(9001, 101, 2, '升级后人工调整', 'e'*32)
            self.assertEqual(service.balance(101), (12, 0))
            message = service.claim_message()
            self.assertEqual(message['content'], '既有通知')
            self.assertIsNone(message['reply_markup'])
            self.assertEqual(len(other.all('SELECT * FROM ledger')), 1)
        finally: other.close()
