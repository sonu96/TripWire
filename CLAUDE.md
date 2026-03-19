# TripWire — Programmable Onchain Event Triggers for AI Agents

## What This Is
TripWire is a programmable onchain event trigger platform for AI agents — the infrastructure layer between onchain events and application execution. x402 payment webhooks are the first use case. Two modes: Notify (Supabase Realtime push) and Execute (Convoy webhook delivery).

## Final Architecture Stack
- **Runtime**: Python 3.11+
- **API**: FastAPI + Uvicorn
- **Database**: Supabase (managed PostgreSQL)
- **Notify Mode**: Supabase Realtime (clients subscribe to DB changes)
- **Webhook Delivery**: Convoy self-hosted + direct httpx fast path
- **Blockchain Indexing**: Goldsky Turbo → delivers events via webhook to TripWire's ingest endpoint
- **Blockchain RPC**: httpx (raw JSON-RPC calls, no web3.py)
- **ABI Decoding**: eth-abi (lightweight, only if needed for raw data)
- **Validation**: Pydantic v2
- **Logging**: structlog
- **HTTP Client**: httpx (async)

## Architecture Layers
- L0 Chain: Base / Ethereum / Arbitrum (ERC-3009 transfers)
- L1 Indexing: Goldsky Turbo → delivers events via webhook to TripWire's /ingest endpoint; optionally routes through Redis Streams event bus for horizontal scaling
- L2 Middleware: TripWire FastAPI (verification, deduplication, identity, policy engine)
- L3 Delivery: Convoy + direct POST (webhook delivery with retries, HMAC signing, DLQ)
- L4 Application: Developer's API (executes business logic on verified webhook)
- L5 MCP: Agent interface (MCP tools for trigger management, middleware registration)

## Key Directories
- `tripwire/ingestion/` — Goldsky pipeline config, ERC-3009 event processing, finality tracking, event_bus.py (Redis Streams pub/sub), trigger_worker.py (TriggerIndex, TriggerWorker, WorkerPool)
- `tripwire/api/` — FastAPI routes, endpoint registration, subscription management
- `tripwire/auth/` — SIWE (EIP-4361) message construction, signature verification, timestamp validation (single source of truth)
- `tripwire/webhook/` — Convoy integration, webhook dispatch
- `tripwire/identity/` — ERC-8004 identity resolution (mock for MVP), reputation scoring
- `tripwire/db/` — Supabase client, repositories, SQL migrations
- `tripwire/types/` — Shared Pydantic models
- `tripwire/config/` — Settings via pydantic-settings
- `tripwire/mcp/` — MCP server, tool handlers, agent middleware registration
- `sdk/` — tripwire-sdk Python package
- `tests/` — Unit and integration tests

## Key Protocols
- **x402**: HTTP 402 micropayment protocol using ERC-3009 transferWithAuthorization. V2 migration complete: `PAYMENT-SIGNATURE` is the only accepted header (`PAYMENT-REQUIRED` and `PAYMENT-RESPONSE` are handled by the x402 SDK automatically). The v1 manifest (`/.well-known/x402-manifest.json`) returns 410 Gone. `GET /discovery/resources` is the V2 Bazaar endpoint
- **ERC-3009**: transferWithAuthorization standard for gasless USDC transfers
- **ERC-8004**: Onchain AI agent identity registry (went mainnet Jan 29 2026)
- **TWSS-1**: TripWire Skill Spec — execution-aware skill standard defining lifecycle states (provisional/confirmed/finalized/reorged), three-layer gating (can_pay/can_trust/is_safe), and two-phase execution (prepare/commit). See docs/SKILL-SPEC.md
- **Trigger Registry**: Dynamic trigger system — create triggers for any EVM event via MCP or API, no deploy needed
- **x402 Bazaar**: Agent service discovery via /discovery/resources (V2) + /.well-known/tripwire-skill-spec.json. V1 manifest (/.well-known/x402-manifest.json) returns 410 Gone

## Decoder Phases (C1-C3)
- **C1 (Implemented)**: Decoder protocol + DecodedEvent envelope + ERC3009Decoder + AbiGenericDecoder in `tripwire/ingestion/decoders/`. AbiGenericDecoder performs best-effort payment field extraction (`_extract_payment_fields`) so C3 payment gating works for dynamic triggers too.
- **C2 (Implemented)**: Unified processing loop — single code path for ERC-3009 and dynamic triggers via `_process_unified()`. Feature-flagged: `UNIFIED_PROCESSOR=true` (default false). Dynamic triggers gain finality (via `check_finality_generic()`), policy, execution state (nested `ExecutionBlock`), notify mode, tracing, metrics.
- **C3 (Implemented)**: Per-trigger payment gating — `require_payment`, `payment_token`, `min_payment_amount` on Trigger model. `payment_amount/token/from/to` on DecodedEvent. Migration 024.

## Key Models
- **`ExecutionBlock`** (`tripwire/types/models.py`): Nested execution metadata — `state` (ExecutionState), `safe_to_execute` (bool), `trust_source` (TrustSource), `finality` (FinalityData | None). Used as the `execution` field on `WebhookPayload`.
- **`derive_execution_metadata()`** returns `ExecutionBlock` (not a tuple). Derives execution state from event type and finality data.
- **`check_finality_generic()`** (`tripwire/ingestion/finality.py`): Finality check using raw values (chain_id, block_number, tx_hash) — no `ERC3009Transfer` required. `check_finality()` delegates to it.
- **Trigger** has `required_agent_class` (str | None) and `version` (str, default "1.0.0"). Migration 025.
- **TriggerTemplate** has `version` (str, default "1.0.0"). Migration 025.
- **Finality field**: `required_confirmations` (not `required`) on `FinalityData`.

## Webhook Delivery (Convoy + direct httpx fast path)
- Dual-path architecture: direct httpx POST for low-latency fast path; Convoy for managed delivery with retries, HMAC signing, and DLQ
- Convoy self-hosted via docker-compose (convoy-server + convoy-worker + convoy-postgres + convoy-redis)
- Convoy handles: exponential backoff retries, signature signing, delivery logs, endpoint management
- Direct httpx path: used when latency is critical and at-least-once delivery can be handled by the caller
- TripWire wraps both paths to add: policy evaluation, identity enrichment, event deduplication
- docker-compose services: `convoy-server` (port 5005), `convoy-worker`, `convoy-postgres` (port 5433), `convoy-redis` (port 6380)

## Event Bus (Redis Streams)
- Optional partitioned event bus for horizontal scaling of event processing. Feature-flagged via `EVENT_BUS_ENABLED` (default: false).
- Config: `EVENT_BUS_ENABLED` (bool, default false), `EVENT_BUS_WORKERS` (int, default 3)
- Data flow: Goldsky → /ingest endpoint → `publish_batch()` → Redis Streams (partitioned by topic0) → TriggerWorkers → `EventProcessor.process_event()` → Convoy/webhook delivery
- Key components (`tripwire/ingestion/`):
  - `event_bus.py` — Redis Streams publish/consume/ack/claim primitives. Streams keyed by `tripwire:events:{topic0}`. Consumer group: `trigger-workers`. Max stream length: 100k. XAUTOCLAIM for stale messages idle >30s. `_known_groups` in-memory set caches which streams already have their consumer group created, avoiding redundant Redis calls. NOGROUP recovery: on NOGROUP error during consume or claim, invalidates `_known_groups` cache for the affected stream and re-creates the consumer group before retrying. topic0 validation: strict regex `^0x[0-9a-f]{64}$` (lowercase hex only); invalid topics route to `tripwire:events:unknown`. `_known_stream_keys` in-memory set tracks all distinct streams seen; used by `_check_stream_cap()` to enforce `MAX_STREAMS` at publish time (both `publish_event` and `publish_batch`).
  - `trigger_worker.py` — `TriggerIndex` (in-memory O(1) lookup by topic0, refreshes from DB every 10s with lock-guarded double-check), `TriggerWorker` (XREADGROUP consumer loop with periodic stale-message claiming, batched ACK via Redis pipeline), `WorkerPool` (round-robin stream partitioning, 30s stream discovery loop for new event types, graceful shutdown with 30s timeout, `_on_worker_done` callback auto-restarts crashed workers unless pool is shutting down)
- Safety mechanisms:
  - **DLQ stream**: `tripwire:dlq` — permanently failed events (after `_MAX_RETRIES` = 5 attempts) are written here with source stream, message ID, raw log, error count, and timestamp before being ACKed from the source stream
  - **Retry cap**: per-message failure count tracked in `_failure_counts` dict keyed by `(stream_key, message_id) → (count, timestamp)`; after 5 failures the message is sent to DLQ and ACKed. Stale entries are cleaned every 100 iterations (entries older than 300s are pruned to prevent memory leaks).
  - **Stream cap**: `MAX_STREAMS = 500` enforced at BOTH publish time (`_check_stream_cap` in event_bus.py using `_known_stream_keys` set) AND discovery time (WorkerPool.start and _discover_streams_loop). Excess streams at publish time are routed to `tripwire:events:unknown`.
  - **Per-worker stream cap**: `_MAX_STREAMS_PER_WORKER = 100` prevents a single worker from being overloaded
  - **Batch size limit**: /ingest endpoint rejects payloads with >1000 logs (HTTP 400)
  - **Exponential backoff**: on consecutive consume errors, workers sleep `min(2^n, 60)` seconds before retrying
  - **Batched ACK**: successful and max-retried messages are ACKed together in a single Redis pipeline call
- Consumer groups provide at-least-once delivery: messages are ACKed only after successful processing; unacked messages are reclaimed by other workers via XAUTOCLAIM
- When `EVENT_BUS_ENABLED=false` (default), the /ingest endpoint processes events synchronously through EventProcessor — no Redis dependency required
- **Graceful startup degradation**: if the event bus worker pool fails to start during app lifespan, the error is logged but the application continues running without event bus workers (the app does not crash)

## Database (Supabase)
- Tables: endpoints, subscriptions, events, nonces, webhook_deliveries, audit_log
- Nonce deduplication via unique constraint on (chain_id, nonce, authorizer)
- Use supabase-py client with service_role key
- SQL migrations in tripwire/db/migrations/

## Conventions
- Pydantic v2 for all input/output validation
- async/await throughout (FastAPI + httpx)
- All amounts in smallest unit (USDC = 6 decimals)
- structlog for structured JSON logging
- No web3.py — use httpx for raw JSON-RPC + eth-abi for decoding
- Multi-chain: x402 payment networks configurable via `x402_networks` list setting (CAIP-2 format, default: Base)
- MCP tools follow the Model Context Protocol spec — mounted at /mcp; MCP payment auth uses `TripWirePaymentHooks` pattern (not manual verify/settle)
