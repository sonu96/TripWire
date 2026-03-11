-- Migration 009: Add missing composite indexes for common query patterns

-- webhook_deliveries: get_by_endpoint() filters on (endpoint_id, status) + order by created_at DESC
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint_status_created
    ON webhook_deliveries (endpoint_id, status, created_at DESC);

-- realtime_events: filtering by endpoint_id then ordering by created_at DESC
CREATE INDEX IF NOT EXISTS idx_realtime_events_endpoint_created
    ON realtime_events (endpoint_id, created_at DESC);

-- events: time-range queries scoped to an endpoint
CREATE INDEX IF NOT EXISTS idx_events_endpoint_created
    ON events (endpoint_id, created_at DESC)
    WHERE endpoint_id IS NOT NULL;

-- endpoints: list_by_recipient() filters on (recipient, active=TRUE)
CREATE INDEX IF NOT EXISTS idx_endpoints_recipient_active
    ON endpoints (recipient)
    WHERE active = TRUE;

-- subscriptions: fetch active subscriptions by endpoint_id
CREATE INDEX IF NOT EXISTS idx_subscriptions_endpoint_active
    ON subscriptions (endpoint_id)
    WHERE active = TRUE;

-- webhook_deliveries: list_paginated() filters by event_id + orders by created_at DESC
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event_created
    ON webhook_deliveries (event_id, created_at DESC);

-- webhook_deliveries: DLQ handler looks up local delivery by provider_message_id
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_provider_message_id
    ON webhook_deliveries (provider_message_id)
    WHERE provider_message_id IS NOT NULL;

-- webhook_deliveries: list_paginated() with no filters orders by created_at DESC
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_created
    ON webhook_deliveries (created_at DESC);
