-- Migration 026: Make events table event-neutral for Pulse/Keeper product split
--
-- Phase 2+3: The events table currently stores only ERC-3009 payment data.
-- This migration makes it generic so Pulse dynamic trigger events can also be
-- stored without requiring payment-specific columns.
--
-- Safety: All new columns have defaults, so existing rows are unaffected.
-- Columns that were already made nullable in 002 are NOT re-altered.

-- ── New columns ─────────────────────────────────────────────────────────

-- event_type: semantic event type (e.g. 'erc3009.transfer', 'Transfer', 'Swap')
-- Defaults to 'erc3009.transfer' so all existing payment rows are tagged correctly.
ALTER TABLE events ADD COLUMN IF NOT EXISTS event_type TEXT NOT NULL DEFAULT 'erc3009.transfer';

-- decoded_fields: generic JSONB bag for Pulse dynamic trigger decoded data
-- Pulse triggers decode arbitrary ABI events into key/value pairs stored here.
ALTER TABLE events ADD COLUMN IF NOT EXISTS decoded_fields JSONB DEFAULT '{}';

-- source: tracks where the event originated ('onchain', 'facilitator', 'manual')
ALTER TABLE events ADD COLUMN IF NOT EXISTS source TEXT NOT NULL DEFAULT 'onchain';

-- trigger_id: links Pulse events to the trigger that matched them (nullable —
-- only set for trigger-matched events, NULL for legacy Keeper payment events)
ALTER TABLE events ADD COLUMN IF NOT EXISTS trigger_id TEXT;

-- product_source: operational routing — which product surface created the event
-- 'keeper' for legacy x402 payment events, 'pulse' for dynamic trigger events
ALTER TABLE events ADD COLUMN IF NOT EXISTS product_source TEXT NOT NULL DEFAULT 'keeper';

-- ── Make tx_hash nullable ───────────────────────────────────────────────
-- tx_hash was still NOT NULL from the initial migration (001). Pulse events
-- sourced from off-chain or pre-confirmation paths may not have a tx_hash.
-- The payment-specific columns (amount, authorizer, nonce, token, from_address,
-- to_address, block_number, block_hash, log_index) were already made nullable
-- in migration 002 — no need to alter them again.
ALTER TABLE events ALTER COLUMN tx_hash DROP NOT NULL;

-- ── Indexes ─────────────────────────────────────────────────────────────

-- event_type queries: Pulse filters events by type
CREATE INDEX IF NOT EXISTS idx_events_event_type ON events (event_type);

-- trigger_id lookups: Pulse queries events matched by a specific trigger
-- Partial index — only rows that actually have a trigger_id
CREATE INDEX IF NOT EXISTS idx_events_trigger_id ON events (trigger_id) WHERE trigger_id IS NOT NULL;

-- product_source routing: operational queries to route events to correct product
CREATE INDEX IF NOT EXISTS idx_events_product_source ON events (product_source);

-- Composite index for Pulse listing: event_type + created_at for efficient
-- paginated queries filtered by type
CREATE INDEX IF NOT EXISTS idx_events_event_type_created
    ON events (event_type, created_at DESC);
