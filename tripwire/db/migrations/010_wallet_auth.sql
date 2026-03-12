-- Migration 010: Replace API key auth with wallet-based auth
ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS owner_address TEXT;
UPDATE endpoints SET owner_address = recipient WHERE owner_address IS NULL;
ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS registration_tx_hash TEXT;
ALTER TABLE endpoints ADD COLUMN IF NOT EXISTS registration_chain_id INTEGER;
CREATE INDEX IF NOT EXISTS idx_endpoints_owner_address ON endpoints (owner_address);
DROP INDEX IF EXISTS idx_endpoints_api_key_hash;
ALTER TABLE endpoints DROP COLUMN IF EXISTS api_key_hash;
ALTER TABLE endpoints DROP COLUMN IF EXISTS old_api_key_hash;
ALTER TABLE endpoints DROP COLUMN IF EXISTS key_rotated_at;
