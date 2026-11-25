CREATE TABLE payment_checks(
    id INTEGER PRIMARY KEY,
    payment_id TEXT NOT NULL REFERENCES payments(id),
    result TEXT NOT NULL,
    verification_hash TEXT NOT NULL DEFAULT '',
    platform_id TEXT,
    checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX payment_checks_payment ON payment_checks(payment_id);
