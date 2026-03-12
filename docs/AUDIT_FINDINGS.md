# TripWire Full Project Audit — 2026-03-11

## CRITICAL (Must Fix)

| # | Issue | Details |
|---|-------|---------|
| 1 | **Missing `erc3009_events` table** | Goldsky pipeline sinks to this table but no migration creates it. Pipeline will fail at runtime. |
| 2 | **Goldsky architecture mismatch** | Pipeline now uses webhook sink delivering to TripWire's ingest endpoint. Verify pipeline YAML uses `type: webhook` sink config pointing to `/api/v1/ingest`. |
| 3 | **CORS wildcard + credentials** | `allow_origins=["*"]` with `allow_credentials=True` in main.py is a security vulnerability (CSRF). Must restrict origins. |
| 4 | **Convoy setup failures silently swallowed** | Endpoint creation returns 201 even when Convoy project/endpoint creation fails. Webhooks never work but user isn't told. endpoints.py lines 110-111. |
| 5 | **No CI/CD** | No GitHub Actions, no automated tests on PR, no lint/type gates. |
| 6 | **~60% of modules have ZERO tests** | auth.py, all 3 DB repos, identity resolver, convoy_client.py, verify.py, provider.py, realtime.py, pipeline.py, SDK client/verify all untested. |

## HIGH (Production Blockers)

| # | Issue | Details |
|---|-------|---------|
| 7 | **No webhook delivery status API** | Developers can't check if their webhooks were delivered or debug failures. Need GET /endpoints/{id}/deliveries and GET /deliveries/{id}. |
| 8 | **No DLQ handler** | Failed webhooks after Convoy's max retries disappear silently. Need background job polling Convoy failed deliveries + alerting. |
| 9 | **Duplicate delivery problem** | Both Convoy and direct path fire simultaneously with no idempotency key. Consumers receive webhook twice with no way to deduplicate. |
| 10 | **No exception handling in processor.py** | Nonce recording (line 94-98), endpoint fetch (line 126), and policy evaluation (line 153-168) have no try-except. Pipeline crashes on DB failures. |
| 11 | **Finality fallback bug** | If RPC fails, finality=None causes event to be marked PAYMENT_CONFIRMED instead of PAYMENT_PENDING. processor.py line 176. |
| 12 | **Unhandled Supabase exceptions** | Zero try-except on `.execute()` calls in ALL route handlers (endpoints.py, events.py, subscriptions.py). Network/auth failures crash with 500. |
| 13 | **Missing DB indexes** | No index on `provider_message_id`, no composite index on `(endpoint_id, status)`, no index on `webhook_deliveries.created_at`. |
| 14 | **Endpoint URL not validated** | No HTTPS enforcement in production, no SSRF protection against localhost/private IPs. Security risk. |
| 15 | **`match_subscriptions()` never called** | Notify-mode endpoints get ALL events regardless of subscription filters. dispatcher.py has the function but processor.py never calls it. |
| 16 | **Stats endpoint has no rate limiter** | 4 COUNT(*) queries with no rate limiting protection = DoS vector. stats.py missing @limiter.limit() decorator. |

## MEDIUM

| # | Issue | Details |
|---|-------|---------|
| 17 | **Docker-compose not production-ready** | Hardcoded Convoy Postgres creds (`convoy:convoy`), no Redis auth, no health checks on Convoy server, no resource limits. |
| 18 | **Race condition in webhook secret** | Secret generated in memory → async Convoy call → DB write. If DB write fails after Convoy succeeds, secret lost. |
| 19 | **3 undocumented API endpoints** | `/rotate-key`, `/stats`, `/goldsky` ingest endpoints not in API reference docs. |
| 20 | **Config naming inconsistency** | Docs say `CONVOY_SIGNING_SECRET`, code uses `WEBHOOK_SIGNING_SECRET`. |
| 21 | **SDK not publishable** | No tests, no package metadata, no changelog, version 0.1.0. |
| 22 | **No metrics/observability** | No Prometheus, no OpenTelemetry, no delivery latency tracking. |
| 23 | **Unused `audit_log` table** | Created in migration 001 but referenced nowhere in code. Dead schema. |

## Files Requiring Changes

### Critical
- `tripwire/db/migrations/` — New migration for erc3009_events table
- `tripwire/main.py` — Fix CORS configuration
- `tripwire/api/routes/endpoints.py` — Fail on Convoy setup failure
- `.github/workflows/` — CI/CD pipeline (new)

### High
- `tripwire/api/routes/deliveries.py` — Webhook delivery status API (new)
- `tripwire/webhook/dispatcher.py` — Add idempotency keys, call match_subscriptions()
- `tripwire/ingestion/processor.py` — Add exception handling, fix finality fallback
- `tripwire/api/routes/endpoints.py` — URL validation, Supabase error handling
- `tripwire/api/routes/events.py` — Supabase error handling
- `tripwire/api/routes/subscriptions.py` — Supabase error handling
- `tripwire/api/routes/stats.py` — Add rate limiter
- `tripwire/db/migrations/` — Add missing indexes
