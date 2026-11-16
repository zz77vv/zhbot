ALTER TABLE products ADD COLUMN price_profile TEXT
    CHECK (price_profile IN ('owner', 'secondary'));

CREATE TABLE order_quotes (
    id TEXT PRIMARY KEY,
    order_id TEXT NOT NULL REFERENCES orders(id),
    price INTEGER NOT NULL CHECK (price > 0),
    tier TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending','accepted','declined','expired','replaced')),
    expires_at TEXT NOT NULL
);
CREATE UNIQUE INDEX order_quotes_open ON order_quotes(order_id) WHERE state='pending';
CREATE INDEX order_quotes_expiration ON order_quotes(expires_at) WHERE state='pending';
