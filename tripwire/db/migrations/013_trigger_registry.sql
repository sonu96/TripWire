-- Migration 013: Trigger registry for MCP-driven AI agent triggers

CREATE TABLE IF NOT EXISTS trigger_templates (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name                TEXT NOT NULL,
    slug                TEXT NOT NULL UNIQUE,
    description         TEXT,
    category            TEXT NOT NULL DEFAULT 'general',
    event_signature     TEXT NOT NULL,
    abi                 JSONB NOT NULL,
    default_chains      JSONB NOT NULL DEFAULT '[]',
    default_filters     JSONB NOT NULL DEFAULT '[]',
    parameter_schema    JSONB NOT NULL DEFAULT '[]',
    webhook_event_type  TEXT NOT NULL,
    reputation_threshold FLOAT NOT NULL DEFAULT 0 CHECK (reputation_threshold >= 0 AND reputation_threshold <= 100),
    author_address      TEXT,
    is_public           BOOLEAN NOT NULL DEFAULT TRUE,
    install_count       BIGINT NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trigger_templates_slug ON trigger_templates (slug);
CREATE INDEX IF NOT EXISTS idx_trigger_templates_category ON trigger_templates (category);
CREATE INDEX IF NOT EXISTS idx_trigger_templates_public ON trigger_templates (is_public) WHERE is_public = TRUE;

CREATE TABLE IF NOT EXISTS triggers (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    owner_address       TEXT NOT NULL,
    endpoint_id         TEXT NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
    name                TEXT,
    event_signature     TEXT NOT NULL,
    abi                 JSONB NOT NULL,
    contract_address    TEXT,
    chain_ids           JSONB NOT NULL DEFAULT '[]',
    filter_rules        JSONB NOT NULL DEFAULT '[]',
    webhook_event_type  TEXT NOT NULL,
    reputation_threshold FLOAT NOT NULL DEFAULT 0 CHECK (reputation_threshold >= 0 AND reputation_threshold <= 100),
    batch_id            UUID,
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_triggers_owner ON triggers (owner_address);
CREATE INDEX IF NOT EXISTS idx_triggers_endpoint ON triggers (endpoint_id);
CREATE INDEX IF NOT EXISTS idx_triggers_event_sig ON triggers (event_signature);
CREATE INDEX IF NOT EXISTS idx_triggers_contract ON triggers (contract_address) WHERE contract_address IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_triggers_active ON triggers (active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_triggers_chain_ids ON triggers USING gin (chain_ids);

CREATE TABLE IF NOT EXISTS trigger_instances (
    id                  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    template_id         UUID NOT NULL REFERENCES trigger_templates (id) ON DELETE CASCADE,
    owner_address       TEXT NOT NULL,
    endpoint_id         TEXT NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
    contract_address    TEXT,
    chain_ids           JSONB NOT NULL DEFAULT '[]',
    parameters          JSONB NOT NULL DEFAULT '{}',
    resolved_filters    JSONB NOT NULL DEFAULT '[]',
    active              BOOLEAN NOT NULL DEFAULT TRUE,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_trigger_instances_template ON trigger_instances (template_id);
CREATE INDEX IF NOT EXISTS idx_trigger_instances_owner ON trigger_instances (owner_address);
CREATE INDEX IF NOT EXISTS idx_trigger_instances_active ON trigger_instances (active) WHERE active = TRUE;

-- Auto-increment template install count
CREATE OR REPLACE FUNCTION increment_template_install_count()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE trigger_templates SET install_count = install_count + 1, updated_at = now() WHERE id = NEW.template_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_increment_install_count ON trigger_instances;
CREATE TRIGGER trg_increment_install_count
    AFTER INSERT ON trigger_instances
    FOR EACH ROW EXECUTE FUNCTION increment_template_install_count();

-- Auto-update updated_at
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_triggers_updated_at ON triggers;
CREATE TRIGGER trg_triggers_updated_at BEFORE UPDATE ON triggers FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_trigger_templates_updated_at ON trigger_templates;
CREATE TRIGGER trg_trigger_templates_updated_at BEFORE UPDATE ON trigger_templates FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS trg_trigger_instances_updated_at ON trigger_instances;
CREATE TRIGGER trg_trigger_instances_updated_at BEFORE UPDATE ON trigger_instances FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- Seed: Bazaar starter templates
INSERT INTO trigger_templates (name, slug, description, category, event_signature, abi, default_chains, default_filters, parameter_schema, webhook_event_type, reputation_threshold)
VALUES
    ('Whale Transfer Monitor', 'whale-transfer', 'Fires when a USDC transfer exceeds a threshold amount', 'defi',
     'Transfer(address,address,uint256)',
     '[{"type":"event","name":"Transfer","inputs":[{"name":"from","type":"address","indexed":true},{"name":"to","type":"address","indexed":true},{"name":"value","type":"uint256","indexed":false}]}]'::jsonb,
     '[8453, 1, 42161]'::jsonb,
     '[{"field":"value","op":"gte","value":"1000000000"}]'::jsonb,
     '[{"name":"threshold","type":"uint256","required":true,"description":"Minimum transfer amount in smallest unit"}]'::jsonb,
     'transfer.whale', 0),
    ('DEX Swap Monitor', 'dex-swap', 'Fires on Uniswap V3 swap events for a specific pool', 'defi',
     'Swap(address,address,int256,int256,uint160,uint128,int24)',
     '[{"type":"event","name":"Swap","inputs":[{"name":"sender","type":"address","indexed":true},{"name":"recipient","type":"address","indexed":true},{"name":"amount0","type":"int256","indexed":false},{"name":"amount1","type":"int256","indexed":false},{"name":"sqrtPriceX96","type":"uint160","indexed":false},{"name":"liquidity","type":"uint128","indexed":false},{"name":"tick","type":"int24","indexed":false}]}]'::jsonb,
     '[8453, 1]'::jsonb, '[]'::jsonb,
     '[{"name":"pool_address","type":"address","required":true,"description":"Uniswap V3 pool contract address"}]'::jsonb,
     'dex.swap', 0),
    ('NFT Mint Monitor', 'nft-mint', 'Fires on ERC-721 Transfer events from the zero address (mints)', 'nft',
     'Transfer(address,address,uint256)',
     '[{"type":"event","name":"Transfer","inputs":[{"name":"from","type":"address","indexed":true},{"name":"to","type":"address","indexed":true},{"name":"tokenId","type":"uint256","indexed":true}]}]'::jsonb,
     '[1, 8453]'::jsonb,
     '[{"field":"from","op":"eq","value":"0x0000000000000000000000000000000000000000"}]'::jsonb,
     '[{"name":"contract_address","type":"address","required":true,"description":"NFT contract to watch"}]'::jsonb,
     'nft.mint', 0),
    ('ERC-3009 Payment', 'erc3009-payment', 'Fires when a transferWithAuthorization is executed', 'payments',
     'AuthorizationUsed(address,bytes32)',
     '[{"type":"event","name":"AuthorizationUsed","inputs":[{"name":"authorizer","type":"address","indexed":true},{"name":"nonce","type":"bytes32","indexed":true}]}]'::jsonb,
     '[8453, 1, 42161]'::jsonb, '[]'::jsonb,
     '[{"name":"authorizer","type":"address","required":false,"description":"Filter to a specific authorizer"}]'::jsonb,
     'payment.confirmed', 0),
    ('Ownership Transfer', 'ownership-transfer', 'Fires when contract ownership changes', 'governance',
     'OwnershipTransferred(address,address)',
     '[{"type":"event","name":"OwnershipTransferred","inputs":[{"name":"previousOwner","type":"address","indexed":true},{"name":"newOwner","type":"address","indexed":true}]}]'::jsonb,
     '[1, 8453, 42161]'::jsonb, '[]'::jsonb,
     '[{"name":"contract_address","type":"address","required":true,"description":"Contract to monitor"}]'::jsonb,
     'governance.ownership_transferred', 25)
ON CONFLICT (slug) DO NOTHING;
