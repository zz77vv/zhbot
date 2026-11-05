"""SQLite persistence. No network calls happen while a transaction is held."""
from contextlib import contextmanager
import json
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
CREATE TABLE IF NOT EXISTS payment_identity_claims (
 kind TEXT NOT NULL CHECK(kind IN ('platform','transaction')), value TEXT NOT NULL,
 payment_id TEXT NOT NULL REFERENCES payments(id),
 PRIMARY KEY(kind,value), UNIQUE(payment_id,kind)
);
CREATE TABLE IF NOT EXISTS payment_observations (
 id INTEGER PRIMARY KEY, job_id INTEGER NOT NULL UNIQUE REFERENCES payment_jobs(id),
 payment_id TEXT NOT NULL REFERENCES payments(id), observed_at INTEGER NOT NULL,
 evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS payment_identity_sightings (
 id INTEGER PRIMARY KEY, observation_id INTEGER NOT NULL REFERENCES payment_observations(id),
 payment_id TEXT NOT NULL REFERENCES payments(id), kind TEXT NOT NULL CHECK(kind IN ('platform','transaction')),
 value TEXT NOT NULL, observed_at INTEGER NOT NULL, evidence TEXT NOT NULL,
 quarantined INTEGER NOT NULL DEFAULT 0 CHECK(quarantined IN (0,1)),
 UNIQUE(observation_id,kind,value)
);
CREATE INDEX IF NOT EXISTS identity_sighting_value ON payment_identity_sightings(kind,value);
CREATE INDEX IF NOT EXISTS identity_sighting_payment ON payment_identity_sightings(payment_id,kind);
"""


def identity_conflicts(connection, payment=None):
    """Find contradictory durable facts; caller must hold a write transaction."""
    predicate = """s.quarantined=1
        OR EXISTS (SELECT 1 FROM payment_identity_sightings x WHERE x.kind=s.kind AND x.value=s.value AND x.payment_id!=s.payment_id)
        OR EXISTS (SELECT 1 FROM payment_identity_sightings x WHERE x.payment_id=s.payment_id AND x.kind=s.kind AND x.value!=s.value)
        OR EXISTS (SELECT 1 FROM payment_identity_claims c WHERE c.kind=s.kind AND
            ((c.value=s.value AND c.payment_id!=s.payment_id) OR (c.payment_id=s.payment_id AND c.value!=s.value)))
        OR (s.kind='platform' AND EXISTS (SELECT 1 FROM payments p WHERE
            (p.platform_id=s.value AND p.id!=s.payment_id) OR (p.id=s.payment_id AND p.platform_id IS NOT NULL AND p.platform_id!=s.value)))
        OR (s.kind='transaction' AND EXISTS (SELECT 1 FROM payment_transactions t WHERE
            (t.transaction_id=s.value AND t.payment_id!=s.payment_id) OR (t.payment_id=s.payment_id AND t.transaction_id!=s.value)))"""
    sql = 'SELECT DISTINCT s.payment_id FROM payment_identity_sightings s WHERE '
    args = ()
    if payment is not None:
        sql += 's.payment_id=? AND '
        args = (payment,)
    return [row[0] for row in connection.execute(sql + '(' + predicate + ')', args)]


def quarantine_identities(connection, payment):
    # Monotonic quarantine: never discard earlier sightings or overwrite ownership.
    return connection.execute("""UPDATE payment_identity_sightings SET quarantined=1
        WHERE quarantined=0 AND EXISTS (SELECT 1 FROM payment_identity_sightings source
            WHERE source.payment_id=? AND source.kind=payment_identity_sightings.kind
            AND source.value=payment_identity_sightings.value)""", (payment,)).rowcount


class Store:
    APPLICATION_ID = 0x5A484231  # ZHB1; independent of any unavailable snapshot schema.
    SCHEMA_VERSION = 4

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
            if has_tables and version < 4:
                self._migrate_identity_sightings()
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
        if ((has_tables and (application_id != self.APPLICATION_ID or version not in {1, 2, 3, self.SCHEMA_VERSION}))
                or (not has_tables and (application_id not in {0, self.APPLICATION_ID} or version not in {0, self.SCHEMA_VERSION}))):
            raise RuntimeError('未知或不兼容的数据库；需单独审查迁移，禁止直接复用。')
        return version, has_tables

    def _migrate_identity_sightings(self):
        # V3 stored internal attestation facts, but not every rejected identity.
        # Conservatively retain verified/local-ID facts for quarantine, never
        # turn legacy JSON into a successful claim or credit.
        for row in self.connection.execute('SELECT * FROM payment_observations').fetchall():
            try:
                data = json.loads(row['evidence'])
            except (ValueError, TypeError):
                continue
            if (type(data) is not dict or data.get('verified') is not True
                    or data.get('unique_id') != row['payment_id']
                    or type(data.get('merchant')) is not str or not data['merchant']):
                continue
            values = [('platform', data.get('platform_id'))]
            if type(data.get('status')) is int and data['status'] == 1:
                values.append(('transaction', data.get('transaction_id')))
            for kind, value in values:
                if type(value) is str and 0 < len(value) <= 128 and value.strip():
                    self.connection.execute('INSERT OR IGNORE INTO payment_identity_sightings(observation_id,payment_id,kind,value,observed_at,evidence) VALUES (?,?,?,?,?,?)',
                                            (row['id'], row['payment_id'], kind, value, row['observed_at'], row['evidence']))
        for payment in identity_conflicts(self.connection):
            if quarantine_identities(self.connection, payment):
                observed = self.connection.execute('SELECT MAX(observed_at) FROM payment_identity_sightings WHERE payment_id=?', (payment,)).fetchone()[0]
                self.connection.execute('INSERT INTO audit(at,actor,action,ref,reason) VALUES (?,0,?,?,?)',
                                        (observed, 'payment_identity_migration_quarantine', payment, '旧观察身份矛盾，升级时隔离；不更改既有账本'))

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
