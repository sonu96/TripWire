-- Index on api_key_hash for fast API key lookups during authentication.
-- The column already exists from 001_initial.sql.

CREATE INDEX IF NOT EXISTS idx_endpoints_api_key_hash ON endpoints (api_key_hash);
