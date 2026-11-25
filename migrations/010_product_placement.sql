ALTER TABLE products ADD COLUMN placement TEXT NOT NULL DEFAULT 'home'
    CHECK (placement IN ('home', 'region'));
