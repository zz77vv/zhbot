ALTER TABLE products ADD COLUMN tiers TEXT NOT NULL DEFAULT '{}';

UPDATE products SET tiers=CASE price_profile
    WHEN 'owner' THEN '{"新菜":1,"大众名":2,"剩菜":12}'
    WHEN 'secondary' THEN '{"新菜":3,"大众名":4,"剩菜":12}'
    END
WHERE price_profile IS NOT NULL;
