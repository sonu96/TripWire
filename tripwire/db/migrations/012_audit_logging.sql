CREATE TABLE IF NOT EXISTS audit_log (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    action text NOT NULL,
    actor text NOT NULL,
    resource_type text,
    resource_id text,
    details jsonb DEFAULT '{}',
    ip_address text,
    created_at timestamptz DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_audit_log_actor ON audit_log(actor);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at);
