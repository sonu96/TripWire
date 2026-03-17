# TripWire — Team Update

**Date:** 2026-03-17
**Prepared from:** Latest codebase + all docs (ARCHITECTURE, API-REFERENCE, WEBHOOKS, MCP-SERVER, SECURITY, OPERATIONS, DEVELOPMENT)

---

## What TripWire Is

TripWire is the infrastructure layer between onchain events and application execution. It watches EVM chains (Base, Ethereum, Arbitrum) for specific log events, decodes them, evaluates policies, resolves agent identity via ERC-8004, and delivers structured webhooks or real-time notifications. x402 payment webhooks are the primary use case, but the trigger registry now supports arbitrary EVM events.

**One-liner:** Goldsky tells you what happened. TripWire decides if it's safe to act. Convoy makes sure you hear about it.

---

## Architecture at a Glance

```
L0  Chain          Base / Ethereum / Arbitrum (ERC-3009 + arbitrary EVM events)
L1  Goldsky Turbo  Indexes raw logs, SQL transforms, delivers via webhook
L2  TripWire       Decode → Dedup → Finality → Identity → Policy → Filter
L3  Delivery       Convoy webhooks (execute) + Supabase Realtime (notify)
L4  Application    Developer's API (acts on verified webhook)
L5  MCP Server     8 tools, 3-tier auth (PUBLIC / SIWX / X402)
```

**Stack:** Python 3.11, FastAPI, Supabase PostgreSQL, Convoy (self-hosted), Redis Streams (optional event bus), Goldsky Edge RPC, Pydantic v2, structlog, Prometheus, OpenTelemetry, Sentry.

---

## What Shipped This Sprint (2026-03-17)

### 1. Unified Processing Loop (Phase C2)

**Problem:** Two completely separate code paths for ERC-3009 events and dynamic triggers. The dynamic trigger path was missing finality checking, full policy evaluation, notify mode, tracing, and metrics.

**Solution:** New `_process_unified()` method in `processor.py` — a single pipeline using `DecodedEvent` as the uniform data structure. Feature-flagged via `UNIFIED_PROCESSOR=true` (default: off, legacy paths remain as fallback).

**What dynamic triggers gain:**
- Block finality checking (confirmations via RPC)
- Full endpoint policy evaluation (not just reputation)
- Finality depth gating per endpoint
- Execution state metadata (`execution_state`, `safe_to_execute`, `trust_source`)
- Notify mode (Supabase Realtime push)
- OpenTelemetry tracing spans
- Prometheus pipeline metrics

**Unified pipeline stages:**
```
DECODE → FILTER → PAYMENT GATE → DEDUP → FINALITY ∥ IDENTITY → REPUTATION → ENDPOINT → POLICY → DISPATCH → RECORD
```

### 2. Per-Trigger Payment Gating (Phase C3)

**Problem:** No way to require that a decoded event contains a payment before firing a trigger.

**Solution:** Three new fields on the `Trigger` model + payment metadata on `DecodedEvent`:

| Trigger Field        | Purpose                                    |
|----------------------|--------------------------------------------|
| `require_payment`    | Enable payment gating (default: false)     |
| `payment_token`      | Required token contract (null = any)       |
| `min_payment_amount` | Minimum amount in smallest unit            |

`ERC3009Decoder` now populates `payment_amount`, `payment_token`, `payment_from`, `payment_to` on every `DecodedEvent`. The processor's `_check_payment_gate()` validates these against trigger requirements before dispatch.

**Migration:** `024_trigger_payment_gating.sql`

### 3. Execution State Everywhere

Every webhook payload, API response, and MCP result now carries:
- `execution_state` — provisional / confirmed / finalized / reorged
- `safe_to_execute` — true only when finalized
- `trust_source` — facilitator or onchain

New `execution_state_from_status()` derives these from the DB `events.status` column at query time — no extra DB columns needed.

### 4. Decoder Abstraction (Phase C1)

New `tripwire/ingestion/decoders/` package:
- `Decoder` protocol (runtime-checkable)
- `DecodedEvent` dataclass (unified envelope)
- `ERC3009Decoder` (wraps `decode_transfer_event`)
- `AbiGenericDecoder` (wraps `decode_event_with_abi`)

Processor routes all decoding through these wrappers. Raw functions remain for backward compatibility.

### 5. Other Additions

- **Reputation gating for MCP** — `register_middleware`, `create_trigger`, `activate_template` require `min_reputation >= 10.0`
- **Execution latency tracking** — `audit_log.execution_latency_ms` (migration 022)
- **Agent metrics view** — `GET /stats/agent-metrics` backed by materialized view (migration 023)

---

## Decoder Phase Completion

| Phase | Status          | Description                                                    |
|-------|-----------------|----------------------------------------------------------------|
| C1    | **Done**        | Decoder protocol + DecodedEvent envelope + two concrete decoders |
| C2    | **Done**        | Unified processing loop (feature-flagged)                       |
| C3    | **Done**        | Per-trigger payment gating via decoder metadata                 |

All three phases of the decoder unification are now implemented.

---

## System Capabilities Summary

### Ingestion Paths

| Path                   | Latency      | Execution State | Safe to Execute |
|------------------------|--------------|-----------------|-----------------|
| x402 facilitator       | ~40-125ms    | provisional     | false           |
| Goldsky (ERC-3009)     | ~1-4s        | confirmed       | false           |
| Goldsky (dynamic)      | ~1-4s        | confirmed       | false           |
| Finality promotion     | chain-dependent | finalized    | true            |

### Finality Depths

| Chain    | Default Depth | Block Time | Time to Finalize |
|----------|--------------|------------|------------------|
| Arbitrum | 1 block      | ~250ms     | ~5s (with poll)  |
| Base     | 3 blocks     | ~2s        | ~16s             |
| Ethereum | 12 blocks    | ~12s       | ~174s            |

### MCP Tools (8 total)

| Tool                | Auth Tier | Price   | Min Reputation |
|---------------------|-----------|---------|----------------|
| register_middleware | X402      | $0.003  | 10.0           |
| create_trigger      | X402      | $0.003  | 10.0           |
| activate_template   | X402      | $0.001  | 10.0           |
| list_triggers       | SIWX      | free    | 0              |
| delete_trigger      | SIWX      | free    | 0              |
| list_templates      | SIWX      | free    | 0              |
| get_trigger_status  | SIWX      | free    | 0              |
| search_events       | SIWX      | free    | 0              |

### Event Bus (Optional)

Redis Streams partitioned by topic0. Feature-flagged via `EVENT_BUS_ENABLED`.
- 500 stream cap (global), 100 per worker, 100k max stream length
- DLQ after 5 failures, exponential backoff, batched ACK
- Graceful degradation: app continues if worker pool fails to start

---

## Database Migrations (24 total)

Key recent migrations:
- **020** — Unified event lifecycle (facilitator-Goldsky correlation via `record_nonce_or_correlate`)
- **021** — DLQ retry count tracking
- **022** — Audit log execution latency
- **023** — Agent metrics materialized view
- **024** — Trigger payment gating columns (`require_payment`, `payment_token`, `min_payment_amount`)

---

## Known Issues & Technical Debt

### High Priority

| Issue | Location | Impact |
|-------|----------|--------|
| Chain ID mismatch (SDK=1, server=8453) | `auth.py` / `signer.py` | SDK auth fails entirely |
| MCP `register_middleware` doesn't create Convoy endpoint | `tools.py` | MCP-created endpoints can't deliver webhooks |
| RLS policies exist but never enforced | `__init__.py` | Application-layer ownership checks are sole defense |

### Medium Priority

| Issue | Impact |
|-------|--------|
| Single-instance finality poller (no distributed lock) | Duplicate confirmations in multi-instance |
| In-process caches not shared across instances | Stale data windows up to 30s |
| No per-wallet trigger cap | Potential starvation with 500-stream cap |
| Stranded pre_confirmed events (no TTL sweep) | Orphaned rows if tx never lands |

### Low Priority

- Vestigial `webhook_signing_secret` config (dead code)
- Nonce archival on fixed 24h interval (not cron-like)
- Test coverage ~60% (gaps: MCP tools, event bus, finality poller, DLQ)

---

## Infrastructure Dependencies

| Service          | Role                              | Deployment        |
|------------------|-----------------------------------|-------------------|
| Supabase         | PostgreSQL + Realtime push        | Managed           |
| Convoy           | Webhook delivery + retries + DLQ  | Self-hosted (Docker) |
| Redis            | SIWE nonces, rate limiting, event bus | Self-hosted     |
| Goldsky Turbo    | Chain indexing (3 pipelines)      | External          |
| Goldsky Edge     | Managed RPC (finality + identity) | External          |
| ERC-8004 Registries | Agent identity + reputation    | Onchain (CREATE2) |

---

## What's Next

### Immediate (unblock production)
- Fix SDK chain ID mismatch (auth broken end-to-end)
- Wire MCP `register_middleware` to Convoy endpoint creation
- Add distributed lock to finality poller (Postgres advisory or Redis)

### Short-term
- Enable `UNIFIED_PROCESSOR=true` in staging, validate parity with legacy paths
- Add per-wallet trigger cap (prevent stream starvation)
- TTL sweeper for stranded `pre_confirmed` events
- Wire RLS policies (call `get_supabase_scoped` in routes)

### Medium-term
- Shared Redis cache for identity resolution
- Test coverage push (MCP tools, event bus, finality poller)
- Additional chains (Optimism, Polygon, Avalanche)
- TypeScript SDK

---

## File Change Summary (This Sprint)

| File | Change |
|------|--------|
| `tripwire/ingestion/processor.py` | `_process_unified()` + `_check_payment_gate()` |
| `tripwire/ingestion/decoders/protocol.py` | Payment fields on `DecodedEvent` |
| `tripwire/ingestion/decoders/erc3009.py` | Populate payment metadata |
| `tripwire/ingestion/decoders/abi_generic.py` | New decoder wrapper |
| `tripwire/types/models.py` | Payment gating on `Trigger` + execution state helpers |
| `tripwire/config/settings.py` | `unified_processor` feature flag |
| `tripwire/db/migrations/022-024` | Latency, agent metrics, payment gating |
| `tripwire/api/routes/events.py` | Execution state in responses |
| `tripwire/api/routes/deliveries.py` | Execution state in responses |
| `tripwire/api/routes/stats.py` | Agent metrics endpoint |
| `tripwire/mcp/server.py` | Reputation gating, execution latency |
| `tripwire/mcp/tools.py` | Execution state in MCP responses |
| `docs/ARCHITECTURE.md` | C2/C3 sections, updated phase table |
| `CHANGELOG.md` | Sprint entries |
