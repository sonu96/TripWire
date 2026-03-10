-- Realtime events table for Notify mode delivery.
-- Supabase Realtime automatically broadcasts inserts on this table
-- to subscribed clients via WebSocket.

CREATE TABLE IF NOT EXISTS realtime_events (
    id          TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
    type        TEXT NOT NULL,
    data        JSONB NOT NULL DEFAULT '{}',
    chain_id    INTEGER NOT NULL,
    recipient   TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_realtime_events_endpoint ON realtime_events (endpoint_id);
CREATE INDEX IF NOT EXISTS idx_realtime_events_recipient ON realtime_events (recipient);
CREATE INDEX IF NOT EXISTS idx_realtime_events_chain ON realtime_events (chain_id);
CREATE INDEX IF NOT EXISTS idx_realtime_events_created_at ON realtime_events (created_at);

-- Enable Supabase Realtime on this table
ALTER PUBLICATION supabase_realtime ADD TABLE realtime_events;
