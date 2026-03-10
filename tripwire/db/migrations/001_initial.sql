-- TripWire: Initial schema migration
-- Run against the Supabase PostgreSQL database.

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ── Endpoints ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS endpoints (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('notify', 'execute')),
    chains      JSONB NOT NULL DEFAULT '[]',
    recipient   TEXT NOT NULL,
    policies    JSONB NOT NULL DEFAULT '{}',
    api_key_hash TEXT,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_endpoints_recipient ON endpoints (recipient);
CREATE INDEX IF NOT EXISTS idx_endpoints_active ON endpoints (active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_endpoints_mode ON endpoints (mode);

-- ── Subscriptions ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS subscriptions (
    id          TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
    filters     JSONB NOT NULL DEFAULT '{}',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_endpoint_id ON subscriptions (endpoint_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_active ON subscriptions (active) WHERE active = TRUE;

-- ── Events ───────────────────────────────────────────────────────
-- Each ERC-3009 payment emits both a Transfer(from, to, value) event and an
-- AuthorizationUsed(authorizer, nonce) event in the same tx. This table
-- stores the combined data: Transfer fields (from_address, to_address, amount)
-- plus AuthorizationUsed fields (authorizer, nonce as bytes32 hex).
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    chain_id        INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    block_number    BIGINT NOT NULL,
    block_hash      TEXT NOT NULL,
    log_index       INTEGER NOT NULL,
    from_address    TEXT NOT NULL,       -- Transfer: from
    to_address      TEXT NOT NULL,       -- Transfer: to
    amount          TEXT NOT NULL,       -- Transfer: value (string for precision)
    authorizer      TEXT NOT NULL,       -- AuthorizationUsed: authorizer
    nonce           TEXT NOT NULL,       -- AuthorizationUsed: bytes32 nonce
    token           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    finality_depth  INTEGER NOT NULL DEFAULT 0,
    identity_data   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_events_chain_tx ON events (chain_id, tx_hash);
CREATE INDEX IF NOT EXISTS idx_events_to_address ON events (to_address);
CREATE INDEX IF NOT EXISTS idx_events_from_address ON events (from_address);
CREATE INDEX IF NOT EXISTS idx_events_authorizer ON events (authorizer);
CREATE INDEX IF NOT EXISTS idx_events_nonce ON events (chain_id, nonce, authorizer);
CREATE INDEX IF NOT EXISTS idx_events_status ON events (status);
CREATE INDEX IF NOT EXISTS idx_events_block ON events (chain_id, block_number);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at);

-- ── Nonces (deduplication) ───────────────────────────────────────
CREATE TABLE IF NOT EXISTS nonces (
    chain_id    INTEGER NOT NULL,
    nonce       TEXT NOT NULL,
    authorizer  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain_id, nonce, authorizer)
);

CREATE INDEX IF NOT EXISTS idx_nonces_authorizer ON nonces (authorizer);

-- ── Webhook Deliveries ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              TEXT PRIMARY KEY,
    endpoint_id     TEXT NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
    event_id        TEXT NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    svix_message_id TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint ON webhook_deliveries (endpoint_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event ON webhook_deliveries (event_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status ON webhook_deliveries (status);

-- ── Audit Log ────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS audit_log (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log (action);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log (created_at);
