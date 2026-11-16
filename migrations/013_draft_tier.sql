ALTER TABLE drafts ADD COLUMN tier_price INTEGER CHECK (tier_price > 0);
