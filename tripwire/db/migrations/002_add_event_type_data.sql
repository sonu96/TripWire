-- Add type and data columns to events table for the webhook payload format.
-- The routes and processor use these columns alongside the structured fields.

ALTER TABLE events ADD COLUMN IF NOT EXISTS type TEXT NOT NULL DEFAULT 'payment.confirmed';
ALTER TABLE events ADD COLUMN IF NOT EXISTS data JSONB NOT NULL DEFAULT '{}';

-- Make structured columns nullable since Goldsky-decoded events
-- may not have all fields (e.g., block_hash, log_index).
ALTER TABLE events ALTER COLUMN block_number DROP NOT NULL;
ALTER TABLE events ALTER COLUMN block_hash DROP NOT NULL;
ALTER TABLE events ALTER COLUMN log_index DROP NOT NULL;
ALTER TABLE events ALTER COLUMN from_address DROP NOT NULL;
ALTER TABLE events ALTER COLUMN to_address DROP NOT NULL;
ALTER TABLE events ALTER COLUMN amount DROP NOT NULL;
ALTER TABLE events ALTER COLUMN authorizer DROP NOT NULL;
ALTER TABLE events ALTER COLUMN nonce DROP NOT NULL;
ALTER TABLE events ALTER COLUMN token DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_events_type ON events (type);
