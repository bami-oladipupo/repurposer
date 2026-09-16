-- Cap the back catalogue to the newest N videos per workflow (null = whole catalogue).
ALTER TABLE workflows ADD COLUMN existing_limit INTEGER;
