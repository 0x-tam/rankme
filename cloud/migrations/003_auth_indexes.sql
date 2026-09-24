CREATE INDEX IF NOT EXISTS challenges_expiry_idx ON rankme_cloud.challenges(expires_at);
CREATE INDEX IF NOT EXISTS rate_limits_window_idx ON rankme_cloud.rate_limits(window_start);
