-- Migration 025: Align trigger tables with skill spec (Gaps #7, #8, #9, #11)
-- Adds version tracking, lifecycle state, and agent class constraints.

-- =============================================================================
-- GAP #8 + #9: trigger_instances needs version and status
-- The boolean `active` column is insufficient for pause/resume workflows.
-- A proper status enum lets agents pause triggers without deleting them.
-- =============================================================================

ALTER TABLE trigger_instances
    ADD COLUMN IF NOT EXISTS version VARCHAR(20) DEFAULT '1.0.0',
    ADD COLUMN IF NOT EXISTS status VARCHAR(20) DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'deleted'));

-- Migrate existing rows: active=true → 'active', active=false → 'deleted'
UPDATE trigger_instances SET status = CASE WHEN active THEN 'active' ELSE 'deleted' END
    WHERE status IS NULL;

-- Index on status for efficient filtering (replaces the boolean active index over time)
CREATE INDEX IF NOT EXISTS idx_trigger_instances_status
    ON trigger_instances (status) WHERE status = 'active';

-- =============================================================================
-- GAP #11: trigger_templates needs lifecycle state and version
-- The boolean `is_public` cannot express draft → active → deprecated → archived.
-- A lifecycle column lets template authors manage rollout and sunset properly.
-- =============================================================================

ALTER TABLE trigger_templates
    ADD COLUMN IF NOT EXISTS version VARCHAR(20) DEFAULT '1.0.0',
    ADD COLUMN IF NOT EXISTS lifecycle VARCHAR(20) DEFAULT 'active'
        CHECK (lifecycle IN ('draft', 'active', 'deprecated', 'archived'));

-- Migrate existing rows: is_public=true → 'active', is_public=false → 'draft'
UPDATE trigger_templates SET lifecycle = CASE WHEN is_public THEN 'active' ELSE 'draft' END
    WHERE lifecycle IS NULL;

-- Index on lifecycle for marketplace queries
CREATE INDEX IF NOT EXISTS idx_trigger_templates_lifecycle
    ON trigger_templates (lifecycle) WHERE lifecycle = 'active';

-- =============================================================================
-- GAP #7: triggers needs required_agent_class and version
-- required_agent_class lets trigger owners restrict which ERC-8004 agent classes
-- can fire the trigger (e.g., only 'payment-processor' agents).
-- =============================================================================

ALTER TABLE triggers
    ADD COLUMN IF NOT EXISTS required_agent_class TEXT,
    ADD COLUMN IF NOT EXISTS version VARCHAR(20) DEFAULT '1.0.0';

-- Sparse index: only index rows that have an agent class restriction
CREATE INDEX IF NOT EXISTS idx_triggers_agent_class
    ON triggers (required_agent_class) WHERE required_agent_class IS NOT NULL;
