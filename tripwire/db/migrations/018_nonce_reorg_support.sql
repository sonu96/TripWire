-- Migration 018: Reorg-aware nonce deduplication
-- Fixes issue #2: reorged nonces are unrecoverable — nonce stuck forever
-- Depends on migration 014 (event_endpoints join table for multi-endpoint dispatch)

ALTER TABLE nonces ADD COLUMN IF NOT EXISTS reorged_at TIMESTAMPTZ;
ALTER TABLE nonces ADD COLUMN IF NOT EXISTS event_id TEXT;

CREATE INDEX IF NOT EXISTS idx_nonces_event_id ON nonces(event_id) WHERE event_id IS NOT NULL;

-- Postgres function for reorg-aware dedup
CREATE OR REPLACE FUNCTION record_nonce_with_reorg(
    p_chain_id INTEGER,
    p_nonce TEXT,
    p_authorizer TEXT,
    p_event_id TEXT DEFAULT NULL
) RETURNS BOOLEAN AS $$
BEGIN
    -- Try to insert new nonce
    INSERT INTO nonces (chain_id, nonce, authorizer, event_id)
    VALUES (p_chain_id, p_nonce, lower(p_authorizer), p_event_id)
    ON CONFLICT (chain_id, nonce, authorizer) DO NOTHING;

    IF FOUND THEN
        RETURN TRUE;
    END IF;

    -- Check if existing was reorged (available for reuse)
    IF EXISTS (
        SELECT 1 FROM nonces
        WHERE chain_id = p_chain_id
          AND nonce = p_nonce
          AND authorizer = lower(p_authorizer)
          AND reorged_at IS NOT NULL
    ) THEN
        UPDATE nonces
        SET reorged_at = NULL, event_id = p_event_id, created_at = now()
        WHERE chain_id = p_chain_id
          AND nonce = p_nonce
          AND authorizer = lower(p_authorizer);
        RETURN TRUE;
    END IF;

    RETURN FALSE;
END;
$$ LANGUAGE plpgsql;
