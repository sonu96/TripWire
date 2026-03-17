-- Migration 017: Add precomputed topic0 (keccak256 hash) to triggers and templates
-- Fixes issue #9: topic0 key mismatch — templates store human-readable signatures
-- but _detect_event_type passes keccak256 hashes, so they never match.
-- PostgreSQL has no native keccak256; computed at application insert time.

ALTER TABLE triggers ADD COLUMN IF NOT EXISTS topic0 TEXT;
ALTER TABLE trigger_templates ADD COLUMN IF NOT EXISTS topic0 TEXT;

CREATE INDEX IF NOT EXISTS idx_triggers_topic0_active
    ON triggers(topic0, active) WHERE active = TRUE;
