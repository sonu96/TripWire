-- Add endpoint_id column to events table so events can be linked
-- to the endpoint that matched the transfer.

ALTER TABLE events ADD COLUMN IF NOT EXISTS endpoint_id TEXT REFERENCES endpoints(id);

CREATE INDEX IF NOT EXISTS idx_events_endpoint_id ON events (endpoint_id);
