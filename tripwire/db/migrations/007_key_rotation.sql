-- TripWire: Add columns for API key rotation with grace period.
-- old_api_key_hash stores the previous key hash so both keys work during rotation.
-- key_rotated_at tracks when the last rotation happened (grace period expires after 24h).

ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS old_api_key_hash TEXT;
ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS key_rotated_at TIMESTAMPTZ;
