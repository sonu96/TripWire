-- 023: Materialized view for per-agent metrics aggregation
CREATE MATERIALIZED VIEW IF NOT EXISTS agent_metrics AS
SELECT
    e.owner_address AS agent_address,
    COUNT(DISTINCT ev.id) AS total_events,
    COUNT(DISTINCT ev.id) FILTER (WHERE ev.status = 'finalized') AS finalized_events,
    COUNT(DISTINCT wd.id) FILTER (WHERE wd.status = 'delivered') AS successful_deliveries,
    COUNT(DISTINCT t.id) FILTER (WHERE t.active = true) AS active_triggers
FROM endpoints e
LEFT JOIN events ev ON ev.endpoint_id = e.id
LEFT JOIN webhook_deliveries wd ON wd.endpoint_id = e.id
LEFT JOIN triggers t ON t.endpoint_id = e.id
GROUP BY e.owner_address;

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY
CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_metrics_address
    ON agent_metrics (agent_address);
