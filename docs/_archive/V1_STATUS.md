# TripWire v1 — Status & Roadmap

> Last updated: 2026-03-16

## v1 Summary

TripWire v1 is the complete x402 execution middleware — the infrastructure layer between onchain ERC-3009 micropayments and application execution. Two delivery modes: **Execute** (Convoy webhook delivery) and **Notify** (Supabase Realtime push).

**67 tests passing** | **29 source modules** | **19 DB migrations** | **Python SDK** | **MCP server (8 tools)** | **Redis Streams event bus**

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
- [x] **Execute mode (Convoy)** — Webhook delivery with retries, HMAC signing, DLQ. Convoy project + endpoint created on registration.
- [x] **Notify mode (Supabase Realtime)** — Events inserted into `realtime_events` table, pushed to subscribed clients via WebSocket.
- [x] **WebhookProvider abstraction** — Protocol/Strategy pattern decouples delivery from Convoy. `ConvoyProvider` for production, `LogOnlyProvider` for development. Swap providers by implementing the protocol.
- [x] **Subscription filtering** — Notify-mode subscriptions with filters: chains, senders, recipients, min_amount, agent_class.

### API
- [x] **Endpoint CRUD** — Register, list, get, update, deactivate endpoints (`/api/v1/endpoints`)
- [x] **Subscription CRUD** — Create, list, remove subscriptions (`/api/v1/endpoints/{id}/subscriptions`)
- [x] **Event history** — List events with cursor pagination, filter by type/chain/endpoint (`/api/v1/events`)
- [x] **Goldsky ingestion** — Batch + single event ingest with HMAC auth (`/api/v1/ingest`)
- [x] **Stats endpoint** — Processing counts, active endpoints, last event timestamp (`/api/v1/stats`)

### Security
- [x] **SIWE wallet authentication** — EIP-191 signature-based auth with 5 headers (X-TripWire-Address, X-TripWire-Signature, X-TripWire-Nonce, X-TripWire-Issued-At, X-TripWire-Expiration). Server-issued nonces stored in Redis with 5-minute TTL, consumed atomically on first use.
- [x] **MCP 3-tier authentication** — PUBLIC (no auth for discovery), SIWX (wallet signature for free tools), X402 (per-call micropayment for paid tools).
- [x] **Goldsky webhook auth** — HMAC Bearer token verification. Enforced in production (500 if secret missing).
- [x] **Facilitator webhook auth** — Separate `FACILITATOR_WEBHOOK_SECRET` for the `/ingest/facilitator` endpoint.
- [x] **Rate limiting** — Per-wallet rate limits via slowapi. Ingest: 100 req/min, CRUD: 30 req/min. 429 with Retry-After header.

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
- [x] **docker-compose** — TripWire + Convoy stack (convoy-server, convoy-worker, convoy-postgres, convoy-redis).
- [x] **Repository pattern** — Clean separation: `EndpointRepository`, `EventRepository`, `NonceRepository`, `TriggerRepository`, `WebhookDeliveryRepository`.
- [x] **19 SQL migrations** — Incremental schema evolution with IF EXISTS guards (001-007 core, 008-013 triggers/MCP, 014-019 event bus/archival).

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

### Trigger Registry & MCP
- [x] **Dynamic trigger system** — Create triggers for any EVM event via MCP or API, no deploy needed. Triggers stored in DB with owner_address, event_signature, chain_ids, filter_rules, contract_address.
- [x] **MCP server** — 8 tools over JSON-RPC 2.0 transport at `/mcp/`: register_middleware, create_trigger, list_triggers, delete_trigger, list_templates, activate_template, get_trigger_status, search_events.
- [x] **MCP 3-tier auth** — PUBLIC (no auth), SIWX (wallet signature, identity-gated), X402 (per-call micropayment). ERC-8004 reputation gating per tool.
- [x] **x402 Bazaar** — Agent service discovery via `/.well-known/x402-manifest.json`. Template catalog with install counts.
- [x] **Trigger worker pool** — TriggerIndex (in-memory O(1) lookup by topic0, 10s refresh), TriggerWorker (XREADGROUP consumer loop), WorkerPool (round-robin stream partitioning, auto-restart on crash).
- [x] **JMESPath filter engine** — Filter rules on decoded event fields using JMESPath expressions.

### Event Bus (Redis Streams)
- [x] **Partitioned event bus** — Feature-flagged via `EVENT_BUS_ENABLED`. Streams keyed by `tripwire:events:{topic0}`. Consumer group: `trigger-workers`.
- [x] **DLQ stream** — `tripwire:dlq` for permanently failed events after 5 retry attempts. Includes source stream, message ID, raw log, error count, and timestamp.
- [x] **Safety mechanisms** — Stream cap (500 max), per-worker stream cap (100), batch size limit (1000 logs), exponential backoff on consume errors, batched ACK via Redis pipeline.
- [x] **Graceful startup degradation** — Event bus worker pool failure during startup is logged but does not crash the application.
- [x] **NOGROUP recovery** — Automatic consumer group re-creation on NOGROUP errors with cache invalidation.

### Ingestion Enhancements
- [x] **Facilitator endpoint** — `POST /api/v1/ingest/facilitator` for x402 pre-settlement events before on-chain submission. Fast path: ~100ms target latency.
- [x] **Event-endpoints join table** — Events can match multiple endpoints. Migration 014 adds the `event_endpoints` join table.
- [x] **topic0 column** — Migration 017 adds `topic0` column for event routing.
- [x] **Nonce reorg support** — Migration 018 adds reorg tracking to nonce deduplication.
- [x] **Nonce archival** — Migration 019 adds archival support for expired nonces.
- [x] **Consolidated RPC clients** — Single httpx-based RPC client, no web3.py dependency.

### Architecture Review Fixes
- [x] **Protocol compliance** — ERC-3009 and ERC-8004 protocol handling hardened.
- [x] **Resilience improvements** — Graceful degradation, retry caps, DLQ, stream caps.
- [x] **webhook_secret not persisted** — Generated and returned once at endpoint creation, never stored in DB.
- [x] **Endpoint model cleanup** — Removed `api_key_hash`, `old_api_key_hash`, `svix_*` fields. Added `owner_address`, `registration_tx_hash`, `registration_chain_id`, `convoy_project_id`, `convoy_endpoint_id`.

### Migrations (014-019)
- [x] **014** — `event_endpoints` join table for many-to-many event-endpoint mapping.
- [x] **015** — Template install count fix.
- [x] **016** — Drop `webhook_secret` column from endpoints table.
- [x] **017** — Add `topic0` column for event routing.
- [x] **018** — Nonce reorg support.
- [x] **019** — Nonce archival support.

### Documentation
- [x] **README** — Full overview with badges, architecture diagram, quickstart.
- [x] **ABOUT.md** — Vision and problem statement.
- [x] **CLAUDE.md** — AI context for development.
- [x] **API reference** — Endpoints, webhook payload format, MCP tools, x402 Bazaar manifest.
- [x] **Guides** — Getting started, deployment, configuration, Goldsky setup, webhook verification.
- [x] **Architecture overview** — Layer diagram and data flow, updated for trigger registry and event bus.
- [x] **SDK docs** — Python SDK usage guide.

---

## Remaining for v1 Production Launch

### Pre-Launch (before first user)
- [ ] **Run migrations on Supabase** — Execute 001-019 against production database
- [ ] **Configure Goldsky pipeline** — Deploy the Turbo pipeline config with webhook sink to deliver USDC events to TripWire's ingest endpoint
- [ ] **Set production env vars** — SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY, CONVOY_API_KEY, GOLDSKY_WEBHOOK_SECRET, FACILITATOR_WEBHOOK_SECRET, EVENT_BUS_ENABLED, EVENT_BUS_WORKERS, RPC URLs
- [ ] **Deploy to Railway** — Push Docker image, configure health check (`/ready`), set env vars
- [ ] **Verify Convoy webhook delivery** — End-to-end test with a real endpoint receiving webhooks
- [ ] **CORS lockdown** — Replace `allow_origins=["*"]` with actual allowed domains

### Post-Launch (iterative)
- [ ] **CI/CD pipeline** — GitHub Actions for: test on push, Docker build, deploy to Railway
- [ ] **TypeScript/JavaScript SDK** — For frontend and Node.js consumers
- [ ] **Webhook retry dashboard** — Convoy portal integration for endpoint owners to see delivery history
- [ ] **Multi-chain expansion** — Add Polygon, Optimism, Avalanche USDC contracts
- [x] **Batch processing optimization** — Redis Streams event bus with partitioned workers for horizontal scaling (feature-flagged via `EVENT_BUS_ENABLED`)
- [ ] **OpenAPI schema export** — Auto-generate and publish API spec from FastAPI
- [ ] **Usage metering** — Track per-endpoint event counts for billing/quotas
- [ ] **Alerting rules** — Configure Railway alerts for error rate spikes, high latency, health check failures

---

## Architecture

```
L0  Chain          Base / Ethereum / Arbitrum (ERC-3009 transfers)
                            |
L1  Indexing        Goldsky Turbo → Webhook POST to TripWire /ingest
                            |
                   [Redis Streams event bus (optional, feature-flagged)]
                            |
L2  Middleware       TripWire FastAPI
                     ├── Decode (ERC-3009 + generic EVM events)
                     ├── Dedup (nonce)
                     ├── Finality (block confirmations)
                     ├── Identity (ERC-8004)
                     ├── Policy (rules engine)
                     ├── Trigger matching (topic0 → TriggerIndex)
                     └── Route (endpoint matching via event_endpoints)
                            |
                    ┌───────┴───────┐
L3  Delivery    Convoy (Execute)   Supabase Realtime (Notify)
                    |                       |
L4  Application  Developer API      Client WebSocket
                            |
L5  MCP          /mcp (8 tools, 3-tier auth: PUBLIC/SIWX/X402)
```

## File Structure

```
tripwire/
├── main.py                    # App entry point, lifespan, health/ready
├── __init__.py                # Version (0.1.0)
├── api/
│   ├── auth.py                # SIWE wallet authentication (EIP-191 signatures)
│   ├── middleware.py           # Request logging, correlation IDs
│   ├── ratelimit.py           # slowapi rate limiting config
│   ├── policies/engine.py     # Policy evaluation rules
│   └── routes/
│       ├── endpoints.py       # Endpoint CRUD
│       ├── events.py          # Event history + pagination
│       ├── ingest.py          # Goldsky + facilitator webhook receivers
│       ├── stats.py           # Processing stats
│       └── subscriptions.py   # Notify-mode subscriptions
├── config/
│   ├── logging.py             # structlog setup
│   └── settings.py            # pydantic-settings config
├── db/
│   ├── client.py              # Supabase singleton
│   ├── archival.py            # Nonce archival
│   ├── repositories/          # endpoints, events, nonces, triggers, webhooks
│   └── migrations/            # 001-019 SQL migrations
├── identity/
│   ├── resolver.py            # ERC-8004 identity resolution
│   └── reputation.py          # Reputation scoring
├── ingestion/
│   ├── decoder.py             # ERC-3009 event decoding
│   ├── event_bus.py           # Redis Streams publish/consume/ack/claim
│   ├── finality.py            # Block confirmation tracking
│   ├── finality_poller.py     # Background finality polling loop
│   ├── pipeline.py            # Goldsky Turbo config
│   ├── processor.py           # Pipeline orchestrator
│   └── trigger_worker.py      # TriggerIndex, TriggerWorker, WorkerPool
├── mcp/
│   ├── server.py              # MCP JSON-RPC server with 3-tier auth
│   ├── tools.py               # 8 MCP tool implementations
│   ├── auth.py                # PUBLIC/SIWX/X402 auth handlers
│   └── types.py               # AuthTier, MCPAuthContext, ToolDef
├── notify/
│   └── realtime.py            # Supabase Realtime notifier
├── types/
│   └── models.py              # All Pydantic models
├── utils/                     # Shared utilities
└── webhook/
    ├── dispatcher.py          # Webhook dispatch orchestrator
    ├── provider.py            # WebhookProvider Protocol + implementations
    ├── convoy_client.py       # Convoy REST API via httpx wrapper
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
| Webhooks | Convoy self-hosted | Delivery, retries, HMAC, DLQ |
| Indexing | Goldsky Turbo (webhook sink) | Blockchain → TripWire via webhooks |
| RPC | httpx | Raw JSON-RPC to chains |
| ABI | eth-abi | ERC-3009 event decoding |
| Validation | Pydantic v2 | Input/output models |
| Logging | structlog | Structured JSON logging |
| Event Bus | Redis Streams | Partitioned event processing (optional) |
| MCP | JSON-RPC 2.0 | AI agent interface (8 tools, 3-tier auth) |
| Rate Limiting | slowapi | Per-wallet request throttling |
| Deployment | Docker + Railway | Container hosting |
