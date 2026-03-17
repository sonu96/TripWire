-- 022: Add execution_latency_ms to audit_log for performance tracking
ALTER TABLE audit_log ADD COLUMN IF NOT EXISTS execution_latency_ms INTEGER;

-- Index for querying slow operations
CREATE INDEX IF NOT EXISTS idx_audit_log_latency
    ON audit_log (execution_latency_ms)
    WHERE execution_latency_ms IS NOT NULL;
