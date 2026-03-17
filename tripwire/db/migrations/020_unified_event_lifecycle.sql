-- Migration 020: Unified event lifecycle
-- Fixes: facilitator claims nonce → Goldsky duplicate silently dropped → consumer never gets payment.confirmed
--
-- Adds a `source` column to nonces so we can distinguish facilitator vs goldsky claims.
-- Adds a `record_nonce_or_correlate` function that returns correlation info when a
-- nonce already exists, enabling Goldsky to "promote" a pre_confirmed event instead
-- of rejecting it as a duplicate.

-- 1. Add source column to nonces
ALTER TABLE nonces ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'goldsky';

-- 2. Create the correlation-aware nonce recording function
CREATE OR REPLACE FUNCTION record_nonce_or_correlate(
    p_chain_id   INTEGER,
    p_nonce      TEXT,
    p_authorizer TEXT,
    p_event_id   TEXT DEFAULT NULL,
    p_source     TEXT DEFAULT 'goldsky'
) RETURNS TABLE (
    is_new           BOOLEAN,
    existing_event_id TEXT,
    existing_source   TEXT
) AS $$
DECLARE
    v_existing RECORD;
BEGIN
    -- Try to insert the new nonce
    INSERT INTO nonces (chain_id, nonce, authorizer, event_id, source)
    VALUES (p_chain_id, p_nonce, lower(p_authorizer), p_event_id, p_source)
    ON CONFLICT (chain_id, nonce, authorizer) DO NOTHING;

    IF FOUND THEN
        -- Fresh insert succeeded
        RETURN QUERY SELECT TRUE, NULL::TEXT, NULL::TEXT;
        RETURN;
    END IF;

    -- Conflict: lock the row to prevent TOCTOU race
    SELECT n.event_id, n.source, n.reorged_at
      INTO v_existing
      FROM nonces n
     WHERE n.chain_id   = p_chain_id
       AND n.nonce      = p_nonce
       AND n.authorizer = lower(p_authorizer)
    FOR UPDATE;

    -- If the existing nonce was reorged, reclaim it
    IF v_existing.reorged_at IS NOT NULL THEN
        UPDATE nonces
           SET reorged_at  = NULL,
               event_id    = p_event_id,
               source      = p_source,
               created_at  = now()
         WHERE chain_id   = p_chain_id
           AND nonce      = p_nonce
           AND authorizer = lower(p_authorizer);
        RETURN QUERY SELECT TRUE, NULL::TEXT, NULL::TEXT;
        RETURN;
    END IF;

    -- Active duplicate — return correlation info
    RETURN QUERY SELECT FALSE, v_existing.event_id, v_existing.source;
END;
$$ LANGUAGE plpgsql;
