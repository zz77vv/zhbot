CREATE TABLE payment_cards(
    payment_id TEXT PRIMARY KEY REFERENCES payments(id),
    user_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL CHECK(message_id>0),
    return_draft TEXT REFERENCES drafts(id)
);
