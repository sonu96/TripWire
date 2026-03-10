# Getting Started with TripWire

This guide walks you through setting up TripWire from scratch, registering your first webhook endpoint, and verifying that payment events are delivered correctly.

## Prerequisites

- **Python 3.11+** -- check with `python --version`
- **Supabase account** -- free tier at [supabase.com](https://supabase.com)
- **Svix account** -- free tier (50k messages/month) at [svix.com](https://svix.com)
- **Git** -- to clone the repository

## 1. Clone and Install

```bash
git clone https://github.com/your-org/tripwire.git
cd tripwire

# Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install TripWire in editable mode
pip install -e .
```

## 2. Set Up Supabase

1. Go to [supabase.com](https://supabase.com) and create a new project.
2. Once your project is ready, navigate to **Settings > API** and copy:
   - **Project URL** (e.g. `https://abcdefgh.supabase.co`)
   - **anon/public key** (starts with `eyJ...`)
   - **service_role key** (starts with `eyJ...`) -- keep this secret

## 3. Run the SQL Migration

Navigate to **SQL Editor** in your Supabase dashboard and paste the contents of the initial migration:

```sql
-- File: tripwire/db/migrations/001_initial.sql

-- Enable UUID generation
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- Endpoints
CREATE TABLE IF NOT EXISTS endpoints (
    id          TEXT PRIMARY KEY,
    url         TEXT NOT NULL,
    mode        TEXT NOT NULL CHECK (mode IN ('notify', 'execute')),
    chains      JSONB NOT NULL DEFAULT '[]',
    recipient   TEXT NOT NULL,
    policies    JSONB NOT NULL DEFAULT '{}',
    api_key_hash TEXT,
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_endpoints_recipient ON endpoints (recipient);
CREATE INDEX IF NOT EXISTS idx_endpoints_active ON endpoints (active) WHERE active = TRUE;
CREATE INDEX IF NOT EXISTS idx_endpoints_mode ON endpoints (mode);

-- Subscriptions
CREATE TABLE IF NOT EXISTS subscriptions (
    id          TEXT PRIMARY KEY,
    endpoint_id TEXT NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
    filters     JSONB NOT NULL DEFAULT '{}',
    active      BOOLEAN NOT NULL DEFAULT TRUE,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_subscriptions_endpoint_id ON subscriptions (endpoint_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_active ON subscriptions (active) WHERE active = TRUE;

-- Events
CREATE TABLE IF NOT EXISTS events (
    id              TEXT PRIMARY KEY,
    chain_id        INTEGER NOT NULL,
    tx_hash         TEXT NOT NULL,
    block_number    BIGINT NOT NULL,
    block_hash      TEXT NOT NULL,
    log_index       INTEGER NOT NULL,
    from_address    TEXT NOT NULL,
    to_address      TEXT NOT NULL,
    amount          TEXT NOT NULL,
    authorizer      TEXT NOT NULL,
    nonce           TEXT NOT NULL,
    token           TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    finality_depth  INTEGER NOT NULL DEFAULT 0,
    identity_data   JSONB,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    confirmed_at    TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_events_chain_tx ON events (chain_id, tx_hash);
CREATE INDEX IF NOT EXISTS idx_events_to_address ON events (to_address);
CREATE INDEX IF NOT EXISTS idx_events_from_address ON events (from_address);
CREATE INDEX IF NOT EXISTS idx_events_authorizer ON events (authorizer);
CREATE INDEX IF NOT EXISTS idx_events_nonce ON events (chain_id, nonce, authorizer);
CREATE INDEX IF NOT EXISTS idx_events_status ON events (status);
CREATE INDEX IF NOT EXISTS idx_events_block ON events (chain_id, block_number);
CREATE INDEX IF NOT EXISTS idx_events_created_at ON events (created_at);

-- Nonces (deduplication)
CREATE TABLE IF NOT EXISTS nonces (
    chain_id    INTEGER NOT NULL,
    nonce       TEXT NOT NULL,
    authorizer  TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chain_id, nonce, authorizer)
);

CREATE INDEX IF NOT EXISTS idx_nonces_authorizer ON nonces (authorizer);

-- Webhook Deliveries
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id              TEXT PRIMARY KEY,
    endpoint_id     TEXT NOT NULL REFERENCES endpoints (id) ON DELETE CASCADE,
    event_id        TEXT NOT NULL REFERENCES events (id) ON DELETE CASCADE,
    svix_message_id TEXT,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_endpoint ON webhook_deliveries (endpoint_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_event ON webhook_deliveries (event_id);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_status ON webhook_deliveries (status);

-- Audit Log
CREATE TABLE IF NOT EXISTS audit_log (
    id          TEXT PRIMARY KEY DEFAULT gen_random_uuid()::text,
    action      TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}',
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log (action);
CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log (created_at);
```

Click **Run** in the SQL Editor. All tables and indexes will be created.

## 4. Configure Environment Variables

Copy the example `.env` file and fill in your credentials:

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
# TripWire Configuration
APP_ENV=development
APP_PORT=3402

# Supabase (from Step 2)
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=eyJ...your_anon_key
SUPABASE_SERVICE_ROLE_KEY=eyJ...your_service_role_key

# Svix (from svix.com dashboard)
SVIX_API_KEY=sk_your_svix_api_key
SVIX_SIGNING_SECRET=whsec_your_signing_secret

# Goldsky (optional -- needed for production indexing)
GOLDSKY_API_KEY=
GOLDSKY_PROJECT_ID=

# Blockchain RPC (defaults work for public endpoints)
BASE_RPC_URL=https://mainnet.base.org
ETHEREUM_RPC_URL=https://eth.llamarpc.com
ARBITRUM_RPC_URL=https://arb1.arbitrum.io/rpc
```

At minimum, you need `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`, and `SVIX_API_KEY`.

## 5. Start the Server

```bash
python -m tripwire.main
```

You should see output like:

```
INFO     tripwire_starting env=development port=3402
INFO     supabase_ready
INFO     svix_ready
INFO     Uvicorn running on http://0.0.0.0:3402 (Press CTRL+C to quit)
```

Verify the server is running:

```bash
curl http://localhost:3402/health
# {"status": "ok"}
```

## 6. Register Your First Endpoint

Register a webhook endpoint to receive payment events for a specific recipient address on Base:

```bash
curl -X POST http://localhost:3402/api/v1/endpoints \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-app.example.com/webhook",
    "mode": "execute",
    "chains": [8453],
    "recipient": "0xYourRecipientAddress"
  }'
```

Response:

```json
{
  "id": "ep_abc123...",
  "url": "https://your-app.example.com/webhook",
  "mode": "execute",
  "chains": [8453],
  "recipient": "0xYourRecipientAddress",
  "policies": {
    "min_amount": null,
    "max_amount": null,
    "allowed_senders": null,
    "blocked_senders": null,
    "required_agent_class": null,
    "min_reputation_score": null,
    "finality_depth": 3
  },
  "active": true,
  "created_at": "2026-01-15T10:30:00Z",
  "updated_at": "2026-01-15T10:30:00Z"
}
```

Save the `id` field -- you will need it to manage the endpoint later.

## 7. Register with Policies (Optional)

You can set policies to filter which payments trigger webhooks:

```bash
curl -X POST http://localhost:3402/api/v1/endpoints \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-app.example.com/webhook",
    "mode": "execute",
    "chains": [8453, 42161],
    "recipient": "0xYourRecipientAddress",
    "policies": {
      "min_amount": "1000000",
      "finality_depth": 5,
      "min_reputation_score": 50.0
    }
  }'
```

This endpoint only triggers for payments of at least 1 USDC (1,000,000 in 6-decimal units), waits for 5 blocks of finality, and requires a sender reputation score of at least 50.

## 8. Simulate a Webhook (Testing)

For local testing, you can use a tool like [webhook.site](https://webhook.site) or [ngrok](https://ngrok.com) to expose a local endpoint:

```bash
# Using ngrok to expose your local app
ngrok http 8000

# Then register the ngrok URL as your endpoint
curl -X POST http://localhost:3402/api/v1/endpoints \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://abc123.ngrok.io/webhook",
    "mode": "execute",
    "chains": [8453],
    "recipient": "0xYourRecipientAddress"
  }'
```

When a real x402 payment is made to your recipient address on a configured chain, TripWire will:

1. Detect the ERC-3009 `transferWithAuthorization` event via Goldsky indexing
2. Verify finality by checking block confirmations
3. Resolve identity via ERC-8004 (if available)
4. Evaluate your endpoint policies
5. Deliver the webhook via Svix (with retries and HMAC signing)

## 9. Verify It Works

Check your endpoint is registered:

```bash
curl http://localhost:3402/api/v1/endpoints
```

Check for events (will be empty until a real payment occurs):

```bash
curl http://localhost:3402/api/v1/events
```

Check server health:

```bash
curl http://localhost:3402/health
```

## Next Steps

- **[Configuration Guide](./configuration.md)** -- all environment variables explained
- **[Python SDK](../sdk/python-sdk.md)** -- use the SDK instead of raw HTTP calls
- **[Webhook Verification](./webhook-verification.md)** -- verify incoming webhook signatures
- **[API Reference](../api-reference/)** -- full REST API documentation
