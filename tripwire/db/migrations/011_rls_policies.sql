-- 011: Row Level Security policies for multi-tenant wallet isolation
--
-- Uses the session variable `app.current_wallet` (set by the application layer
-- via SET LOCAL before each request) to restrict row access by owner_address.
--
-- Child tables (subscriptions, events, webhook_deliveries) join through
-- endpoints.owner_address for ownership checks.

-- ── Helper function for setting wallet context via RPC ───────
-- Called from the application layer: sb.rpc("set_wallet_context", {...})

CREATE OR REPLACE FUNCTION set_wallet_context(wallet_address text)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
AS $$
BEGIN
    PERFORM set_config('app.current_wallet', wallet_address, true);
END;
$$;

-- ── Enable RLS ───────────────────────────────────────────────

ALTER TABLE endpoints ENABLE ROW LEVEL SECURITY;
ALTER TABLE endpoints FORCE ROW LEVEL SECURITY;

ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
ALTER TABLE subscriptions FORCE ROW LEVEL SECURITY;

ALTER TABLE events ENABLE ROW LEVEL SECURITY;
ALTER TABLE events FORCE ROW LEVEL SECURITY;

ALTER TABLE webhook_deliveries ENABLE ROW LEVEL SECURITY;
ALTER TABLE webhook_deliveries FORCE ROW LEVEL SECURITY;

-- ── Endpoints: direct owner_address match ────────────────────

CREATE POLICY endpoints_wallet_isolation ON endpoints
    USING (
        lower(owner_address) = lower(current_setting('app.current_wallet', true))
    );

-- ── Subscriptions: join through endpoints ────────────────────

CREATE POLICY subscriptions_wallet_isolation ON subscriptions
    USING (
        EXISTS (
            SELECT 1 FROM endpoints e
            WHERE e.id = subscriptions.endpoint_id
              AND lower(e.owner_address) = lower(current_setting('app.current_wallet', true))
        )
    );

-- ── Events: join through endpoints ───────────────────────────

CREATE POLICY events_wallet_isolation ON events
    USING (
        EXISTS (
            SELECT 1 FROM endpoints e
            WHERE e.id = events.endpoint_id
              AND lower(e.owner_address) = lower(current_setting('app.current_wallet', true))
        )
    );

-- ── Webhook Deliveries: join through endpoints ───────────────

CREATE POLICY deliveries_wallet_isolation ON webhook_deliveries
    USING (
        EXISTS (
            SELECT 1 FROM endpoints e
            WHERE e.id = webhook_deliveries.endpoint_id
              AND lower(e.owner_address) = lower(current_setting('app.current_wallet', true))
        )
    );
