import os
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from zhbot.store import Store


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.db'
        self.store = Store(self.source, 99)
        self.store.adjust(99, 1, 7, 'test funds', 'backup-seed')

    def tearDown(self):
        self.temp.cleanup()

    def test_existing_destination_and_source_alias_are_preserved(self):
        destination = self.root / 'existing.db'
        destination.write_bytes(b'keep this existing file')
        for target in (destination, self.source):
            before = target.read_bytes()
            with self.assertRaises(FileExistsError):
                self.store.backup(target)
            self.assertEqual(before, target.read_bytes())

    def test_hardlink_destination_is_not_overwritten(self):
        alias = self.root / 'alias.db'
        try:
            os.link(self.source, alias)
        except PermissionError:
            self.skipTest('Hardlink creation is blocked by this sandbox')
        before = self.source.read_bytes()
        with self.assertRaises(FileExistsError):
            self.store.backup(alias)
        self.assertEqual(before, self.source.read_bytes())

    @unittest.skipUnless(os.name == 'posix', 'symlink creation requires privileges on Windows')
    def test_dangling_symlink_destination_is_not_followed(self):
        target = self.root / 'missing.db'
        alias = self.root / 'alias.db'
        alias.symlink_to(target)
        with self.assertRaises(FileExistsError):
            self.store.backup(alias)
        self.assertFalse(target.exists())
        self.assertTrue(alias.is_symlink())

    def test_backup_restores_funds_and_cleans_temporary_file(self):
        target = self.root / 'snapshot.db'
        Store(self.source, 99, readonly=True).backup(target)
        restored = Store(target, 99, readonly=True)
        self.assertEqual((7, 0), restored.balance(1))
        self.assertFalse(list(self.root.glob('.zhbot-backup-*')))
        if os.name == 'posix':
            self.assertEqual(0o600, target.stat().st_mode & 0o777)

    def test_publish_failure_leaves_no_incomplete_destination_or_temp(self):
        target = self.root / 'snapshot.db'
        publisher='os.rename' if os.name=='nt' else 'os.link'
        with patch('zhbot.store.'+publisher, side_effect=OSError('publish failed')):
            with self.assertRaises(OSError):
                self.store.backup(target)
        self.assertFalse(target.exists())
        self.assertFalse(list(self.root.glob('.zhbot-backup-*')))
        self.assertEqual((7, 0), self.store.balance(1))

    def test_concurrent_destination_is_preserved_at_publish(self):
        target = self.root / 'snapshot.db'
        publish = os.rename if os.name == 'nt' else os.link

        def concurrent_publish(source, destination):
            Path(destination).write_bytes(b'concurrent backup')
            return publish(source, destination)

        publisher = 'os.rename' if os.name == 'nt' else 'os.link'
        with patch('zhbot.store.' + publisher, side_effect=concurrent_publish):
            with self.assertRaises(FileExistsError):
                self.store.backup(target)
        self.assertEqual(b'concurrent backup', target.read_bytes())
        self.assertFalse(list(self.root.glob('.zhbot-backup-*')))

    def test_readonly_missing_source_does_not_initialize_database(self):
        missing = self.root / 'missing' / 'source.db'
        with self.assertRaises(FileNotFoundError):
            Store(missing, 99, readonly=True)
        self.assertFalse(missing.parent.exists())

    def test_missing_source_after_store_creation_is_not_recreated(self):
        self.source.unlink()
        destination = self.root / 'snapshot.db'
        with self.assertRaises(sqlite3.OperationalError):
            self.store.backup(destination)
        self.assertFalse(self.source.exists())
        self.assertFalse(destination.exists())
        self.assertFalse(list(self.root.glob('.zhbot-backup-*')))

    def test_readonly_open_does_not_run_pending_migrations(self):
        with closing(sqlite3.connect(self.source)) as db:
            db.execute('DELETE FROM schema_migrations WHERE version > 1')
            db.commit()
        with self.assertRaises(sqlite3.OperationalError):
            Store(self.source, 99)
        # Read-only maintenance must still allow backup before migrations.
        readonly = Store(self.source, 99, readonly=True)
        readonly.backup(self.root / 'before-migrate.db')
        self.assertEqual([1], [r['version'] for r in readonly.rows('SELECT version FROM schema_migrations')])


@unittest.skipUnless(os.name == 'posix', 'CLI uses Linux flock')
class MaintenanceCLITests(unittest.TestCase):
    def test_missing_source_is_rejected_without_initializing_directory(self):
        from zhbot.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'missing' / 'source.db'
            for command in ('check', 'backup'):
                with patch.dict(os.environ, {'DATABASE_PATH': str(path), 'ADMIN_TELEGRAM_ID': '99'}):
                    with patch('sys.argv', ['zhbot', command]):
                        with self.assertRaises(SystemExit):
                            main()
                self.assertFalse(path.parent.exists())

    def test_check_returns_failure_for_reported_corruption(self):
        from zhbot.__main__ import main

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'source.db'
            Store(path, 99)
            with patch.dict(os.environ, {'DATABASE_PATH': str(path), 'ADMIN_TELEGRAM_ID': '99'}):
                with patch('sys.argv', ['zhbot', 'check']):
                    with patch.object(Store, 'rows', return_value=[{'integrity_check': 'damaged'}]):
                        with self.assertRaises(SystemExit) as error:
                            main()
            self.assertNotEqual(0, error.exception.code)
