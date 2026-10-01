-- iPhones registered by the IV Repost app for push alerts (APNs device tokens).
CREATE TABLE devices (
    token       TEXT PRIMARY KEY,
    environment TEXT NOT NULL DEFAULT 'production',   -- sandbox | production
    created_at  TEXT NOT NULL,
    last_seen   TEXT NOT NULL,
    disabled_at TEXT,                                 -- set when Apple says the token is dead
    last_error  TEXT
);
