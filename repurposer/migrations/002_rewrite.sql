-- Caption rewriting with Claude: one status per video, text lands in the existing per-video columns.
ALTER TABLE videos ADD COLUMN rewrite_status TEXT;      -- null | done | failed | skipped
ALTER TABLE videos ADD COLUMN rewrite_error TEXT;
ALTER TABLE videos ADD COLUMN rewritten_at TEXT;
