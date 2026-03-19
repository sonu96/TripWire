# TripWire Operations Guide

## 1. Configuration Reference

All configuration is loaded from environment variables (or a `.env` file) via pydantic-settings. Secret values use `SecretStr` and are never logged.

### App

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `APP_ENV` | str | `production` | No | `development` or `production`. Controls logging format and dev features. |
| `APP_PORT` | int | `3402` | No | HTTP port for the FastAPI server. |
| `APP_BASE_URL` | str | `http://localhost:3402` | No | Public base URL used in webhook callbacks. |
| `LOG_LEVEL` | str | `info` | No | Minimum log level: `debug`, `info`, `warning`, `error`, `critical`. |
| `CORS_ALLOWED_ORIGINS` | list[str] | `["http://localhost:3000", "http://localhost:3402"]` | No | JSON array of allowed CORS origins. |

### Supabase

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `SUPABASE_URL` | str | `""` | Yes | Supabase project URL. |
| `SUPABASE_ANON_KEY` | str | `""` | No | Public anon key (safe to expose client-side). |
| `SUPABASE_SERVICE_ROLE_KEY` | SecretStr | `""` | Yes | Service role key. Bypasses RLS. Never expose. |

### Convoy / Webhooks

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `CONVOY_API_KEY` | SecretStr | `""` | Yes | Convoy project API key. |
| `CONVOY_URL` | str | `http://localhost:5005` | No | Convoy self-hosted base URL. |
| `WEBHOOK_SIGNING_SECRET` | SecretStr | `""` | No | Default HMAC secret for webhook payloads. Can be overridden per endpoint. |

### Goldsky Turbo (Event Indexing)

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `GOLDSKY_API_KEY` | SecretStr | `""` | No | API key for the Goldsky CLI (`goldsky turbo apply`, `goldsky turbo status`, etc.). Used for pipeline deployment and management only. |
| `GOLDSKY_PROJECT_ID` | str | `""` | No | Goldsky project identifier. Passed to the CLI via `--project-id`. |
| `GOLDSKY_WEBHOOK_SECRET` | SecretStr | `""` | Yes | Validates inbound Goldsky Turbo webhooks at `/api/v1/ingest/goldsky`. Must be set in non-development envs or ingest rejects requests. |

### Goldsky Edge RPC (Finality + Identity)

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `GOLDSKY_EDGE_API_KEY` | SecretStr | `""` | No | Bearer token for Goldsky Edge managed RPC endpoints. Sent as `Authorization: Bearer <key>` on every JSON-RPC call. |
| `BASE_RPC_URL` | str | `""` | No | Goldsky Edge RPC endpoint for Base. Used by the finality poller and identity resolver. |
| `ETHEREUM_RPC_URL` | str | `""` | No | Goldsky Edge RPC endpoint for Ethereum. |
| `ARBITRUM_RPC_URL` | str | `""` | No | Goldsky Edge RPC endpoint for Arbitrum. |

### x402 Payment Gating

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `X402_FACILITATOR_URL` | str | `https://x402.org/facilitator` | No | x402 facilitator endpoint. |
| `X402_REGISTRATION_PRICE` | str | `$1.00` | No | Price charged to register an endpoint via x402. |
| `X402_NETWORK` | str | `eip155:8453` | No | CAIP-2 chain ID (Base mainnet). |
| `TRIPWIRE_TREASURY_ADDRESS` | str | `""` | Yes | Wallet that receives USDC registration payments. Enables x402 payment gating when set. |

### Identity / Auth

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `AUTH_TIMESTAMP_TOLERANCE_SECONDS` | int | `300` | No | Max age (seconds) of a SIWE signature. |
| `REDIS_URL` | str | `redis://localhost:6379` | No | Redis for SIWE nonce storage, rate limiting, and event bus. |
| `SIWE_DOMAIN` | str | `tripwire.dev` | No | Domain for Sign-In with Ethereum messages. |
| `ERC8004_IDENTITY_REGISTRY` | str | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | No | CREATE2-deployed identity registry address. |
| `ERC8004_REPUTATION_REGISTRY` | str | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | No | CREATE2-deployed reputation registry address. |
| `IDENTITY_CACHE_TTL` | int | `300` | No | Seconds to cache identity lookups. |
| `FACILITATOR_WEBHOOK_SECRET` | SecretStr | `""` | No | Validates x402 facilitator callbacks. |

### Product Mode

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `PRODUCT_MODE` | str | `both` | No | `pulse` (generic triggers), `keeper` (x402 payments), or `both`. Controls which event handlers and features are active. |

### Session System (Keeper Only)

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `SESSION_ENABLED` | bool | `false` | No | Enable the Keeper session system. When true, `POST/GET/DELETE /api/v1/auth/session` endpoints become available. Requires Redis. |
| `SESSION_DEFAULT_TTL_SECONDS` | int | `900` | No | Default session lifetime (15 minutes). |
| `SESSION_MAX_TTL_SECONDS` | int | `1800` | No | Maximum session lifetime (30 minutes). |
| `SESSION_DEFAULT_BUDGET_USDC` | int | `10000000` | No | Default budget in smallest USDC units (10 USDC). |
| `SESSION_MAX_BUDGET_USDC` | int | `100000000` | No | Maximum budget (100 USDC). |

### Unified Processor (Phase C2)

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `UNIFIED_PROCESSOR` | bool | `false` | No | Enable unified processing loop for ERC-3009 and dynamic triggers. When true, both event types flow through `_process_unified()` — dynamic triggers gain finality checking, full policy evaluation, execution state metadata, notify mode, tracing, and metrics. See [TWSS-1 Skill Spec](SKILL-SPEC.md). |

### Event Bus (Redis Streams)

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `EVENT_BUS_ENABLED` | bool | `false` | No | Enable Redis Streams event bus for async event processing. |
| `EVENT_BUS_WORKERS` | int | `3` | No | Number of TriggerWorker consumers for the event bus. |

### PreConfirmed Sweeper

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `PRE_CONFIRMED_TTL_SECONDS` | int | `1800` | No | Seconds before a `pre_confirmed` event is marked as `payment.failed` (30 minutes). |
| `PRE_CONFIRMED_SWEEP_INTERVAL_SECONDS` | int | `300` | No | How often (seconds) the sweeper checks for stale events (5 minutes). |

### Resource Quotas

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `MAX_TRIGGERS_PER_WALLET` | int | `50` | No | Maximum active triggers per wallet. HTTP 429 on exceed. |
| `MAX_ENDPOINTS_PER_WALLET` | int | `20` | No | Maximum active endpoints per wallet. HTTP 429 on exceed. |

### Finality Poller

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `FINALITY_POLLER_ENABLED` | bool | `true` | No | Enable on-chain finality polling. |
| `FINALITY_POLL_INTERVAL_ARBITRUM` | int | `5` | No | Seconds between Arbitrum finality polls. |
| `FINALITY_POLL_INTERVAL_BASE` | int | `10` | No | Seconds between Base finality polls. |
| `FINALITY_POLL_INTERVAL_ETHEREUM` | int | `30` | No | Seconds between Ethereum finality polls. |

### Dead Letter Queue

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `DLQ_ENABLED` | bool | `true` | No | Enable DLQ processing. |
| `DLQ_POLL_INTERVAL_SECONDS` | int | `60` | No | How often (seconds) the DLQ handler polls Convoy for failed deliveries. |
| `DLQ_MAX_RETRIES` | int | `3` | No | Max retry attempts before marking a delivery as dead-lettered. |
| `DLQ_ALERT_WEBHOOK_URL` | str | `""` | No | URL to POST DLQ alerts when a delivery is dead-lettered. |

### Observability

| Variable | Type | Default | Required in prod | Description |
|---|---|---|---|---|
| `METRICS_BEARER_TOKEN` | str | `""` | No | Protects `/metrics` endpoint with Bearer auth when set. |
| `SENTRY_DSN` | SecretStr | `""` | No | Sentry DSN for error tracking. Requires `pip install tripwire[sentry]`. |
| `SENTRY_TRACES_SAMPLE_RATE` | float | `0.1` | No | Fraction of transactions sent to Sentry (0.0-1.0). |
| `OTEL_ENABLED` | bool | `false` | No | Enable OpenTelemetry distributed tracing. |
| `OTEL_ENDPOINT` | str | `""` | No | OTLP exporter endpoint (e.g., `http://localhost:4317`). |
| `OTEL_SERVICE_NAME` | str | `tripwire` | No | Service name reported to the tracing backend. |

### Production Validation

When `APP_ENV=production`, the application raises a `ValueError` at startup if any of these are missing: `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`, `CONVOY_API_KEY`, `TRIPWIRE_TREASURY_ADDRESS`.

---

## 2. Startup Sequence

The FastAPI lifespan manager (`tripwire/main.py`) runs the following steps in order. Each step logs a `*_ready` event on success.

**Important**: Goldsky Turbo pipelines are external infrastructure and are **not** started by the application lifespan. They must be deployed and running on Goldsky's platform before the app can receive ingest webhooks. See section 5 (Goldsky Pipeline Management).

1. **Sentry initialization** (before lifespan) -- If `SENTRY_DSN` is set, Sentry is initialized early to capture startup errors.
2. **structlog configuration** (before lifespan) -- `setup_logging()` is called before any logger is created.
3. **OpenTelemetry tracing** -- If `OTEL_ENABLED=true`, tracing is set up early so all spans are captured.
4. **Supabase client** -- Connects to the Supabase PostgreSQL database.
5. **Audit logger** -- Fire-and-forget writes to the `audit_log` table.
6. **Webhook provider** -- Creates `ConvoyProvider` (production) or `LogOnlyProvider` (dev without key).
7. **Goldsky webhook secret check** -- Warns if missing in non-development environments.
8. **Identity resolver** -- ERC-8004 resolver (production) or mock (development).
9. **Nonce repository** -- Nonce deduplication backed by Supabase.
10. **Realtime notifier** -- Notify-mode delivery via Supabase Realtime.
11. **Event processor** -- Wires the full ingestion-to-dispatch pipeline together.
12. **Event bus worker pool** (if `EVENT_BUS_ENABLED=true`) -- Initializes stream keys from Redis, creates and starts the WorkerPool. **Degrades gracefully**: if the worker pool fails to start, the error is logged but the application continues without event bus workers.
13. **Redis DLQ consumer** (if `EVENT_BUS_ENABLED=true`) -- Starts after the worker pool. Reads from `tripwire:dlq`, logs errors, fires alerts, trims stream. See Background Tasks section.
14. **DLQ handler** (if `DLQ_ENABLED=true` and Convoy is configured) -- Background poller for failed Convoy deliveries.
15. **Finality poller** (if `FINALITY_POLLER_ENABLED=true`) -- One asyncio task per chain for confirming pending events. Leader-elected via advisory lock 839201.
16. **PreConfirmedSweeper** (if Keeper mode active) -- Sweeps stale pre_confirmed events. Leader-elected via advisory lock 839202.
17. **Nonce archiver** -- Daily background task to move old nonces to archive. **Degrades gracefully**: if it fails to start, the error is logged and the app continues.
18. **Prometheus build info** -- Sets version and environment labels.
19. **Ready flag** -- `app.state.ready = True` signals readiness to the `/ready` probe.

### Shutdown Sequence

1. Stop worker pool (30s timeout; force-cancels tasks if exceeded)
2. Stop Redis DLQ consumer
3. Stop finality poller (cancels per-chain tasks)
4. Stop PreConfirmedSweeper
5. Stop nonce archiver
6. Stop DLQ handler
7. Close shared RPC HTTP client
8. Flush and shut down OpenTelemetry tracing

---

## 3. Middleware Stack

Middleware is applied in reverse declaration order (outermost runs first). The effective execution order for an inbound request is:

1. **x402 PaymentMiddlewareASGI** (conditional) -- Only active when `TRIPWIRE_TREASURY_ADDRESS` is set and the `x402` package is installed. Gates `POST /api/v1/endpoints` with a USDC micropayment. If the x402 package is missing, a warning is logged and the middleware is skipped.
2. **CORSMiddleware** -- Configured with `CORS_ALLOWED_ORIGINS`. Allows all methods and headers with credentials.
3. **SlowAPIMiddleware** -- Rate limiting via SlowAPI. Rate limit exceeded returns a handler-defined error response.
4. **RequestLoggingMiddleware** -- Logs method, path, status code, and duration for every request.

### Exception Handlers

- **PostgrestAPIError** -- Maps PostgreSQL error codes to HTTP status codes (23505 -> 409, 23503 -> 422, 23514 -> 422, 42501 -> 403, PGRST* -> 502, other -> 500).
- **httpx.ConnectError / httpx.TimeoutException** -- Returns 503 Service Unavailable.
- **Catch-all Exception** -- Reports to Sentry (if configured), logs the error, returns 500.

---

## 4. Background Tasks

### Finality Poller

**Leader-elected**: Uses Postgres advisory lock 839201 via `try_acquire_leader_lock()`. Safe for multi-instance deployment -- only one instance polls per cycle.

Spawns one asyncio task per supported chain. Each chain polls on its own cadence because finality semantics differ:

| Chain | Block time | Confirmations required | Poll interval (default) |
|---|---|---|---|
| Arbitrum | ~250ms | 1 | 5s |
| Base | 2s | 3 | 10s |
| Ethereum | 12s | 12 | 30s |

For each pending event, the poller fetches the current block number via JSON-RPC, computes confirmations, and promotes the event to `confirmed` once the required depth is reached. Also performs reorg detection by comparing stored block hashes against canonical hashes.

**Config knobs**: `FINALITY_POLLER_ENABLED`, `FINALITY_POLL_INTERVAL_ARBITRUM`, `FINALITY_POLL_INTERVAL_BASE`, `FINALITY_POLL_INTERVAL_ETHEREUM`.

### DLQ Handler

Polls Convoy for failed webhook deliveries across all active endpoints. Retries failed deliveries via `force_resend`. After `DLQ_MAX_RETRIES` attempts, marks the delivery as `dead_lettered` and fires an alert to `DLQ_ALERT_WEBHOOK_URL`.

**Config knobs**: `DLQ_ENABLED`, `DLQ_POLL_INTERVAL_SECONDS` (default 60), `DLQ_MAX_RETRIES` (default 3), `DLQ_ALERT_WEBHOOK_URL`.

Tracks retry counts in the `dlq_retry_count` column on `webhook_deliveries` (migration 021), so counts survive restarts. Registers with the health registry as `dlq_handler`.

### Nonce Archiver

Runs daily (every 86400 seconds). Calls the `archive_old_nonces` database function to move nonces older than 30 days to the `nonces_archive` table. Processes in batches of 5000.

**Hardcoded config** (in `tripwire/db/archival.py`): `_DEFAULT_AGE_DAYS=30`, `_DEFAULT_BATCH_SIZE=5000`, `_POLL_INTERVAL=86400`.

### PreConfirmedSweeper

Background task that marks stale `pre_confirmed` events as `payment.failed`. Runs every `PRE_CONFIRMED_SWEEP_INTERVAL_SECONDS` (default 300 = 5 minutes). Events older than `PRE_CONFIRMED_TTL_SECONDS` (default 1800 = 30 minutes) are swept. Dispatches `payment.failed` webhooks to linked endpoints.

**Keeper-only**: Only active when `PRODUCT_MODE` includes Keeper (`keeper` or `both`).

**Leader-elected**: Uses Postgres advisory lock 839202 via `try_acquire_leader_lock()`. Safe for multi-instance deployment -- only one instance sweeps per cycle. Non-leaders skip silently.

**Config knobs**: `PRE_CONFIRMED_TTL_SECONDS` (default 1800), `PRE_CONFIRMED_SWEEP_INTERVAL_SECONDS` (default 300).

### Redis DLQ Consumer

Background task (`tripwire/ingestion/dlq_consumer.py`) that reads from the `tripwire:dlq` Redis stream. Logs error details for each dead-lettered event, fires alerts (to `DLQ_ALERT_WEBHOOK_URL` if configured), and trims the stream. Only active when `EVENT_BUS_ENABLED=true`. Starts after the event bus worker pool in the startup sequence.

### Event Bus Worker Pool

See section 6 below.

---

## 5. Goldsky Pipeline Management

Goldsky Turbo pipelines are external infrastructure deployed to Goldsky's platform. They are **not** part of the TripWire application lifespan -- they must be deployed, started, and stopped separately using the Goldsky CLI (or the helper functions in `tripwire/ingestion/pipeline.py`).

### Pipeline Naming

Each pipeline follows the pattern `tripwire-{chain}-erc3009`:

| Chain | Pipeline name |
|---|---|
| Ethereum | `tripwire-ethereum-erc3009` |
| Base | `tripwire-base-erc3009` |
| Arbitrum | `tripwire-arbitrum-erc3009` |

### Lifecycle Commands

**Deploy** (creates or updates a pipeline from the generated YAML config):

```bash
goldsky turbo apply <config.yaml> --api-key $GOLDSKY_API_KEY --project-id $GOLDSKY_PROJECT_ID
```

Or programmatically: `tripwire.ingestion.pipeline.deploy_pipeline(chain_id)`

**Status** (check if a pipeline is running):

```bash
goldsky turbo status tripwire-base-erc3009 --api-key $GOLDSKY_API_KEY --project-id $GOLDSKY_PROJECT_ID
```

Or: `tripwire.ingestion.pipeline.get_pipeline_status(chain_id)`

**Stop** a running pipeline:

```bash
goldsky turbo stop tripwire-base-erc3009 --api-key $GOLDSKY_API_KEY --project-id $GOLDSKY_PROJECT_ID
```

Or: `tripwire.ingestion.pipeline.stop_pipeline(chain_id)`

**Start** a stopped pipeline:

```bash
goldsky turbo start tripwire-base-erc3009 --api-key $GOLDSKY_API_KEY --project-id $GOLDSKY_PROJECT_ID
```

Or: `tripwire.ingestion.pipeline.start_pipeline(chain_id)`

### Pipeline Config

Each pipeline config is generated by `build_pipeline_config()` in `tripwire/ingestion/pipeline.py`. It defines:

- **Source**: Goldsky raw_logs dataset for the target chain (e.g., `base.raw_logs`)
- **Transform**: SQL that JOINs `AuthorizationUsed` and `Transfer` events from the same transaction on the USDC contract, decoding both via `_gs_log_decode`
- **Sink**: Webhook delivery to `{APP_BASE_URL}/api/v1/ingest/goldsky` with the `GOLDSKY_WEBHOOK_SECRET` as a Bearer token

### Reference

- Pipeline config generation: `tripwire/ingestion/pipeline.py`
- Event decoding: `tripwire/ingestion/decoder.py`
- Ingest endpoint: `tripwire/api/routes/ingest.py`

---

## 6. Event Bus Operations

### When to Enable

Set `EVENT_BUS_ENABLED=true` when you need horizontal scaling of event processing. When disabled (the default), the `/ingest` endpoint processes events synchronously through `EventProcessor` with no Redis dependency.

### Redis Requirements

The event bus requires a Redis instance accessible via `REDIS_URL`. Redis is also used for SIWE nonce storage and rate limiting regardless of the event bus setting.

### Architecture

```
Goldsky -> /ingest -> publish_batch() -> Redis Streams (partitioned by topic0)
                                              |
                                    TriggerWorkers (N consumers)
                                              |
                                    EventProcessor.process_event()
                                              |
                                    Convoy / webhook delivery
```

### Worker Count

Controlled by `EVENT_BUS_WORKERS` (default 3). Streams are partitioned across workers via round-robin. Workers are named `worker-0`, `worker-1`, etc.

### Stream Caps

| Cap | Value | Scope | Behavior when exceeded |
|---|---|---|---|
| `MAX_STREAMS` | 500 | Global | Excess streams routed to `tripwire:events:unknown` |
| `MAX_STREAMS_PER_WORKER` | 100 | Per worker | New stream assignment rejected for that worker |
| `MAX_STREAM_LEN` | 100,000 | Per stream | Redis XADD with approximate MAXLEN trims old entries |

### Consumer Group

All workers share the consumer group `trigger-workers`. Messages are delivered via `XREADGROUP` with `BLOCK_MS=2000`. Stale messages idle for more than 30 seconds are reclaimed via `XAUTOCLAIM`.

### Process Event Timeout

Each event processed by a TriggerWorker has a 30-second timeout (`_PROCESS_TIMEOUT = 30.0` in `trigger_worker.py`). If `EventProcessor.process_event()` exceeds this timeout, the call is cancelled and the event is treated as a processing failure (subject to the retry/DLQ mechanism).

### Dead Letter Queue (Event Bus)

Distinct from the Convoy DLQ handler. Events that fail processing 5 times (`_MAX_RETRIES=5`) are written to the `tripwire:dlq` Redis stream with source stream, message ID, raw log, error count, and timestamp, then ACKed from the source stream.

### Stream Discovery

The WorkerPool runs a discovery loop every 30 seconds (`_STREAM_DISCOVERY_INTERVAL`) that scans Redis for new `tripwire:events:*` keys and assigns them to the least-loaded worker.

### Worker Restart

Crashed workers are automatically restarted with exponential backoff (base 2s, max 120s). The restart counter resets after 300 seconds of stability.

### Graceful Shutdown

The pool signals all workers to stop, then waits up to 30 seconds (`_SHUTDOWN_TIMEOUT`). Workers that don't finish within the timeout are force-cancelled.

### Monitoring Streams

Use `event_bus.get_stream_info()` to get per-stream length, pending count, and consumer group info. The `/health/detailed` endpoint includes worker pool stats when the event bus is enabled.

### Failure Count Cleanup

Per-message failure counts are stored in an in-memory dict. Stale entries (older than 300 seconds) are cleaned every 100 iterations to prevent memory leaks.

---

## 7. Database Migrations

All migrations live in `tripwire/db/migrations/` and are numbered sequentially. Run them in order against the Supabase PostgreSQL database.

| Migration | Purpose |
|---|---|
| `001_initial.sql` | Initial schema: `endpoints`, `subscriptions`, `events`, `nonces`, `webhook_deliveries` tables. Enables `pgcrypto`. |
| `002_add_event_type_data.sql` | Adds `type` and `data` JSONB columns to `events`. |
| `003_realtime_events.sql` | Creates `realtime_events` table for Notify-mode Supabase Realtime delivery. |
| `004_add_endpoint_id_to_events.sql` | Adds `endpoint_id` FK column to `events` for endpoint matching. |
| `005_api_key_index.sql` | Index on `api_key_hash` for fast API key lookups. |
| `006_svix_ids.sql` | Adds Svix provider ID columns to `endpoints`. (Historical -- Svix has been fully replaced by Convoy) |
| `007_key_rotation.sql` | Adds `old_api_key_hash` and `key_rotated_at` for API key rotation with 24h grace period. |
| `008_svix_to_convoy.sql` | Renames Svix columns to Convoy equivalents (Svix to Convoy migration). (Historical -- Svix has been fully replaced by Convoy) |
| `009_performance_indexes.sql` | Composite indexes for common query patterns on `webhook_deliveries` and other tables. |
| `010_wallet_auth.sql` | Adds `owner_address`, `registration_tx_hash`, `registration_chain_id` for wallet-based auth. |
| `011_rls_policies.sql` | Row Level Security policies for multi-tenant wallet isolation using `app.current_wallet`. |
| `012_audit_logging.sql` | Creates `audit_log` table for action/actor/resource tracking. |
| `013_trigger_registry.sql` | Creates `trigger_templates` and `trigger_instances` tables for MCP-driven AI agent triggers. |
| `014_event_endpoints_join.sql` | Many-to-many `event_endpoints` join table. Fixes single-endpoint limitation. |
| `015_install_count_fix.sql` | Fixes gameable `install_count` on `trigger_templates` by deduplicating per agent+template. |
| `016_webhook_secret_drop.sql` | Drops plaintext `webhook_secret` from `endpoints`. Convoy is the sole HMAC signer. Deploy code first, then run migration. |
| `017_topic0_column.sql` | Adds precomputed `topic0` (keccak256 hash) to triggers and templates for correct event matching. |
| `018_nonce_reorg_support.sql` | Adds `reorged_at` column to `nonces` for reorg-aware nonce deduplication. |
| `019_nonce_archival.sql` | Creates `nonces_archive` table and `archive_old_nonces` DB function for nonce cleanup. |
| `020_unified_event_lifecycle.sql` | Adds `source` column to `nonces` (facilitator vs goldsky) and `record_nonce_or_correlate` function to correlate duplicate nonces instead of silently dropping them. Fixes facilitator-then-Goldsky race condition. |
| `021_dlq_retry_count.sql` | Adds `dlq_retry_count` column to `webhook_deliveries` so DLQ retry counts survive restarts. |
| `022_audit_latency.sql` | Adds `execution_latency_ms` column to `audit_log` for MCP tool execution latency tracking. |
| `023_agent_metrics_view.sql` | Creates `agent_metrics` materialized view for per-agent metrics. |
| `024_trigger_payment_gating.sql` | Adds `require_payment`, `payment_token`, `min_payment_amount` to triggers for per-trigger payment gating. |
| `025_skill_spec_alignment.sql` | Adds `version`, `status`/lifecycle, `required_agent_class` to triggers and templates. |
| `026_event_neutral_schema.sql` | Makes events table event-neutral for Pulse/Keeper split: adds `event_type`, `decoded_fields`, `source`, `trigger_id`, `product_source` columns. |
| `027_advisory_locks.sql` | Creates `try_acquire_leader_lock(bigint)` and `release_leader_lock(bigint)` SQL functions wrapping `pg_try_advisory_lock` / `pg_advisory_unlock`. Used for leader election by FinalityPoller (lock 839201) and PreConfirmedSweeper (lock 839202). |

### How to Run

Migrations are plain SQL files. Execute them in order against your Supabase PostgreSQL database using the Supabase SQL Editor, `psql`, or any SQL client.

**Migration 016 note**: This migration drops a column. Deploy the application code first (so it stops writing to the column), then run the migration.

---

## 8. Health Checks

### GET /health (Liveness)

Returns 200 immediately with version info. No dependency checks. Suitable for container liveness probes.

```json
{"status": "ok", "service": "tripwire", "version": "x.y.z"}
```

### GET /health/detailed (Deep Probe)

Probes all dependencies and background tasks. Returns 200 if all components are healthy, 503 otherwise.

**Components checked:**

| Component | What it probes |
|---|---|
| `supabase` | SELECT query against `events` table. |
| `webhook_provider` | Checks provider type (Convoy vs LogOnly). |
| `redis` | `PING` command. |
| `identity_resolver` | Verifies resolver is initialized; reports type name. |
| `background_tasks` | Checks `finality_poller` and `dlq_handler` via health registry. Tasks are unhealthy if they haven't run in 300 seconds (5 minutes) or have never run. |
| `worker_pool` | (Only when event bus is enabled.) Reports per-worker stats: stream keys, processed count, errors, running status. Unhealthy if any worker is not running. |

**Overall status**: `healthy` if all components pass, `unhealthy` if any fail. Includes `uptime_seconds`.

### GET /ready (Readiness)

Returns 200 only after the full lifespan startup completes (`app.state.ready = True`). Returns 503 before that. Suitable for Kubernetes readiness probes and load balancer health checks.

---

## 9. Observability

### Prometheus Metrics (GET /metrics)

Served as an ASGI sub-application. Protected by `METRICS_BEARER_TOKEN` when set (requires `Authorization: Bearer <token>` header).

#### Counters

| Metric | Labels | Description |
|---|---|---|
| `tripwire_events_processed_total` | `chain_id`, `status` | Total events processed through the pipeline. |
| `tripwire_webhooks_sent_total` | `status`, `mode` | Total webhook delivery attempts. |
| `tripwire_errors_total` | `error_type` | Total application errors by type (including `dead_lettered`). |
| `tripwire_auth_requests_total` | `result` | Total authentication requests. |
| `tripwire_nonce_dedup_total` | `result` | Total nonce deduplication lookups. |
| `tripwire_redis_dlq_total` | (none) | Total Redis DLQ messages processed by the DLQ consumer. |

#### Histograms

| Metric | Labels | Description |
|---|---|---|
| `tripwire_pipeline_duration_seconds` | `stage` | Duration of pipeline stages (decode, dedup, identity, etc.). Buckets: 5ms to 10s. |
| `tripwire_request_duration_seconds` | `method`, `path_template`, `status_code` | HTTP request duration. Buckets: 5ms to 5s. |
| `tripwire_webhook_delivery_duration_seconds` | (none) | Webhook delivery round-trip duration. Buckets: 10ms to 30s. |

#### Gauges

| Metric | Description |
|---|---|
| `tripwire_dlq_backlog` | Number of failed deliveries in the dead-letter queue. |
| `tripwire_convoy_circuit_state` | Convoy circuit breaker state (0=closed, 1=open, 2=half_open). |

#### Info

| Metric | Labels | Description |
|---|---|---|
| `tripwire_build` | `version`, `env` | Build and deployment information. |

### OpenTelemetry Tracing (Optional)

Enable with `OTEL_ENABLED=true` and configure `OTEL_ENDPOINT` (OTLP exporter). Service name defaults to `tripwire` (`OTEL_SERVICE_NAME`). Tracing is set up early in the lifespan so all spans are captured, and flushed on shutdown.

### Sentry Error Tracking (Optional)

Set `SENTRY_DSN` to enable. Requires `pip install tripwire[sentry]`. Initialized before the lifespan to capture startup errors. `SENTRY_TRACES_SAMPLE_RATE` controls the fraction of transactions sampled (default 0.1). Unhandled exceptions in the global exception handler are reported via `capture_exception`.

### Structured Logging

Uses structlog with the following configuration:

- **Development** (`APP_ENV=development`): Colored, human-readable console output to stderr.
- **Production** (any other `APP_ENV`): JSON-formatted output to stderr, parseable by log aggregators (Datadog, Loki, CloudWatch, etc.).

Shared processors: context variables merge, log level, logger name, ISO timestamps, stack info, Unicode decoding. Third-party loggers (httpx, httpcore, hpack, supabase) are quieted to WARNING or above.

---

## 10. Infrastructure Dependencies

### Supabase (PostgreSQL)

Managed PostgreSQL via Supabase. Provides the database, Row Level Security, and Realtime (WebSocket broadcast for Notify-mode delivery). Required for all deployments.

### Convoy (Self-Hosted)

Webhook delivery infrastructure with retries, HMAC signing, delivery logs, and DLQ.

| Service | Port | Purpose |
|---|---|---|
| `convoy-server` | 5005 | Convoy HTTP API |
| `convoy-worker` | -- | Background delivery worker |
| `convoy-postgres` | 5433 | Convoy's own PostgreSQL |
| `convoy-redis` | 6380 | Convoy's own Redis |

Deployed via docker-compose. A direct httpx fast path for low-latency delivery is planned but not yet implemented; all delivery currently goes through Convoy exclusively.

**Circuit breaker**: Convoy calls are protected by a circuit breaker (5 consecutive failure threshold, 30s recovery window). States: `closed` (normal), `open` (all calls short-circuited), `half_open` (single probe request allowed). When the circuit is open, webhook deliveries fail fast instead of queuing behind a down Convoy instance.

### Redis

Used for five purposes:
1. SIWE nonce storage (always required when auth is used)
2. Rate limiting via SlowAPI
3. Event bus streams (only when `EVENT_BUS_ENABLED=true`)
4. Session storage (only when `SESSION_ENABLED=true`) -- each session is a Redis hash at `session:{session_id}` with fields for wallet address, budget, TTL, identity data. Budget decrements use an atomic Lua script (`EVALSHA`). Sessions have a Redis-level TTL of `ttl_seconds + 60` for automatic cleanup.
5. Shared caches for endpoints and triggers (`tripwire/cache.py` `RedisCache`). Keys use the `cache:` prefix. Cache invalidation on mutation ensures near-instant cross-instance visibility.

**Fail-open**: The shared caches degrade gracefully -- if Redis is unavailable, the app falls back to per-instance in-memory caches. All other functionality (auth, rate limiting, event bus, sessions) continues to require Redis when enabled.

Configured via `REDIS_URL` (default `redis://localhost:6379`). This is a separate Redis instance from Convoy's internal Redis (port 6380).

### Goldsky Turbo (Event Indexing)

Real-time blockchain indexing service. TripWire deploys one Goldsky Turbo pipeline per chain. Each pipeline applies a SQL transform to filter and decode ERC-3009 events from raw logs, then delivers decoded rows via webhook to TripWire's `/api/v1/ingest/goldsky` endpoint. Pipelines are external infrastructure -- they run on Goldsky's platform, not inside the TripWire process. See section 5 (Goldsky Pipeline Management) for deployment and lifecycle commands.

### Goldsky Edge (Managed RPC)

Managed RPC endpoints with caching, load balancing, and cross-node consensus. Used by:

- **Finality poller** -- Fetches current block numbers and block hashes for confirmation depth and reorg detection.
- **Identity resolver** -- Reads ERC-8004 identity and reputation registries on-chain.

Each RPC URL (`BASE_RPC_URL`, `ETHEREUM_RPC_URL`, `ARBITRUM_RPC_URL`) points to a Goldsky Edge endpoint. The `GOLDSKY_EDGE_API_KEY` is sent as a Bearer token on every JSON-RPC call.

### Identity Resolution (ERC-8004)

TripWire resolves onchain AI agent identities via the ERC-8004 registries deployed at the addresses configured by `ERC8004_IDENTITY_REGISTRY` and `ERC8004_REPUTATION_REGISTRY`. Resolution uses the Goldsky Edge RPC endpoints (same endpoints as the finality poller).

**RPC call profile per resolution**: 2 sequential calls (registry lookup → profile fetch) followed by up to 5 parallel calls (reputation scores, delegate checks, etc.).

**Caching**: Results are cached in-process using a two-tier TTL:
- Cache hits: 300 seconds (controlled by `IDENTITY_CACHE_TTL`)
- Cache misses: 30 seconds (to avoid hammering RPC for unknown addresses)

**Development mode**: When `APP_ENV=development`, a mock resolver is used. No RPC calls are made. This means `BASE_RPC_URL` and `GOLDSKY_EDGE_API_KEY` are not required for local development.

**Known limitation**: The identity cache is per-instance and is not shared across horizontally scaled deployments. Identity updates (e.g., a newly registered agent) will not be visible to other instances until their individual cache TTLs expire. With the default 300-second TTL, stale identity data can persist for up to 5 minutes per instance.

---

## 11. Production Checklist

### Required Environment Variables

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- `CONVOY_API_KEY`
- `TRIPWIRE_TREASURY_ADDRESS`

### Goldsky Data Plane

- [ ] Deploy Goldsky Turbo pipelines for each chain (`goldsky turbo apply`). See section 5.
- [ ] Configure Goldsky Edge RPC URLs (`BASE_RPC_URL`, `ETHEREUM_RPC_URL`, `ARBITRUM_RPC_URL`) with `GOLDSKY_EDGE_API_KEY`.
- [ ] Verify pipelines are running (`goldsky turbo status tripwire-{chain}-erc3009`).

### Security Settings

- [ ] Set `APP_ENV=production` to enforce secret validation and enable JSON logging.
- [ ] Set `GOLDSKY_WEBHOOK_SECRET` to validate inbound Goldsky webhooks.
- [ ] Set `FACILITATOR_WEBHOOK_SECRET` to validate x402 facilitator callbacks.
- [ ] Set `METRICS_BEARER_TOKEN` to protect the `/metrics` endpoint.
- [ ] Configure `CORS_ALLOWED_ORIGINS` to your frontend domain(s).
- [ ] Ensure `SUPABASE_SERVICE_ROLE_KEY` is never exposed to client-side code.

### Monitoring Setup

- [ ] Point your Prometheus scraper at `/metrics` (with Bearer token if set).
- [ ] Set `SENTRY_DSN` for error tracking.
- [ ] Set `DLQ_ALERT_WEBHOOK_URL` to receive dead-letter notifications (Slack, PagerDuty, etc.).
- [ ] Configure container orchestrator to use `/health` for liveness and `/ready` for readiness probes.
- [ ] Monitor `/health/detailed` for dependency health and background task staleness.

### Multi-Instance Deployment

Postgres advisory locks (migration 027) make the following background tasks safe for multi-instance deployment without manual configuration:

- **Finality poller** (lock 839201) -- Only one instance polls per cycle. Losing instances skip silently.
- **PreConfirmedSweeper** (lock 839202) -- Only one instance sweeps per cycle.

The following components do NOT have advisory locks and still require manual singleton control:

- **DLQ handler** -- Multiple instances will retry the same failed deliveries. Use `DLQ_ENABLED=false` on extra instances.
- **Nonce archiver** -- Multiple instances will attempt concurrent archival (not harmful but wasteful, uses `FOR UPDATE SKIP LOCKED` at the DB level).

---

## 12. Known Operational Limitations

### Finality Poller Leader Election

The finality poller uses a Postgres advisory lock (lock ID 839201, migration 027) for leader election. In a multi-instance deployment, only one instance runs the finality poll per cycle. Non-leaders skip silently and retry on the next interval. No manual `FINALITY_POLLER_ENABLED` toggling is required across instances.

### Caches

**Shared (Redis-backed)**: Endpoint and trigger caches are now backed by `tripwire/cache.py` `RedisCache`. Cache keys are invalidated on mutation (create/update/delete), so new triggers and endpoints are visible across instances within seconds.

**Still in-process** (not shared across instances):

- **Event bus consumer group cache** (`_known_groups`) -- Tracks which streams have consumer groups created. Invalidated on NOGROUP errors.
- **Event bus stream key cache** (`_known_stream_keys`) -- Tracks distinct stream keys for MAX_STREAMS enforcement. Populated from Redis on startup.
- **TriggerWorker failure counts** -- Per-message failure counts for DLQ routing. Lost on restart; messages may be retried from zero.
- **Identity resolver cache** -- TTL-based cache (`IDENTITY_CACHE_TTL=300s`). Each instance has independent cache state.

The identity resolver cache remains per-instance. Identity updates propagate only via TTL expiry (up to 300s staleness).

### Advisory Lock Coordination

Postgres advisory locks (migration 027) coordinate the finality poller (lock 839201) and PreConfirmedSweeper (lock 839202) across instances. Other background tasks (DLQ handler, nonce archiver) do not have advisory locks and must be coordinated at the deployment level.

### Event Bus Stream Cap

The `MAX_STREAMS=500` cap is enforced per-process using an in-memory set. In a multi-instance deployment with event bus enabled, each instance tracks streams independently, so the effective global cap could be up to `500 * N` streams. In practice this is mitigated by the shared Redis state, but the enforcement is not globally consistent.

### Batch Size Limit

The `/ingest` endpoint rejects payloads with more than 1000 logs (HTTP 400). If Goldsky sends larger batches, they must be split upstream.

### Pre-Confirmed Event TTL

Events that reach `pre_confirmed` status (e.g., from the x402 facilitator) but never receive a corresponding Goldsky confirmation are now swept by the `PreConfirmedSweeper` background task. Events older than `PRE_CONFIRMED_TTL_SECONDS` (default 1800 = 30 minutes) are marked as `payment.failed` and `payment.failed` webhooks are dispatched to linked endpoints. The sweeper runs every `PRE_CONFIRMED_SWEEP_INTERVAL_SECONDS` (default 300 = 5 minutes) and is protected by advisory lock 839202.

### Nonce Archival Timing

The nonce archiver runs on a fixed 24-hour interval starting from process boot. There is no cron-like scheduling. If the process restarts frequently, archival may run more or less often than daily.
