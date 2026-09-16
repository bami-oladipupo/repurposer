-- One row per TikTok video ever seen. Platform columns are prefixed yt_ / ig_.
CREATE TABLE IF NOT EXISTS videos (
    tiktok_id        TEXT PRIMARY KEY,
    tiktok_url       TEXT,
    tiktok_caption   TEXT,
    thumbnail_url    TEXT,
    published_at     TEXT,
    local_path       TEXT,
    duration_s       REAL,
    width            INTEGER,
    height           INTEGER,
    origin           TEXT NOT NULL DEFAULT 'new',        -- new | existing
    status           TEXT NOT NULL DEFAULT 'new',        -- new | downloaded | ready | held | skipped | done | failed
    status_reason    TEXT,
    yt_status        TEXT NOT NULL DEFAULT 'queued',     -- queued | scheduled | uploaded | failed | skipped | cancelled
    yt_scheduled_for TEXT,
    yt_video_id      TEXT,
    yt_error         TEXT,
    yt_attempts      INTEGER NOT NULL DEFAULT 0,
    yt_title         TEXT,                               -- per-video override, null = template
    yt_description   TEXT,
    yt_published_at  TEXT,
    ig_status        TEXT NOT NULL DEFAULT 'queued',
    ig_scheduled_for TEXT,
    ig_container_id  TEXT,
    ig_media_id      TEXT,
    ig_error         TEXT,
    ig_attempts      INTEGER NOT NULL DEFAULT 0,
    ig_caption       TEXT,
    ig_published_at  TEXT,
    attempts         INTEGER NOT NULL DEFAULT 0,
    first_seen       TEXT NOT NULL,
    last_attempt     TEXT
);
CREATE INDEX IF NOT EXISTS idx_videos_status ON videos(status);
CREATE INDEX IF NOT EXISTS idx_videos_yt ON videos(yt_status, yt_scheduled_for);
CREATE INDEX IF NOT EXISTS idx_videos_ig ON videos(ig_status, ig_scheduled_for);
CREATE INDEX IF NOT EXISTS idx_videos_published ON videos(published_at);

-- One row per worker execution.
CREATE TABLE IF NOT EXISTS runs (
    run_id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started          TEXT NOT NULL,
    finished         TEXT,
    kind             TEXT NOT NULL DEFAULT 'cycle',      -- cycle | dry-run | publish-now | republish | import
    videos_seen      INTEGER NOT NULL DEFAULT 0,
    videos_published INTEGER NOT NULL DEFAULT 0,
    failures         INTEGER NOT NULL DEFAULT 0,
    ok               INTEGER,
    log_path         TEXT,
    summary          TEXT                                -- JSON: stages, errors, published links
);

-- One row per connected platform. Drives the Connections page and the red banner.
CREATE TABLE IF NOT EXISTS connections (
    platform         TEXT PRIMARY KEY,
    account_name     TEXT,
    account_id       TEXT,
    token_expires_at TEXT,
    last_checked     TEXT,
    healthy          INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT
);

-- Auto publish schedule. Up to five slots per platform per weekday, at least two hours apart.
CREATE TABLE IF NOT EXISTS slots (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    platform         TEXT NOT NULL,
    weekday          INTEGER NOT NULL,                   -- 0 = Monday .. 6 = Sunday
    local_time       TEXT NOT NULL,                      -- HH:MM in publish.timezone
    UNIQUE(platform, weekday, local_time)
);

-- One row per workflow (platform). Seeded from config.yaml on first run, edited in the UI after.
CREATE TABLE IF NOT EXISTS workflows (
    platform            TEXT PRIMARY KEY,
    enabled             INTEGER NOT NULL DEFAULT 1,
    auto_publish        INTEGER NOT NULL DEFAULT 1,      -- 0 = Manual (nothing publishes), 1 = Auto
    mode                TEXT NOT NULL DEFAULT 'schedule',-- asap | schedule
    content_scope       TEXT NOT NULL DEFAULT 'new',     -- new | new_and_existing
    existing_start_from TEXT,                            -- ISO date, local
    existing_order      TEXT NOT NULL DEFAULT 'newest_first',
    existing_include_before TEXT,
    delay_minutes       INTEGER NOT NULL DEFAULT 0,      -- stagger after min_age
    title_template      TEXT,
    caption_template    TEXT,
    hashtags            TEXT NOT NULL DEFAULT '[]',      -- JSON list
    exclude_keywords    TEXT NOT NULL DEFAULT '[]',      -- JSON list
    extra               TEXT NOT NULL DEFAULT '{}',      -- JSON: privacy, category_id, made_for_kids, share_to_feed
    updated_at          TEXT
);

-- Global settings that the UI may edit. Seeded from config.yaml.
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- Immediate jobs raised by the UI (Publish Now, Run Now). The worker drains them.
CREATE TABLE IF NOT EXISTS jobs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    kind       TEXT NOT NULL,                            -- publish_now | run_now | import
    tiktok_id  TEXT,
    platform   TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    ok         INTEGER,
    error      TEXT
);
