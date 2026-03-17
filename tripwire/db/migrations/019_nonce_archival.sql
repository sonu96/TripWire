-- Migration 019: Nonce archival for unbounded table growth
-- Fixes issue #15: nonces table grows forever with no cleanup strategy
-- Depends on migration 018 (reorged_at column)

CREATE TABLE IF NOT EXISTS nonces_archive (
    chain_id    INTEGER NOT NULL,
    nonce       TEXT NOT NULL,
    authorizer  TEXT NOT NULL,
    event_id    TEXT,
    reorged_at  TIMESTAMPTZ,
    created_at  TIMESTAMPTZ NOT NULL,
    archived_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE(chain_id, nonce, authorizer)
);

CREATE INDEX IF NOT EXISTS idx_nonces_archive_created ON nonces_archive(created_at);

-- Moves confirmed, non-reorged nonces older than threshold to archive.
-- Returns the number of rows archived.
CREATE OR REPLACE FUNCTION archive_old_nonces(
    age_threshold INTERVAL DEFAULT '30 days',
    batch_size INTEGER DEFAULT 5000
) RETURNS INTEGER AS $$
DECLARE
    archived_count INTEGER;
BEGIN
    WITH to_archive AS (
        SELECT chain_id, nonce, authorizer, event_id, reorged_at, created_at
        FROM nonces
        WHERE created_at < now() - age_threshold
          AND reorged_at IS NULL
        LIMIT batch_size
        FOR UPDATE SKIP LOCKED
    ),
    inserted AS (
        INSERT INTO nonces_archive (chain_id, nonce, authorizer, event_id, reorged_at, created_at)
        SELECT chain_id, nonce, authorizer, event_id, reorged_at, created_at
        FROM to_archive
        ON CONFLICT (chain_id, nonce, authorizer) DO NOTHING
        RETURNING 1
    ),
    deleted AS (
        DELETE FROM nonces
        WHERE (chain_id, nonce, authorizer) IN (
            SELECT chain_id, nonce, authorizer FROM to_archive
        )
        RETURNING 1
    )
    SELECT COUNT(*) INTO archived_count FROM deleted;

    RETURN archived_count;
END;
$$ LANGUAGE plpgsql;

-- Update the nonce dedup function to also check archive table
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

    -- Also check archive table (nonce was archived but still can't be reused)
    IF EXISTS (
        SELECT 1 FROM nonces_archive
        WHERE chain_id = p_chain_id
          AND nonce = p_nonce
          AND authorizer = lower(p_authorizer)
    ) THEN
        RETURN FALSE;
    END IF;

    RETURN FALSE;
END;
$$ LANGUAGE plpgsql;
