import contextlib
from io import StringIO
from pathlib import Path
import tempfile
import unittest

from zhbot.__main__ import main, run_bot
from zhbot.demo import run_demo


class Stop:
    def is_set(self):
        return False

    def wait(self, seconds):
        raise KeyboardInterrupt


class CLITests(unittest.TestCase):
    def test_offline_demo_exercises_recharge_to_mixed_delivery(self):
        result = run_demo()
        self.assertEqual(result['implementation'], 'fresh')
        self.assertEqual(result['payment_mode'], 'offline_simulation')
        self.assertEqual(result['order_status'], 'completed')
        self.assertEqual(result['available_points'], 2)
        self.assertEqual(result['frozen_points'], 0)
        self.assertEqual(result['ledger_entries'], 3)
        self.assertEqual(result['receipt_parts'], 5)

    def test_missing_configuration_fails_without_creating_database(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'bot.sqlite3'
            with contextlib.redirect_stderr(StringIO()):
                code = main(['run', '--db', str(path)], environ={})
            self.assertEqual(code, 2)
            self.assertFalse(path.exists())

    def test_active_webhook_is_not_removed_and_runner_refuses(self):
        class ExistingWebhook:
            def identity(self):
                return 12345
            def has_webhook(self):
                return True
            def get_updates(self, offset):
                raise AssertionError('must not poll')
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(RuntimeError):
                run_bot(str(Path(tmp) / 'bot.sqlite3'), {9001}, ExistingWebhook(), stop=Stop(), lock_root=tmp)

    def test_fake_runner_handles_one_batch_and_stops_cleanly(self):
        from tests.test_app import update
        from zhbot.store import Store
        class FakeAPI:
            def identity(self):
                return 12345
            def has_webhook(self):
                return False
            def get_updates(self, offset):
                return [update(1, '/start')]
            def send(self, message):
                return 77
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / 'bot.sqlite3')
            run_bot(path, {9001}, FakeAPI(), stop=Stop(), lock_root=tmp)
            db = Store(path)
            try:
                self.assertEqual(db.one('SELECT value FROM settings WHERE key=?', ('next_offset',))['value'], '2')
                self.assertEqual(db.one('SELECT status FROM messages')['status'], 'sent')
            finally:
                db.close()


if __name__ == '__main__':
    unittest.main()
