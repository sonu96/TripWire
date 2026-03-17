-- Migration 015: Fix gameable install_count on trigger_templates
-- Fixes issue #14: agent can inflate count by creating/deleting instances

-- Dedup existing duplicates (keep oldest active per agent+template)
UPDATE trigger_instances SET active = FALSE WHERE id NOT IN (
    SELECT DISTINCT ON (template_id, owner_address) id
    FROM trigger_instances WHERE active = TRUE
    ORDER BY template_id, owner_address, created_at ASC
) AND active = TRUE;

-- Unique partial index: one active instance per agent per template
CREATE UNIQUE INDEX IF NOT EXISTS idx_trigger_instances_unique_active
    ON trigger_instances(template_id, owner_address) WHERE active = TRUE;

-- Replace increment-only trigger with balanced increment/decrement
DROP TRIGGER IF EXISTS trg_increment_install_count ON trigger_instances;
DROP FUNCTION IF EXISTS increment_template_install_count();

CREATE OR REPLACE FUNCTION sync_template_install_count() RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.active = TRUE THEN
            UPDATE trigger_templates SET install_count = install_count + 1, updated_at = now()
            WHERE id = NEW.template_id;
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'UPDATE' THEN
        IF OLD.active = TRUE AND NEW.active = FALSE THEN
            UPDATE trigger_templates SET install_count = GREATEST(install_count - 1, 0), updated_at = now()
            WHERE id = NEW.template_id;
        ELSIF OLD.active = FALSE AND NEW.active = TRUE THEN
            UPDATE trigger_templates SET install_count = install_count + 1, updated_at = now()
            WHERE id = NEW.template_id;
        END IF;
        RETURN NEW;
    ELSIF TG_OP = 'DELETE' THEN
        IF OLD.active = TRUE THEN
            UPDATE trigger_templates SET install_count = GREATEST(install_count - 1, 0), updated_at = now()
            WHERE id = OLD.template_id;
        END IF;
        RETURN OLD;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_sync_install_count
    AFTER INSERT OR UPDATE OF active OR DELETE ON trigger_instances
    FOR EACH ROW EXECUTE FUNCTION sync_template_install_count();

-- Recalculate from actual data
UPDATE trigger_templates tt SET install_count = (
    SELECT COUNT(*) FROM trigger_instances ti
    WHERE ti.template_id = tt.id AND ti.active = TRUE
);
