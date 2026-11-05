"""SQLite persistence. No network calls happen while a transaction is held."""
from contextlib import contextmanager
import sqlite3


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 tg_id INTEGER PRIMARY KEY CHECK(tg_id>0),
 available INTEGER NOT NULL DEFAULT 0 CHECK(typeof(available)='integer' AND available BETWEEN 0 AND 1000000000000),
 frozen INTEGER NOT NULL DEFAULT 0 CHECK(typeof(frozen)='integer' AND frozen BETWEEN 0 AND 1000000000000)
);
CREATE TABLE IF NOT EXISTS projects (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, price INTEGER NOT NULL CHECK(price BETWEEN 1 AND 1000000),
 active INTEGER NOT NULL CHECK(active IN (0,1)), revision INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS drafts (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(tg_id),
 project_id INTEGER NOT NULL REFERENCES projects(id), project_revision INTEGER NOT NULL,
 title TEXT NOT NULL, price INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 preview_revision INTEGER, state TEXT NOT NULL DEFAULT 'editing', order_id INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_draft ON drafts(user_id) WHERE state='editing';
CREATE TABLE IF NOT EXISTS draft_items (
 id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL REFERENCES drafts(id),
 kind TEXT NOT NULL, content TEXT NOT NULL, caption TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY, draft_id INTEGER NOT NULL UNIQUE REFERENCES drafts(id),
 user_id INTEGER NOT NULL REFERENCES users(tg_id), project_id INTEGER NOT NULL REFERENCES projects(id),
 title TEXT NOT NULL, price INTEGER NOT NULL, status TEXT NOT NULL,
 created_at INTEGER NOT NULL, failure_reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS order_items (
 id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
 kind TEXT NOT NULL, content TEXT NOT NULL, caption TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS receipts (
 order_id INTEGER PRIMARY KEY REFERENCES orders(id), revision INTEGER NOT NULL DEFAULT 0,
 preview_revision INTEGER, preview_actor INTEGER
);
CREATE TABLE IF NOT EXISTS receipt_items (
 id INTEGER PRIMARY KEY, order_id INTEGER NOT NULL REFERENCES orders(id),
 kind TEXT NOT NULL, content TEXT NOT NULL, caption TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS selections (actor INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS batches (
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, ref INTEGER, revision INTEGER,
 actor INTEGER NOT NULL, chat_id INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
 audience TEXT NOT NULL DEFAULT 'legacy' CHECK(audience IN ('customer','admin','legacy'))
);
CREATE TABLE IF NOT EXISTS messages (
 id INTEGER PRIMARY KEY, batch_id INTEGER NOT NULL REFERENCES batches(id), seq INTEGER NOT NULL,
 kind TEXT NOT NULL, content TEXT NOT NULL, caption TEXT NOT NULL DEFAULT '',
 status TEXT NOT NULL DEFAULT 'pending', telegram_id INTEGER, reason TEXT NOT NULL DEFAULT '',
 not_before INTEGER NOT NULL DEFAULT 0,
 UNIQUE(batch_id,seq)
);
CREATE TABLE IF NOT EXISTS ledger (
 id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(tg_id), entry_key TEXT NOT NULL UNIQUE,
 available_delta INTEGER NOT NULL, frozen_delta INTEGER NOT NULL,
 available_after INTEGER NOT NULL, frozen_after INTEGER NOT NULL, at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
 id INTEGER PRIMARY KEY, at INTEGER NOT NULL, actor INTEGER NOT NULL,
 action TEXT NOT NULL, ref TEXT NOT NULL, reason TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS processed_updates (
 id INTEGER PRIMARY KEY, status TEXT NOT NULL, at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS payments (
 id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(tg_id), amount INTEGER NOT NULL,
 status TEXT NOT NULL, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
 platform_id TEXT, pay_url TEXT, reason TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payment_transactions (
 transaction_id TEXT PRIMARY KEY, payment_id TEXT NOT NULL UNIQUE REFERENCES payments(id)
);
CREATE TABLE IF NOT EXISTS payment_jobs (
 id INTEGER PRIMARY KEY, payment_id TEXT NOT NULL REFERENCES payments(id),
 kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_payment_job ON payment_jobs(payment_id)
 WHERE status IN ('pending','sending');
"""


class Store:
    APPLICATION_ID = 0x5A484231  # ZHB1; independent of any unavailable snapshot schema.
    SCHEMA_VERSION = 2

    def __init__(self, path):
        self.connection = sqlite3.connect(path, isolation_level=None, timeout=10)
        self.connection.row_factory = sqlite3.Row
        try:
            self._schema_state()  # Refuse unknown DBs before changing journal mode.
            self.connection.execute('PRAGMA foreign_keys=ON')
            self.connection.execute('PRAGMA journal_mode=WAL')
            self.connection.execute('PRAGMA synchronous=FULL')
            self.connection.execute('BEGIN IMMEDIATE')
            # Re-read under the lock: another opener may already have migrated it.
            version, has_tables = self._schema_state()
            if has_tables and version == 1:
                self.connection.execute("ALTER TABLE batches ADD COLUMN audience TEXT NOT NULL DEFAULT 'legacy' CHECK(audience IN ('customer','admin','legacy'))")
                self.connection.execute("UPDATE batches SET audience='customer' WHERE kind IN ('customer_preview','delivery')")
                self.connection.execute("UPDATE batches SET audience='admin' WHERE kind='receipt_preview'")
                # V1 'note' has no durable ownership/privilege marker. Leave it
                # legacy so Service cancels unsent content instead of guessing.
            # SCHEMA contains simple DDL statements only (no trigger bodies).
            for statement in SCHEMA.split(';'):
                if statement.strip():
                    self.connection.execute(statement)
            self.connection.execute(f'PRAGMA application_id={self.APPLICATION_ID}')
            self.connection.execute(f'PRAGMA user_version={self.SCHEMA_VERSION}')
            self.connection.execute('COMMIT')
        except BaseException:
            self.connection.close()
            raise
        self.depth = 0

    def _schema_state(self):
        application_id = self.connection.execute('PRAGMA application_id').fetchone()[0]
        version = self.connection.execute('PRAGMA user_version').fetchone()[0]
        has_tables = self.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone() is not None
        if ((has_tables and (application_id != self.APPLICATION_ID or version not in {1, self.SCHEMA_VERSION}))
                or (not has_tables and (application_id not in {0, self.APPLICATION_ID} or version not in {0, self.SCHEMA_VERSION}))):
            raise RuntimeError('未知或不兼容的数据库；需单独审查迁移，禁止直接复用。')
        return version, has_tables

    def execute(self, sql, args=()):
        return self.connection.execute(sql, args)

    def one(self, sql, args=()):
        row = self.execute(sql, args).fetchone()
        return dict(row) if row is not None else None

    def all(self, sql, args=()):
        return [dict(row) for row in self.execute(sql, args).fetchall()]

    @contextmanager
    def atomic(self):
        name = f'sp_{self.depth}'
        nested = self.depth > 0
        self.execute(f'SAVEPOINT {name}' if nested else 'BEGIN IMMEDIATE')
        self.depth += 1
        try:
            yield
            self.execute(f'RELEASE {name}' if nested else 'COMMIT')
        except BaseException:
            self.execute(f'ROLLBACK TO {name}' if nested else 'ROLLBACK')
            if nested:
                self.execute(f'RELEASE {name}')
            raise
        finally:
            self.depth -= 1

    def close(self):
        self.connection.close()
