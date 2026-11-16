ALTER TABLE drafts ADD COLUMN updated_at TEXT;
UPDATE drafts SET updated_at=CURRENT_TIMESTAMP;
ALTER TABLE sessions ADD COLUMN updated_at TEXT;
UPDATE sessions SET updated_at=CURRENT_TIMESTAMP;
ALTER TABLE orders ADD COLUMN finished_at TEXT;
ALTER TABLE orders ADD COLUMN data_cleared_at TEXT;
UPDATE orders SET finished_at=CURRENT_TIMESTAMP WHERE status IN ('completed','empty','cancel');
ALTER TABLE events ADD COLUMN finished_at TEXT;
ALTER TABLE events ADD COLUMN data_cleared_at TEXT;
UPDATE events SET finished_at=CURRENT_TIMESTAMP WHERE state IN ('sent','cancelled');

CREATE TRIGGER drafts_created AFTER INSERT ON drafts BEGIN
    UPDATE drafts SET updated_at=CURRENT_TIMESTAMP WHERE id=NEW.id;
END;
CREATE TRIGGER drafts_touched AFTER UPDATE OF data,status,edit_version ON drafts BEGIN
    UPDATE drafts SET updated_at=CURRENT_TIMESTAMP WHERE id=NEW.id;
END;
CREATE TRIGGER sessions_created AFTER INSERT ON sessions BEGIN
    UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE user_id=NEW.user_id;
END;
CREATE TRIGGER sessions_touched AFTER UPDATE OF data ON sessions BEGIN
    UPDATE sessions SET updated_at=CURRENT_TIMESTAMP WHERE user_id=NEW.user_id;
END;
CREATE TRIGGER orders_finished AFTER UPDATE OF status ON orders
WHEN NEW.status IN ('completed','empty','cancel') AND NEW.status!=OLD.status BEGIN
    UPDATE orders SET finished_at=CURRENT_TIMESTAMP WHERE id=NEW.id;
END;
CREATE TRIGGER events_finished AFTER UPDATE OF state ON events
WHEN NEW.state IN ('sent','cancelled') AND NEW.state!=OLD.state BEGIN
    UPDATE events SET finished_at=CURRENT_TIMESTAMP WHERE id=NEW.id;
END;
CREATE INDEX retention_drafts ON drafts(status,updated_at);
CREATE INDEX retention_orders ON orders(status,finished_at) WHERE data_cleared_at IS NULL;
CREATE INDEX retention_events ON events(state,finished_at) WHERE data_cleared_at IS NULL;
CREATE INDEX retention_inbox ON inbox(state,created_at);
