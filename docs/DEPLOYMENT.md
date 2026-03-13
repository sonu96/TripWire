# TripWire Deployment Guide

## Prerequisites

- **Python 3.11+** (required by `pyproject.toml`)
- **Supabase account** with a PostgreSQL database (used for all persistent storage, RLS, and Realtime)
- **Redis 7+** (used for SIWE nonce management in wallet-based auth)
- **Convoy v26.2.2** (webhook delivery engine — server + agent processes)
- **Goldsky account** (optional, for production blockchain event ingestion via Turbo pipelines)
- **x402 SDK** (bundled as a dependency; requires `x402[fastapi,evm]>=2.3.0`)

---

## Environment Variables

All configuration is managed through `tripwire/config/settings.py` using `pydantic-settings`. Variables are read from the environment or a `.env` file.

### App

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `APP_ENV` | No | `production` | Environment name. Set to `development` for dev mode, `testing` for tests. Production mode enforces secret validation. |
| `APP_PORT` | No | `3402` | Port the FastAPI server listens on. |
| `APP_BASE_URL` | No | `http://localhost:3402` | Public base URL of the TripWire instance. |
| `LOG_LEVEL` | No | `info` | Logging level (`debug`, `info`, `warning`, `error`). |
| `CORS_ALLOWED_ORIGINS` | No | `["http://localhost:3000", "http://localhost:3402"]` | JSON list of allowed CORS origins. |

### Supabase

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `SUPABASE_URL` | **Yes (prod)** | `""` | Supabase project URL (e.g., `https://xyz.supabase.co`). |
| `SUPABASE_ANON_KEY` | No | `""` | Supabase anonymous/public key. |
| `SUPABASE_SERVICE_ROLE_KEY` | **Yes (prod)** | `""` | Supabase service role key (SecretStr). Used for privileged database operations. |

### Convoy (Webhook Delivery)

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CONVOY_API_KEY` | **Yes (prod)** | `""` | Convoy API key (SecretStr). When empty in dev, falls back to `LogOnlyProvider`. |
| `CONVOY_URL` | No | `http://localhost:5005` | Convoy server URL. |
| `WEBHOOK_SIGNING_SECRET` | No | `""` | Default HMAC secret for webhook signatures. Can be overridden per endpoint. |

### Goldsky

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `GOLDSKY_API_KEY` | No | `""` | Goldsky API key for pipeline management (SecretStr). |
| `GOLDSKY_PROJECT_ID` | No | `""` | Goldsky project identifier. |
| `GOLDSKY_WEBHOOK_SECRET` | No | `""` | HMAC secret for validating Goldsky webhook payloads (SecretStr). If empty in non-dev environments, ingest endpoints will reject requests. |

### x402 Facilitator

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FACILITATOR_WEBHOOK_SECRET` | No | `""` | Secret for validating x402 facilitator webhook callbacks (SecretStr). |

### Blockchain RPC

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BASE_RPC_URL` | No | `https://mainnet.base.org` | Base mainnet JSON-RPC endpoint. |
| `ETHEREUM_RPC_URL` | No | `https://eth.llamarpc.com` | Ethereum mainnet JSON-RPC endpoint. |
| `ARBITRUM_RPC_URL` | No | `https://arb1.arbitrum.io/rpc` | Arbitrum One JSON-RPC endpoint. |

### x402 Payment Gating

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `X402_FACILITATOR_URL` | No | `https://x402.org/facilitator` | URL of the x402 facilitator service. |
| `X402_REGISTRATION_PRICE` | No | `$1.00` | Price for endpoint registration via x402 payment. |
| `X402_NETWORK` | No | `eip155:8453` | Chain identifier for x402 payments (Base mainnet). |
| `TRIPWIRE_TREASURY_ADDRESS` | **Yes (prod)** | `""` | Ethereum address that receives USDC registration payments. When empty, x402 payment gating is disabled. |

### Wallet-Based Auth

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `AUTH_TIMESTAMP_TOLERANCE_SECONDS` | No | `300` | Maximum age (in seconds) of a SIWE signature before it is rejected. |
| `REDIS_URL` | No | `redis://localhost:6379` | Redis connection URL for SIWE nonce storage. |
| `SIWE_DOMAIN` | No | `tripwire.dev` | Domain used in SIWE (EIP-4361) message construction. |

### Dead Letter Queue

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DLQ_ENABLED` | No | `true` | Enable background DLQ poller for failed Convoy deliveries. |
| `DLQ_POLL_INTERVAL_SECONDS` | No | `60` | How often (seconds) the DLQ handler polls for failed deliveries. |
| `DLQ_MAX_RETRIES` | No | `3` | Maximum retry attempts for a failed delivery before it is marked dead. |
| `DLQ_ALERT_WEBHOOK_URL` | No | `""` | Optional webhook URL to receive alerts when deliveries exhaust retries. |

### Finality Poller

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `FINALITY_POLLER_ENABLED` | No | `true` | Enable the background finality confirmation poller. |
| `FINALITY_POLL_INTERVAL_ARBITRUM` | No | `5` | Poll interval (seconds) for Arbitrum block finality checks. |
| `FINALITY_POLL_INTERVAL_BASE` | No | `10` | Poll interval (seconds) for Base block finality checks. |
| `FINALITY_POLL_INTERVAL_ETHEREUM` | No | `30` | Poll interval (seconds) for Ethereum block finality checks. |

### Identity Resolver

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `IDENTITY_CACHE_TTL` | No | `300` | TTL (seconds) for cached ERC-8004 identity lookups. |

### ERC-8004 Registries

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ERC8004_IDENTITY_REGISTRY` | No | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | Address of the ERC-8004 identity registry contract. |
| `ERC8004_REPUTATION_REGISTRY` | No | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | Address of the ERC-8004 reputation registry contract. |

### Goldsky Edge RPC

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `BASE_RPC_URL` | Yes (prod) | `""` | Goldsky Edge RPC endpoint for Base. |
| `ETHEREUM_RPC_URL` | Yes (prod) | `""` | Goldsky Edge RPC endpoint for Ethereum. |
| `ARBITRUM_RPC_URL` | Yes (prod) | `""` | Goldsky Edge RPC endpoint for Arbitrum. |
| `GOLDSKY_EDGE_API_KEY` | No | `""` | Goldsky Edge API key for authenticated RPC access. |

### Production Validation

When `APP_ENV=production`, the settings validator enforces that these are non-empty: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CONVOY_API_KEY`, `TRIPWIRE_TREASURY_ADDRESS`. The app will refuse to start if any are missing.

---

## Database Setup

TripWire uses Supabase (PostgreSQL) for all persistent storage. Migrations must be run in order against your Supabase database using the SQL editor or `psql`.

### Migration Order

Run each migration file from `tripwire/db/migrations/` sequentially:

1. **`001_initial.sql`** -- Core schema: `endpoints`, `subscriptions`, `events`, `nonces`, `webhook_deliveries`, `audit_log` tables with indexes.
2. **`002_add_event_type_data.sql`** through **`009_performance_indexes.sql`** -- Incremental schema evolution (event types, realtime, endpoint_id FK on events, Svix-to-Convoy migration, performance indexes).
3. **`010_wallet_auth.sql`** -- Adds `owner_address`, `registration_tx_hash`, `registration_chain_id` to endpoints. Removes legacy API key columns.
4. **`011_rls_policies.sql`** -- Enables Row Level Security on all tables. Creates the `set_wallet_context()` RPC function and per-table isolation policies using `app.current_wallet` session variable.

### Running Migrations

```bash
# Via Supabase SQL Editor (recommended):
# Paste each .sql file's contents into the SQL editor and run them in order.

# Via psql:
psql "$SUPABASE_DB_URL" -f tripwire/db/migrations/001_initial.sql
psql "$SUPABASE_DB_URL" -f tripwire/db/migrations/010_wallet_auth.sql
psql "$SUPABASE_DB_URL" -f tripwire/db/migrations/011_rls_policies.sql
# ... and all migrations in between
```

The RLS policies in `011_rls_policies.sql` use a `set_wallet_context(wallet_address)` function that the application calls before each request. This ensures multi-tenant isolation at the database level -- each wallet can only see its own endpoints, subscriptions, events, and deliveries.

---

## Local Development

### Using docker-compose

The `docker-compose.yml` provides the full Convoy stack (server, worker/agent, Postgres, Redis) and optionally the TripWire container itself:

```bash
# Start Convoy infrastructure only (recommended for local dev):
docker compose up convoy-server convoy-worker convoy-postgres convoy-redis

# Or start everything including TripWire:
docker compose up
```

Services and ports:
- **Convoy server**: `localhost:5005`
- **Convoy Postgres**: `localhost:5433` (mapped from container port 5432)
- **Convoy Redis**: `localhost:6380` (mapped from container port 6379)
- **TripWire** (if using compose): `localhost:3402`

### Running TripWire directly

For development with hot reload:

```bash
# Create .env with your Supabase credentials (APP_ENV=development)
python dev_server.py
```

`dev_server.py` does two things:
1. Sets `APP_ENV=development` to skip production secret validation.
2. Overrides `require_wallet_auth` with a bypass that returns a hardcoded dev wallet (`0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`, Hardhat account #0). You can override this with the `DEV_WALLET_ADDRESS` env var.

The auth bypass exists **only** in `dev_server.py` and must never be deployed. The production entry point is `tripwire.main:app`.

---

## Railway Deployment

TripWire is designed to run on Railway (or similar PaaS). You need the following services:

### Services

| Service | Image / Source | Notes |
|---------|---------------|-------|
| **TripWire** | Build from repo | Entry point: `uvicorn tripwire.main:app --host 0.0.0.0 --port 3402` |
| **Convoy Server** | `getconvoy/convoy:v26.2.2` | Command: `server`. Needs its own Postgres and Redis. |
| **Convoy Agent** | `getconvoy/convoy:v26.2.2` | Command: `agent`. Processes webhook deliveries. Points at Convoy Server. |
| **Convoy Postgres** | `postgres:16-alpine` | Dedicated database for Convoy state. |
| **Convoy Redis** | `redis:7-alpine` | Queue backend for Convoy. |

### Environment Configuration

For the TripWire service, set:
- `APP_ENV=production`
- `APP_PORT=3402`
- `APP_BASE_URL=https://your-railway-domain.up.railway.app`
- All Supabase credentials (`SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_SERVICE_ROLE_KEY`)
- `CONVOY_API_KEY` and `CONVOY_URL` pointing to the Convoy Server service
- `TRIPWIRE_TREASURY_ADDRESS` for x402 payment gating
- `REDIS_URL` pointing to the Convoy Redis service (or a separate Redis instance)
- `GOLDSKY_WEBHOOK_SECRET` for ingest endpoint authentication
- RPC URLs for any chains you are monitoring

For Convoy services, replicate the environment variables from `docker-compose.yml`, substituting Railway service hostnames for `convoy-postgres` and `convoy-redis`.

---

## Goldsky Pipeline

TripWire ingests on-chain ERC-3009 `AuthorizationUsed` events via a Goldsky Turbo pipeline. The pipeline watches USDC contracts on supported chains and POSTs decoded events to TripWire's ingest endpoint.

### Deploying a Pipeline

1. Install the Goldsky CLI and authenticate with your `GOLDSKY_API_KEY`.
2. Create a Turbo pipeline targeting the ERC-3009 `AuthorizationUsed(address authorizer, bytes32 nonce)` event on the USDC contract for your chain (e.g., Base USDC at `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913`).
3. Set the pipeline's webhook destination to `https://your-domain/api/v1/ingest/goldsky`.
4. Configure the pipeline's webhook secret to match `GOLDSKY_WEBHOOK_SECRET` in your TripWire environment.

The ingest endpoint validates the HMAC signature from Goldsky before processing. If `GOLDSKY_WEBHOOK_SECRET` is empty in non-development environments, all ingest requests will be rejected.

### Pipeline Data

The pipeline should deliver decoded log data including:
- `transaction_hash`, `block_number`, `block_hash`, `log_index`, `block_timestamp`
- `address` (token contract)
- `chain_id`
- `decoded.authorizer`, `decoded.nonce`
- `transfer.from_address`, `transfer.to_address`, `transfer.value` (joined Transfer event data)

---

## Health Checks

TripWire exposes three operational endpoints (not behind auth):

### `GET /health`

Basic liveness probe. Returns `200 OK` immediately.

```json
{"status": "ok", "service": "tripwire", "version": "0.1.0"}
```

Use this for container health checks and load balancer probes. The `docker-compose.yml` uses this endpoint with a 30-second interval.

### `GET /health/detailed`

Deep health check that probes Supabase connectivity, webhook provider status, and identity resolver. Returns `200` when all components are healthy, `503` when any component is unhealthy.

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "uptime_seconds": 3600.1,
  "components": {
    "supabase": {"status": "healthy"},
    "webhook_provider": {"status": "healthy", "type": "convoy"},
    "identity_resolver": {"status": "healthy", "type": "ERC8004Resolver"}
  }
}
```

### `GET /ready`

Readiness probe. Returns `200` only after the full lifespan startup completes (Supabase client, webhook provider, identity resolver, nonce repo, event processor, DLQ handler, and finality poller are all initialized). Returns `503` during startup.

---

## Monitoring

### Structured Logging

TripWire uses `structlog` for all logging. Logs are structured JSON in production, human-readable in development.

### Key Log Events to Watch

| Event | Level | Meaning |
|-------|-------|---------|
| `tripwire_starting` | INFO | Application startup initiated. Includes version, env, port. |
| `tripwire_ready` | INFO | All subsystems initialized, ready for traffic. |
| `tripwire_shutting_down` | INFO | Graceful shutdown in progress. |
| `supabase_ready` | INFO | Supabase client connected. |
| `webhook_provider_ready` | INFO | Convoy (or LogOnly fallback) initialized. |
| `dlq_handler_ready` | INFO | Dead letter queue poller started. |
| `finality_poller_ready` | INFO | Block finality confirmation poller started. |
| `goldsky_webhook_secret_missing` | WARNING | Goldsky secret is empty in non-dev environment; ingest will reject all requests. |
| `x402_payment_gating_disabled` | WARNING | Treasury address is empty; endpoint registration is free. |
| `x402_payment_gating_unavailable` | WARNING | x402 package not installed. |
| `supabase_api_error` | ERROR | PostgreSQL/PostgREST error during a request. Includes error code and path. |
| `network_connect_error` | ERROR | httpx connection failure (Convoy, RPC, etc.). |
| `network_timeout_error` | ERROR | httpx timeout. |
| `unhandled_exception` | ERROR | Catch-all for unexpected errors. Includes full traceback. |

### Rate Limiting

TripWire uses `slowapi` for rate limiting. Rate-limited requests return `429 Too Many Requests` and are logged via the `SlowAPIMiddleware`.

### Request Logging

The `RequestLoggingMiddleware` logs every request with method, path, status code, and duration. Use these logs to monitor latency and error rates.
