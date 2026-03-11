# TripWire v1 — Status & Roadmap

> Last updated: 2026-03-10

## v1 Summary

TripWire v1 is the complete x402 execution middleware — the infrastructure layer between onchain ERC-3009 micropayments and application execution. Two delivery modes: **Execute** (Svix webhook delivery) and **Notify** (Supabase Realtime push).

**67 tests passing** | **29 source modules** | **7 DB migrations** | **Python SDK**

---

## Completed in v1

### Core Pipeline
- [x] **ERC-3009 event decoding** — Decodes both `Transfer` and `AuthorizationUsed` events from Goldsky-streamed data. Extracts from_address, to_address, value, authorizer, nonce.
- [x] **Nonce deduplication** — Atomic upsert with `ignore_duplicates` prevents replay attacks. Unique constraint on `(chain_id, nonce, authorizer)`.
- [x] **Block finality tracking** — Per-chain confirmation depths (Ethereum: 12, Base: 3, Arbitrum: 1) via JSON-RPC `eth_blockNumber`.
- [x] **ERC-8004 identity resolution** — Onchain agent identity lookup from IdentityRegistry + ReputationRegistry. Parallel RPC calls via `asyncio.gather()`. In-memory cache with configurable TTL.
- [x] **Policy engine** — Per-endpoint rules: min/max amount, allowed/blocked senders, required agent class, minimum reputation score, custom finality depth.
- [x] **Endpoint matching** — Routes events to endpoints by recipient address + chain ID.

### Delivery
- [x] **Execute mode (Svix)** — Webhook delivery with retries, HMAC signing, DLQ. Svix app + endpoint created on registration.
- [x] **Notify mode (Supabase Realtime)** — Events inserted into `realtime_events` table, pushed to subscribed clients via WebSocket.
- [x] **WebhookProvider abstraction** — Protocol/Strategy pattern decouples delivery from Svix. `SvixProvider` for production, `LogOnlyProvider` for development. Swap providers by implementing the protocol.
- [x] **Subscription filtering** — Notify-mode subscriptions with filters: chains, senders, recipients, min_amount, agent_class.

### API
- [x] **Endpoint CRUD** — Register, list, get, update, deactivate endpoints (`/api/v1/endpoints`)
- [x] **Subscription CRUD** — Create, list, remove subscriptions (`/api/v1/endpoints/{id}/subscriptions`)
- [x] **Event history** — List events with cursor pagination, filter by type/chain/endpoint (`/api/v1/events`)
- [x] **Goldsky ingestion** — Batch + single event ingest with HMAC auth (`/api/v1/ingest`)
- [x] **Stats endpoint** — Processing counts, active endpoints, last event timestamp (`/api/v1/stats`)

### Security
- [x] **API key authentication** — `tw_` prefixed keys, SHA-256 hashed storage, Bearer token auth. Keys shown once on creation.
- [x] **API key rotation** — `POST /endpoints/{id}/rotate-key` with 24h grace period. Both old and new keys valid during transition.
- [x] **Goldsky webhook auth** — HMAC Bearer token verification. Enforced in production (500 if secret missing).
- [x] **Rate limiting** — Per-key rate limits via slowapi. Ingest: 100 req/min, CRUD: 30 req/min. 429 with Retry-After header.

### Monitoring & Observability
- [x] **Structured logging** — structlog with JSON output in production, colored console in dev. Context propagation via `bind_contextvars` (request_id, tx_hash, chain_id).
- [x] **Request correlation** — Auto-generated request IDs, honors client `X-Request-ID`.
- [x] **Deep health check** — `GET /health/detailed` probes Supabase, webhook provider, identity resolver. Per-component status.
- [x] **Readiness endpoint** — `GET /ready` returns 503 until startup completes. Zero-downtime deploys on Railway.
- [x] **Pipeline timing** — Per-stage latency (decode, dedup, finality, identity, policy, dispatch) logged as structured fields.

### Infrastructure
- [x] **Single-source versioning** — `tripwire/__init__.py` is the source of truth, read by pyproject.toml via hatch.
- [x] **Centralized logging config** — `setup_logging()` called before any logger creation. Quiets noisy third-party loggers.
- [x] **Dockerfile** — Multi-stage build, non-root user, health check.
- [x] **docker-compose** — Single-service config (Redis removed — Supabase handles everything).
- [x] **Repository pattern** — Clean separation: `EndpointRepository`, `EventRepository`, `NonceRepository`, `WebhookDeliveryRepository`.
- [x] **7 SQL migrations** — Incremental schema evolution with IF EXISTS guards.

### SDK
- [x] **Python SDK** (`sdk/tripwire_sdk/`) — Async client with full API coverage: endpoint management, event listing, ingestion, webhook verification.
- [x] **SDK types** — Pydantic models for all API responses.
- [x] **Webhook verification** — `verify_webhook()` helper for SDK consumers.

### Testing
- [x] **45 unit tests** — Decoder, dispatcher, finality, middleware, nonce repo, policy engine, processor, routes.
- [x] **6 monitoring tests** — Health check, readiness, stats endpoint.
- [x] **5 rate limiting tests** — Per-key limits, 429 responses, independent buckets.
- [x] **6 key rotation tests** — Rotation flow, grace period, expired key rejection.
- [x] **5 integration tests** — Full pipeline e2e: register→ingest→verify, dedup, auth roundtrip, policy rejection, notify mode.

### Documentation
- [x] **README** — Full overview with badges, architecture diagram, quickstart.
- [x] **ABOUT.md** — Vision and problem statement.
- [x] **CLAUDE.md** — AI context for development.
- [x] **API reference** — Endpoints, webhook payload format.
- [x] **Guides** — Getting started, deployment, configuration, Goldsky setup, webhook verification.
- [x] **Architecture overview** — Layer diagram and data flow.
- [x] **SDK docs** — Python SDK usage guide.

---

## Remaining for v1 Production Launch

### Pre-Launch (before first user)
- [ ] **Run migrations on Supabase** — Execute 001-007 against production database
- [ ] **Configure Goldsky pipeline** — Deploy the Mirror/Turbo pipeline config to stream USDC events into Supabase
- [ ] **Set production env vars** — SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, SVIX_API_KEY, GOLDSKY_WEBHOOK_SECRET, RPC URLs
- [ ] **Deploy to Railway** — Push Docker image, configure health check (`/ready`), set env vars
- [ ] **Verify Svix webhook delivery** — End-to-end test with a real endpoint receiving webhooks
- [ ] **CORS lockdown** — Replace `allow_origins=["*"]` with actual allowed domains

### Post-Launch (iterative)
- [ ] **CI/CD pipeline** — GitHub Actions for: test on push, Docker build, deploy to Railway
- [ ] **TypeScript/JavaScript SDK** — For frontend and Node.js consumers
- [ ] **Webhook retry dashboard** — Svix portal integration for endpoint owners to see delivery history
- [ ] **Multi-chain expansion** — Add Polygon, Optimism, Avalanche USDC contracts
- [ ] **Batch processing optimization** — Parallel event processing within a batch (currently sequential)
- [ ] **OpenAPI schema export** — Auto-generate and publish API spec from FastAPI
- [ ] **Usage metering** — Track per-endpoint event counts for billing/quotas
- [ ] **Alerting rules** — Configure Railway alerts for error rate spikes, high latency, health check failures

---

## Architecture

```
L0  Chain          Base / Ethereum / Arbitrum (ERC-3009 transfers)
                            |
L1  Indexing        Goldsky Mirror/Turbo → Supabase PostgreSQL
                            |
L2  Middleware       TripWire FastAPI
                     ├── Decode (ERC-3009)
                     ├── Dedup (nonce)
                     ├── Finality (block confirmations)
                     ├── Identity (ERC-8004)
                     ├── Policy (rules engine)
                     └── Route (endpoint matching)
                            |
                    ┌───────┴───────┐
L3  Delivery    Svix (Execute)   Supabase Realtime (Notify)
                    |                       |
L4  Application  Developer API      Client WebSocket
```

## File Structure

```
tripwire/
├── main.py                    # App entry point, lifespan, health/ready
├── __init__.py                # Version (0.1.0)
├── api/
│   ├── auth.py                # API key generation, hashing, dual-key auth
│   ├── middleware.py           # Request logging, correlation IDs
│   ├── ratelimit.py           # slowapi rate limiting config
│   ├── policies/engine.py     # Policy evaluation rules
│   └── routes/
│       ├── endpoints.py       # Endpoint CRUD + key rotation
│       ├── events.py          # Event history + pagination
│       ├── ingest.py          # Goldsky webhook receiver
│       ├── stats.py           # Processing stats
│       └── subscriptions.py   # Notify-mode subscriptions
├── config/
│   ├── logging.py             # structlog setup
│   └── settings.py            # pydantic-settings config
├── db/
│   ├── client.py              # Supabase singleton
│   ├── repositories/          # endpoints, events, nonces, webhooks
│   └── migrations/            # 001-007 SQL migrations
├── identity/
│   ├── resolver.py            # ERC-8004 identity resolution
│   └── reputation.py          # Reputation scoring
├── ingestion/
│   ├── decoder.py             # ERC-3009 event decoding
│   ├── finality.py            # Block confirmation tracking
│   ├── pipeline.py            # Goldsky Mirror config
│   └── processor.py           # Pipeline orchestrator
├── notify/
│   └── realtime.py            # Supabase Realtime notifier
├── types/
│   └── models.py              # All Pydantic models
└── webhook/
    ├── dispatcher.py          # Webhook dispatch orchestrator
    ├── provider.py            # WebhookProvider Protocol + implementations
    ├── svix_client.py         # Svix SDK wrapper
    └── verify.py              # HMAC verification

sdk/tripwire_sdk/
├── client.py                  # Async API client
├── types.py                   # SDK Pydantic models
└── verify.py                  # Webhook verification helper

tests/                         # 67 tests
├── conftest.py
├── integration/test_pipeline.py
├── test_decoder.py
├── test_dispatcher.py
├── test_finality.py
├── test_key_rotation.py
├── test_middleware.py
├── test_monitoring.py
├── test_nonce_repo.py
├── test_policy_engine.py
├── test_processor.py
├── test_ratelimit.py
└── test_routes.py
```

## Tech Stack

| Layer | Technology | Purpose |
|-------|-----------|---------|
| Runtime | Python 3.11+ | Core language |
| API | FastAPI + Uvicorn | HTTP framework |
| Database | Supabase (PostgreSQL) | Managed DB + Auth + Realtime |
| Webhooks | Svix | Delivery, retries, HMAC, DLQ |
| Indexing | Goldsky Mirror/Turbo | Blockchain → Supabase streaming |
| RPC | httpx | Raw JSON-RPC to chains |
| ABI | eth-abi | ERC-3009 event decoding |
| Validation | Pydantic v2 | Input/output models |
| Logging | structlog | Structured JSON logging |
| Rate Limiting | slowapi | Per-key request throttling |
| Deployment | Docker + Railway | Container hosting |
