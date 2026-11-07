CREATE TABLE inbox_archive(
    id INTEGER PRIMARY KEY,
    update_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    created_at TEXT NOT NULL
);
