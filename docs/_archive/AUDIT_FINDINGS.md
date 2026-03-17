# TripWire Full Project Audit — 2026-03-11
# Last Updated: 2026-03-16 (feature/trigger-registry-mcp)

## CRITICAL (Must Fix)

| # | Issue | Status | Details |
|---|-------|--------|---------|
| 1 | **Missing `erc3009_events` table** | RESOLVED (migration 013) | Trigger registry migration (013_trigger_registry.sql) introduced the full trigger schema. Goldsky pipeline now delivers events via webhook to `/api/v1/ingest` rather than sinking to a raw table directly. |
| 2 | **Goldsky architecture mismatch** | RESOLVED | Pipeline uses `type: webhook` sink delivering to TripWire's `/api/v1/ingest` endpoint. Ingest route processes and fans out to endpoints/triggers. |
| 3 | **CORS wildcard + credentials** | RESOLVED | `allow_origins` restricted to configured `ALLOWED_ORIGINS` list in main.py. Wildcard + credentials combination removed. |
| 4 | **Convoy setup failures silently swallowed** | RESOLVED | endpoints.py now propagates Convoy project/endpoint creation failures with a 502 response instead of swallowing the error and returning 201. |
| 5 | **No CI/CD** | PENDING | No GitHub Actions, no automated tests on PR, no lint/type gates. No pipeline exists on this branch. |
| 6 | **~60% of modules have ZERO tests** | PENDING | auth.py, all 3 DB repos, identity resolver, convoy_client.py, verify.py, provider.py, realtime.py, trigger_worker.py, event_bus.py, and SDK client/verify all remain untested. Estimated coverage gap unchanged at ~60%. |

## HIGH (Production Blockers)

| # | Issue | Status | Details |
|---|-------|--------|---------|
| 7 | **No webhook delivery status API** | PENDING | No GET /endpoints/{id}/deliveries or GET /deliveries/{id}. Developers cannot check delivery state or debug failures without querying Supabase directly. |
| 8 | **No DLQ handler** | RESOLVED (event_bus.py) | Redis Streams DLQ implemented: `tripwire:dlq` stream captures messages that exceed 5 retry attempts, recording source stream, message ID, raw log, and error count before ACKing from the source stream. Event bus path only; Convoy DLQ polling not yet implemented. |
| 9 | **Duplicate delivery problem** | RESOLVED (migration 014) | `event_endpoints` M2M join table (014_event_endpoints_join.sql) replaces the single `endpoint_id` column on events. Each event fan-outs to all matched endpoints exactly once via the join table, eliminating duplicate dispatch caused by both Convoy and direct path firing simultaneously. |
| 10 | **No exception handling in processor.py** | RESOLVED | processor.py now wraps nonce recording, endpoint fetch, and policy evaluation in try-except blocks. DB failures return a structured error rather than crashing the pipeline. |
| 11 | **Finality fallback bug** | RESOLVED | processor.py finality fallback now defaults to PAYMENT_PENDING (not PAYMENT_CONFIRMED) when RPC returns None. |
| 12 | **Unhandled Supabase exceptions** | RESOLVED | All route handlers (endpoints.py, events.py, subscriptions.py) now wrap `.execute()` calls with try-except, returning structured 503 responses on DB/auth failure. |
| 13 | **Missing DB indexes** | RESOLVED (migrations 009, 017) | Migration 009 added composite indexes on `(endpoint_id, status)` and `webhook_deliveries.created_at`. Migration 017 added a composite index on `triggers(topic0, active)` for O(1) trigger lookup by event signature. |
| 14 | **Endpoint URL not validated** | RESOLVED | HTTPS enforcement in production and SSRF protection against RFC-1918/loopback addresses added to endpoint registration. |
| 15 | **`match_subscriptions()` never called** | RESOLVED | processor.py now calls `match_subscriptions()` before dispatching to notify-mode endpoints so subscription filters are respected. |
| 16 | **Stats endpoint has no rate limiter** | RESOLVED | `@limiter.limit()` decorator added to stats.py route; 4 COUNT(*) queries are now rate-limited per IP. |

## MEDIUM

| # | Issue | Status | Details |
|---|-------|--------|---------|
| 17 | **Docker-compose not production-ready** | PENDING | Hardcoded Convoy Postgres creds (`convoy:convoy`), no Redis auth, no health checks on Convoy server, no resource limits. Unchanged. |
| 18 | **Race condition in webhook secret** | RESOLVED (migration 016) | 016_webhook_secret_drop.sql removes the `webhook_secret` column from `endpoints`. Convoy is the sole HMAC signer and stores secrets internally, eliminating the memory-to-DB race. |
| 19 | **3 undocumented API endpoints** | RESOLVED | `/rotate-key`, `/stats`, and the Goldsky ingest endpoint are documented in the API reference. |
| 20 | **Config naming inconsistency** | RESOLVED | `WEBHOOK_SIGNING_SECRET` renamed to `CONVOY_SIGNING_SECRET` in settings.py and .env.example to match documentation. |
| 21 | **SDK not publishable** | PENDING | No tests, no package metadata, no changelog; still at version 0.1.0. |
| 22 | **No metrics/observability** | PENDING | No Prometheus, no OpenTelemetry, no delivery latency tracking. Structured logging via structlog is in place but no metrics pipeline. |
| 23 | **Unused `audit_log` table** | RESOLVED (migration 012) | Migration 012 wired up audit logging triggers and the `AuditLogger` helper is used in MCP tool handlers. |

## NEW FINDINGS (feature/trigger-registry-mcp)

| # | Issue | Status | Details |
|---|-------|--------|---------|
| 24 | **install_count gameable** | RESOLVED (migration 015) | 015_install_count_fix.sql adds a unique partial index `idx_trigger_instances_unique_active` on `(template_id, owner_address) WHERE active = TRUE`, preventing an agent from inflating the count by rapidly creating/deleting instances. The DB trigger was replaced with a balanced increment/decrement function (`sync_template_install_count`) that handles INSERT, UPDATE, and DELETE cases. install_count recalculated from live data on deploy. |
| 25 | **topic0 key mismatch** | RESOLVED (migration 017) | 017_topic0_column.sql adds a precomputed `topic0` column to both `triggers` and `trigger_templates`. Application code computes the keccak256 hash at insert time (PostgreSQL has no native keccak256). The composite index `idx_triggers_topic0_active` enables O(1) active-trigger lookup by event signature. |
| 26 | **Reorged nonces unrecoverable** | RESOLVED (migration 018) | 018_nonce_reorg_support.sql adds `reorged_at` and `event_id` columns to `nonces` and introduces the `record_nonce_with_reorg()` PL/pgSQL function. A nonce with `reorged_at IS NOT NULL` is treated as available for reuse when the same (chain_id, nonce, authorizer) appears in a new confirmed event. |
| 27 | **Nonces table grows unbounded** | RESOLVED (migration 019) | 019_nonce_archival.sql creates `nonces_archive` and the `archive_old_nonces(age_threshold, batch_size)` function. Confirmed, non-reorged nonces older than 30 days are moved in batches of 5000 using `FOR UPDATE SKIP LOCKED` to be safe under concurrent load. `record_nonce_with_reorg()` updated to check the archive table before accepting a nonce, preventing reuse of archived nonces. A background archival job (`tripwire/db/archival.py`) calls this function on a configurable schedule. |
| 28 | **Wallet auth (SIWE) for MCP** | RESOLVED (migration 010) | Auth migrated from API-key to SIWE wallet-based authentication. MCP server implements 3-tier auth: PUBLIC (no auth), SIWX (SIWE wallet signature), X402 (per-call micropayment). SIWE message binds method, path, and SHA-256 body hash in the statement field, providing request integrity. Nonces are consumed atomically via Redis `DEL`. |
| 29 | **MCP SIWE does not bind method/path to signature** | RESOLVED | `_verify_siwe()` in `tripwire/mcp/auth.py` builds a SIWE statement of the form `"{METHOD} {PATH} {body_sha256}"` before signature recovery (lines 118–129), binding the credential to a specific request. Cross-endpoint replay is not possible. |
| 30 | **No per-address MCP rate limiting** | RESOLVED (mcp/server.py) | Per-address rate limiting enforced at 60 calls/minute using Redis INCR + TTL. On Redis unavailability the check fails open (auth has already passed). |
| 31 | **Finality poller has no distributed coordination** | PENDING | The finality poller runs as an in-process asyncio task with no distributed lock. Deploying more than one instance of TripWire will cause every instance to poll and confirm the same pending events, resulting in duplicate `payment.confirmed` webhooks. Requires a Redis-based leader election or distributed lock (e.g., Redis `SET NX` with TTL) before this can be horizontally scaled. |
| 32 | **Pre-confirmed events can be stranded** | PENDING | Events that reach `payment.pre_confirmed` state but whose originating chain reorgs (or whose finality poll never fires) have no cleanup path. There is no background job that ages out or requeues stale `payment.pre_confirmed` events. A sweeper job with a configurable staleness threshold (e.g., 2× the chain's finality window) is needed. |
| 33 | **In-process caches not invalidated across instances** | PENDING | `TriggerIndex` (trigger_worker.py) and the identity resolver cache are in-process only. Multi-instance deployments will serve stale trigger/identity data until the 10-second DB refresh fires. Requires Redis pub/sub invalidation (e.g., a `tripwire:cache:invalidate` channel) so that any instance updating a trigger or identity record can broadcast invalidation to all peers. |
| 34 | **No per-wallet trigger count cap** | PENDING | The MCP server enforces 60 calls/minute per wallet address but there is no upper bound on the total number of active triggers an owner can create. A single agent can create an unbounded number of triggers, exhausting DB resources and degrading `TriggerIndex` lookup performance. A `max_triggers_per_owner` config value enforced at `create_trigger` time is needed. |
| 35 | **Identity cache is in-process only** | PENDING | `IdentityResolver` holds resolved ERC-8004 identities in a process-local dict. Multi-instance deployments resolve the same address independently, putting unnecessary load on the identity RPC endpoint and returning inconsistent reputation scores across instances mid-window. Identity records should be cached in Redis with a TTL matching the expected on-chain update cadence. |
| 36 | **Event bus (Redis Streams)** | RESOLVED (event_bus.py, trigger_worker.py) | Redis Streams event bus implemented behind `EVENT_BUS_ENABLED` feature flag (default: false). Key safety properties: DLQ stream (`tripwire:dlq`) for messages exceeding 5 retries; per-message failure counter with stale-entry pruning every 100 iterations; `MAX_STREAMS=500` cap enforced at both publish and worker-discovery time; `_MAX_STREAMS_PER_WORKER=100` per-worker cap; exponential backoff (up to 60s) on consecutive consume errors; batched ACK via Redis pipeline; XAUTOCLAIM reclaims messages idle >30s from crashed workers; `WorkerPool` auto-restarts crashed workers unless shutting down; graceful 30s shutdown timeout. When disabled, /ingest processes events synchronously with no Redis dependency. |

## Files Requiring Changes

### Still Pending
- `.github/workflows/` — CI/CD pipeline (not created)
- `tripwire/ingestion/finality_poller.py` — Add Redis distributed lock for single-poller coordination
- `tripwire/ingestion/processor.py` or new sweeper — Add staleness sweeper for `payment.pre_confirmed` events
- `tripwire/ingestion/trigger_worker.py` + `tripwire/identity/resolver.py` — Replace in-process caches with Redis-backed cache + pub/sub invalidation
- `tripwire/mcp/tools.py` — Enforce `max_triggers_per_owner` at `create_trigger` time
- `tripwire/api/routes/deliveries.py` — Webhook delivery status API (not yet created)
- `tests/` — Increase coverage to >80%; prioritize DB repos, event bus, trigger worker, processor, auth

### Resolved This Branch
- `tripwire/db/migrations/014_event_endpoints_join.sql` — M2M join table for multi-endpoint dispatch
- `tripwire/db/migrations/015_install_count_fix.sql` — install_count dedup + balanced trigger
- `tripwire/db/migrations/016_webhook_secret_drop.sql` — Remove plaintext webhook_secret
- `tripwire/db/migrations/017_topic0_column.sql` — Precomputed topic0 + composite index
- `tripwire/db/migrations/018_nonce_reorg_support.sql` — Reorg-aware nonce dedup
- `tripwire/db/migrations/019_nonce_archival.sql` — nonces_archive + archive_old_nonces()
- `tripwire/mcp/auth.py` — SIWE auth with method+path+body binding, per-address rate limiting
- `tripwire/mcp/server.py` — 3-tier auth (PUBLIC / SIWX / X402), per-address rate limit
- `tripwire/ingestion/event_bus.py` — Redis Streams pub/sub with DLQ and stream caps
- `tripwire/ingestion/trigger_worker.py` — TriggerIndex, TriggerWorker, WorkerPool with auto-restart
