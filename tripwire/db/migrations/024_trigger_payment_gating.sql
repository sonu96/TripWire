-- Migration 024: Add payment gating columns to triggers table (Phase C3)
--
-- Allows triggers to require that a decoded event contains payment data
-- meeting a minimum threshold before dispatching.

ALTER TABLE triggers
    ADD COLUMN IF NOT EXISTS require_payment   BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS payment_token     TEXT,
    ADD COLUMN IF NOT EXISTS min_payment_amount TEXT;

COMMENT ON COLUMN triggers.require_payment IS 'C3: Gate dispatch on payment metadata in decoded event';
COMMENT ON COLUMN triggers.payment_token IS 'C3: Required token contract address (NULL = any token)';
COMMENT ON COLUMN triggers.min_payment_amount IS 'C3: Minimum payment amount in smallest unit (e.g. 1000000 = 1 USDC)';
