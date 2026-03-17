-- Migration 014: Many-to-many join table for events ↔ endpoints
-- Fixes issue #7: events.endpoint_id only records first match

CREATE TABLE IF NOT EXISTS event_endpoints (
    event_id    TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
    endpoint_id TEXT NOT NULL REFERENCES endpoints(id) ON DELETE CASCADE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, endpoint_id)
);

CREATE INDEX IF NOT EXISTS idx_event_endpoints_endpoint ON event_endpoints(endpoint_id);
CREATE INDEX IF NOT EXISTS idx_event_endpoints_created ON event_endpoints(created_at DESC);

-- Backfill from existing data
INSERT INTO event_endpoints (event_id, endpoint_id, created_at)
SELECT id, endpoint_id, created_at FROM events
WHERE endpoint_id IS NOT NULL
ON CONFLICT DO NOTHING;
