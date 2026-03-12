# TripWire

**x402 Execution Middleware for AI Agents**

Programmable onchain event triggers that watch ERC-3009 USDC transfers on Base, Ethereum, and Arbitrum -- and deliver verified webhooks so your application can execute.

---

## What is TripWire?

When a user pays for an API call via the [x402 protocol](https://www.x402.org/), an ERC-3009 `transferWithAuthorization` settles onchain -- but nothing tells the application to _execute_. TripWire fills that gap.

TripWire watches for ERC-3009 (USDC) transfer events across Base, Ethereum, and Arbitrum. When a payment lands, TripWire decodes the event, deduplicates by nonce, resolves the payer's onchain identity via [ERC-8004](https://github.com/ethereum/ERCs/pull/8004), evaluates developer-defined policies, and delivers a signed webhook to your application so it can fulfill the request.

Think of it as **Stripe Webhooks for x402**: register an endpoint, TripWire watches for payments to your address, and you get a verified `payment.confirmed` webhook with transfer data, finality status, and agent identity -- everything you need to execute.

Built on x402 + ERC-8004. Three ingestion paths from sub-second to real-time. Convoy-backed delivery with exponential retry and dead-letter queue. SIWE wallet authentication with no API keys.

---

## Architecture

```
                          INGESTION                          PROCESSING                       DELIVERY
                     (three parallel paths)              (single pipeline)

  +---------------------+
  |   Goldsky Turbo      |  2-4s
  |   (webhook sink)     |------+
  +---------------------+      |
                                |     +--------+   +--------+   +----------+   +--------+   +----------+
  +---------------------+      +---->|        |   |        |   |          |   |        |   |          |
  |  x402 Facilitator    |  ~100ms   | Decode +-->+ Nonce  +-->+ Identity +-->+ Policy +-->+ Dispatch |
  |  (pre-settlement)    |---------->|        |   | Dedup  |   | ERC-8004 |   | Engine |   |          |
  +---------------------+      +--->|        |   |        |   |          |   |        |   +----+-----+
                                |     +--------+   +--------+   +----------+   +--------+        |
  +---------------------+      |                                                                  |
  |  WebSocket RPC       |  real-time                                                            |
  |  (log subscription)  |------+                                                                 |
  +---------------------+                                                                        |
                                                                                                  v
                                                                                    +-------------+-------------+
                                                                                    |                           |
                                                                              +-----v------+          +--------v--------+
                                                                              |   Convoy    |          | Supabase        |
                                                                              |   Webhook   |          | Realtime        |
                                                                              | (execute)   |          | (notify)        |
                                                                              +-----+------+          +--------+--------+
                                                                                    |                           |
                                                                                    v                           v
                                                                              +-----------+            +-----------+
                                                                              | Your API  |            | Your App  |
                                                                              | (HMAC     |            | (push     |
                                                                              |  signed)  |            |  events)  |
                                                                              +-----------+            +-----------+

  AUTH: SIWE (EIP-4361) wallet signatures -- nonce-based replay prevention -- body hash binding
```

**Three ingestion paths, one pipeline:**

| Path | Latency | Source | Use Case |
|------|---------|--------|----------|
| Goldsky Turbo | 2-4s | Webhook sink from indexed chain data | Primary production path, reliable batch delivery |
| x402 Facilitator | ~100ms | Pre-settlement hook from facilitator | Fast path -- payment detected before tx is onchain |
| WebSocket RPC | Real-time | `eth_subscribe` log subscription | Opt-in medium-speed path for direct RPC watchers |

**Two delivery modes:**

| Mode | Transport | Features |
|------|-----------|----------|
| **Execute** | Convoy webhook POST | HMAC-signed, exponential retry (10 attempts), dead-letter queue, delivery logs |
| **Notify** | Supabase Realtime push | Lightweight event stream for dashboards and listeners |

---

## Quick Start

### 1. Install the SDK

```bash
pip install tripwire-sdk[x402]
```

### 2. Export your private key

```bash
export TRIPWIRE_PRIVATE_KEY="0xYourEthereumPrivateKey"
```

Your wallet authenticates via SIWE and pays the $1 USDC registration fee via x402 -- the same key handles both.

### 3. Register an endpoint

```python
import asyncio
from tripwire_sdk import TripwireClient

async def main():
    async with TripwireClient(private_key=os.environ["TRIPWIRE_PRIVATE_KEY"]) as client:
        # Registration costs $1.00 USDC on Base, paid automatically via x402.
        # The SDK intercepts the 402 response, signs an ERC-3009 authorization,
        # and retries -- all transparently.
        endpoint = await client.register_endpoint(
            url="https://your-api.com/webhook",
            mode="execute",
            chains=[8453],  # Base
            recipient="0xYourAddress",
            policies={
                "min_amount": "1000000",       # 1 USDC minimum (6 decimals)
                "min_reputation_score": 70,    # Only trusted agents
            },
        )
        print(f"Endpoint ID: {endpoint.id}")
        print(f"Webhook secret: {endpoint.webhook_secret}")  # Store this securely

asyncio.run(main())
```

### 4. Receive webhooks

When a payment lands at your recipient address, TripWire delivers a signed POST:

```json
{
  "id": "evt_7f3a8b2c-1d4e-5f6a-7b8c-9d0e1f2a3b4c",
  "idempotency_key": "idem_a1b2c3d4e5f6...",
  "type": "payment.confirmed",
  "mode": "execute",
  "timestamp": 1710000000,
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0xabc123...def456",
      "block_number": 12345678,
      "from_address": "0xSenderAddress",
      "to_address": "0xYourAddress",
      "amount": "5000000",
      "nonce": "0xunique_nonce_bytes32",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": {
      "confirmations": 3,
      "required_confirmations": 3,
      "is_finalized": true
    },
    "identity": {
      "address": "0xSenderAddress",
      "agent_class": "trading-bot",
      "deployer": "0xDeployerAddress",
      "capabilities": ["swap", "bridge"],
      "reputation_score": 87.5,
      "registered_at": 1706500000,
      "metadata": {}
    }
  }
}
```

Event types: `payment.confirmed`, `payment.pending`, `payment.pre_confirmed`, `payment.failed`, `payment.reorged`

### 5. Verify signatures

```python
from tripwire_sdk.verify import verify_webhook_signature

def handle_webhook(request):
    is_valid = verify_webhook_signature(
        payload=request.body,
        headers={
            "X-TripWire-ID": request.headers["X-TripWire-ID"],
            "X-TripWire-Timestamp": request.headers["X-TripWire-Timestamp"],
            "X-TripWire-Signature": request.headers["X-TripWire-Signature"],
        },
        secret=endpoint.webhook_secret,  # The secret returned at registration
    )
    if not is_valid:
        return {"error": "Invalid signature"}, 401

    event = request.json()
    if event["type"] == "payment.confirmed":
        transfer = event["data"]["transfer"]
        print(f"Payment: {transfer['amount']} USDC from {transfer['from_address']}")
```

---

## Authentication

TripWire uses **SIWE (Sign-In with Ethereum, EIP-4361)** for all authenticated endpoints. There are no API keys -- your Ethereum wallet is your identity.

**Flow:**

1. **Get a nonce** -- `GET /auth/nonce` returns a cryptographically random nonce stored in Redis with a 5-minute TTL.
2. **Sign a SIWE message** -- Construct an EIP-4361 message with the nonce, your wallet address, and a statement containing `{METHOD} {PATH} {BODY_SHA256}`. Sign it with `personal_sign` (EIP-191).
3. **Send authenticated requests** -- Include these headers on every request:

| Header | Description |
|--------|-------------|
| `X-TripWire-Address` | Your Ethereum address (0x...) |
| `X-TripWire-Signature` | EIP-191 personal_sign hex signature (0x...) |
| `X-TripWire-Nonce` | Nonce from step 1 (single-use) |
| `X-TripWire-Issued-At` | ISO-8601 timestamp when message was signed |
| `X-TripWire-Expiration` | ISO-8601 expiration timestamp |

**Security properties:**

- **Replay prevention** -- Each nonce is atomically consumed from Redis on first use. Reuse returns 401.
- **Body binding** -- The SIWE statement includes the SHA-256 hash of the request body, preventing payload tampering.
- **Expiration** -- Signatures expire after a configurable window (default 5 minutes).
- **Address recovery** -- The server recovers the signer from the signature and compares to the claimed address (case-insensitive, EIP-55 safe).

---

## Payment

Endpoint registration is gated by the **x402 protocol** -- registering a webhook endpoint costs **$1.00 USDC on Base**.

**How it works:**

1. You call `POST /api/v1/endpoints`.
2. The server responds with `HTTP 402 Payment Required` and a payment header specifying the price, network (`eip155:8453`), and treasury address.
3. The SDK's x402 interceptor sees the 402, constructs an ERC-3009 `transferWithAuthorization` signature using your private key -- no onchain transaction yet.
4. The interceptor retries the original request with the signed payment authorization in headers.
5. The x402 facilitator verifies the authorization and submits the USDC transfer onchain.
6. The server returns `201 Created` with the endpoint and a `registration_tx_hash` proving payment.

All of this is handled transparently by the SDK when you install `tripwire-sdk[x402]`. Your wallet just needs USDC on Base.

**Configuration:**

| Variable | Default | Description |
|----------|---------|-------------|
| `X402_FACILITATOR_URL` | `https://x402.org/facilitator` | x402 facilitator endpoint |
| `X402_REGISTRATION_PRICE` | `$1.00` | Registration price |
| `X402_NETWORK` | `eip155:8453` | Payment network (Base mainnet) |
| `TRIPWIRE_TREASURY_ADDRESS` | -- | USDC recipient address for registration payments |

---

## API Reference

All business endpoints are prefixed with `/api/v1`. Operational endpoints are at the root.

### Operational

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/health` | None | Basic health check (status, version) |
| `GET` | `/health/detailed` | None | Deep health check -- probes Supabase, webhook provider, identity resolver |
| `GET` | `/ready` | None | Readiness probe -- returns 200 only after startup completes |

### Authentication

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/auth/nonce` | None | Generate a SIWE nonce (stored in Redis, 5-min TTL, 30/min rate limit) |

### Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/endpoints` | SIWE + x402 | Register a new webhook endpoint ($1 USDC). Returns endpoint with `webhook_secret` |
| `GET` | `/api/v1/endpoints` | SIWE | List all active endpoints owned by the authenticated wallet |
| `GET` | `/api/v1/endpoints/{id}` | SIWE | Get endpoint details |
| `PATCH` | `/api/v1/endpoints/{id}` | SIWE | Update endpoint (URL, mode, chains, policies, active) |
| `DELETE` | `/api/v1/endpoints/{id}` | SIWE | Deactivate (soft-delete) an endpoint |

### Subscriptions (Notify Mode)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/endpoints/{id}/subscriptions` | SIWE | Create a subscription filter for a notify-mode endpoint |
| `GET` | `/api/v1/endpoints/{id}/subscriptions` | SIWE | List active subscriptions for an endpoint |
| `DELETE` | `/api/v1/subscriptions/{id}` | SIWE | Deactivate a subscription |

### Events

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/events` | SIWE | List events with cursor pagination. Filters: `event_type`, `chain_id` |
| `GET` | `/api/v1/events/{id}` | SIWE | Get event details |
| `GET` | `/api/v1/endpoints/{id}/events` | SIWE | List events for a specific endpoint |

### Deliveries

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/deliveries` | SIWE | List deliveries. Filters: `endpoint_id`, `event_id`, `status` |
| `GET` | `/api/v1/deliveries/{id}` | SIWE | Get delivery details |
| `GET` | `/api/v1/endpoints/{id}/deliveries` | SIWE | List deliveries for a specific endpoint |
| `GET` | `/api/v1/endpoints/{id}/deliveries/stats` | SIWE | Delivery stats (total, pending, sent, delivered, failed, success rate) |
| `POST` | `/api/v1/deliveries/{id}/retry` | SIWE | Retry a failed delivery via Convoy |

### Ingestion (Internal)

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `POST` | `/api/v1/ingest/goldsky` | Bearer token | Receive batch of Goldsky-decoded ERC-3009 events |
| `POST` | `/api/v1/ingest/event` | Bearer token | Process a single raw event (testing / manual submission) |
| `POST` | `/api/v1/ingest/facilitator` | Bearer token | Receive pre-settlement ERC-3009 data from x402 facilitator (~100ms fast path) |

### Stats

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/api/v1/stats` | SIWE | Processing statistics scoped to the authenticated wallet |

---

## Configuration

All configuration is via environment variables (loaded from `.env` via pydantic-settings).

### Application

| Variable | Default | Description |
|----------|---------|-------------|
| `APP_ENV` | `production` | Environment -- `development` or `production`. Production enforces all secrets. |
| `APP_PORT` | `3402` | Server port |
| `APP_BASE_URL` | `http://localhost:3402` | Public base URL |
| `LOG_LEVEL` | `info` | Log level (structlog) |
| `CORS_ALLOWED_ORIGINS` | `["http://localhost:3000", "http://localhost:3402"]` | CORS allowed origins |

### Supabase

| Variable | Default | Description |
|----------|---------|-------------|
| `SUPABASE_URL` | -- | Supabase project URL (required in production) |
| `SUPABASE_ANON_KEY` | -- | Supabase anon/public key |
| `SUPABASE_SERVICE_ROLE_KEY` | -- | Supabase service role key (required in production) |

### Convoy (Webhook Delivery)

| Variable | Default | Description |
|----------|---------|-------------|
| `CONVOY_API_KEY` | -- | Convoy API key (required in production) |
| `CONVOY_URL` | `http://localhost:5005` | Convoy server URL |
| `WEBHOOK_SIGNING_SECRET` | -- | Default HMAC secret (can be overridden per endpoint) |

### Goldsky (Chain Indexing)

| Variable | Default | Description |
|----------|---------|-------------|
| `GOLDSKY_API_KEY` | -- | Goldsky API key |
| `GOLDSKY_PROJECT_ID` | -- | Goldsky project ID |
| `GOLDSKY_WEBHOOK_SECRET` | -- | Shared secret for Goldsky webhook auth |

### Blockchain RPC

| Variable | Default | Description |
|----------|---------|-------------|
| `BASE_RPC_URL` | `https://mainnet.base.org` | Base mainnet RPC |
| `ETHEREUM_RPC_URL` | `https://eth.llamarpc.com` | Ethereum mainnet RPC |
| `ARBITRUM_RPC_URL` | `https://arb1.arbitrum.io/rpc` | Arbitrum One RPC |

### x402 Payment Gating

| Variable | Default | Description |
|----------|---------|-------------|
| `X402_FACILITATOR_URL` | `https://x402.org/facilitator` | x402 facilitator URL |
| `X402_REGISTRATION_PRICE` | `$1.00` | Registration price |
| `X402_NETWORK` | `eip155:8453` | Payment network (Base mainnet) |
| `TRIPWIRE_TREASURY_ADDRESS` | -- | USDC recipient for registration payments (required in production) |

### Authentication

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_TIMESTAMP_TOLERANCE_SECONDS` | `300` | SIWE signature expiration tolerance |
| `REDIS_URL` | `redis://localhost:6379` | Redis URL for nonce storage |
| `SIWE_DOMAIN` | `tripwire.dev` | SIWE message domain |

### Facilitator Webhook

| Variable | Default | Description |
|----------|---------|-------------|
| `FACILITATOR_WEBHOOK_SECRET` | -- | Bearer token for x402 facilitator ingest endpoint |

### ERC-8004 Identity

| Variable | Default | Description |
|----------|---------|-------------|
| `ERC8004_IDENTITY_REGISTRY` | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | ERC-8004 identity registry (CREATE2, same on all chains) |
| `ERC8004_REPUTATION_REGISTRY` | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | ERC-8004 reputation registry |
| `IDENTITY_CACHE_TTL` | `300` | Identity resolution cache TTL (seconds) |

### WebSocket Subscriber

| Variable | Default | Description |
|----------|---------|-------------|
| `WS_SUBSCRIBER_ENABLED` | `false` | Enable WebSocket RPC log subscription (opt-in) |
| `ETHEREUM_WS_URL` | -- | Ethereum WebSocket RPC URL |
| `BASE_WS_URL` | -- | Base WebSocket RPC URL |
| `ARBITRUM_WS_URL` | -- | Arbitrum WebSocket RPC URL |

### Dead Letter Queue

| Variable | Default | Description |
|----------|---------|-------------|
| `DLQ_ENABLED` | `true` | Enable DLQ background poller |
| `DLQ_POLL_INTERVAL_SECONDS` | `60` | DLQ poll interval |
| `DLQ_MAX_RETRIES` | `3` | Max DLQ retries before alerting |
| `DLQ_ALERT_WEBHOOK_URL` | -- | URL to POST alerts for permanently failed deliveries |

### Finality Poller

| Variable | Default | Description |
|----------|---------|-------------|
| `FINALITY_POLLER_ENABLED` | `true` | Enable background finality confirmation + reorg detection |
| `FINALITY_POLL_INTERVAL_ARBITRUM` | `5` | Arbitrum poll interval (seconds) |
| `FINALITY_POLL_INTERVAL_BASE` | `10` | Base poll interval (seconds) |
| `FINALITY_POLL_INTERVAL_ETHEREUM` | `30` | Ethereum poll interval (seconds) |

---

## Deployment

### Railway (Production)

TripWire is designed for Railway deployment. Set all environment variables in the Railway dashboard and deploy from the repo. The server listens on port 3402.

Required services:
- **TripWire** -- the FastAPI application
- **Supabase** -- managed database (external, hosted on supabase.com)
- **Redis** -- nonce storage for SIWE auth
- **Convoy** -- self-hosted or managed webhook delivery

### Docker Compose (Local Development)

The included `docker-compose.yml` sets up TripWire with a full Convoy stack:

```bash
docker compose up
```

This starts:
- **tripwire** -- FastAPI server on port 3402
- **convoy-server** -- Convoy API on port 5005
- **convoy-worker** -- Convoy background agent for delivery processing
- **convoy-postgres** -- PostgreSQL 16 for Convoy on port 5433
- **convoy-redis** -- Redis 7 for Convoy on port 6380

The TripWire container health-checks against `/health` and waits for Convoy to be healthy before starting.

For local development without Docker:

```bash
pip install -e ".[dev]"
cp .env.example .env
# Edit .env with your Supabase credentials
python -m tripwire.main
# Server starts on http://localhost:3402
```

---

## Tech Stack

| Component | Technology | Role |
|-----------|------------|------|
| API Framework | [FastAPI](https://fastapi.tiangolo.com/) + Uvicorn | Async HTTP server with auto-generated OpenAPI docs |
| Database | [Supabase](https://supabase.com/) (PostgreSQL) | Endpoints, events, nonces, deliveries + Realtime for notify mode |
| Webhook Delivery | [Convoy](https://getconvoy.io/) (self-hosted) | Exponential retry (10 attempts), HMAC signing, DLQ, delivery logs |
| Chain Indexing | [Goldsky](https://goldsky.com/) Turbo | Real-time ERC-3009 event streaming via webhook sink |
| Nonce Storage | [Redis](https://redis.io/) | SIWE nonce storage with TTL for replay prevention |
| Payment Protocol | [x402](https://www.x402.org/) | HTTP-native micropayments -- $1 USDC registration on Base |
| Agent Identity | [ERC-8004](https://github.com/ethereum/ERCs/pull/8004) | Onchain AI agent identity registry (class, deployer, reputation) |
| ABI Decoding | eth-abi + eth-account | ERC-3009 event decoding, EIP-191 signature recovery |
| Validation | Pydantic v2 | Runtime type safety for all inputs, outputs, and configuration |
| Logging | structlog | Structured JSON logging for production observability |
| HTTP Client | httpx | Async HTTP with connection pooling for RPC + Convoy calls |
| Rate Limiting | slowapi | Per-route rate limits for CRUD and ingestion endpoints |

---

## License

Proprietary. All Rights Reserved.

---

Built for the agentic web. Payments happen onchain. TripWire makes them actionable.
