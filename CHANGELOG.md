# Changelog

All notable changes to TripWire are documented in this file.

## [2026-03-18] — x402 V2 Migration

### Changed
- **x402 V2 payment header** — X402-tier MCP tools now accept `PAYMENT-SIGNATURE` (V2) in addition to `X-PAYMENT` (V1, deprecated). V2 callers should use `PAYMENT-SIGNATURE`.
- **SIWX authentication** — SIWX-tier MCP tools now accept the x402 V2 `SIGN-IN-WITH-X` header alongside the existing custom `X-TripWire-*` SIWE headers. The custom headers are deprecated for MCP.

### Added
- **`GET /discovery/resources`** — x402 V2 Bazaar discovery endpoint. Serves the same service discovery manifest as `GET /.well-known/x402-manifest.json` using V2 conventions.

### Documentation
- Updated SECURITY.md, MCP-SERVER.md, API-REFERENCE.md, and CLAUDE.md to reflect x402 V2 header changes, SIWX support, and the new Bazaar V2 endpoint.
- Added V1-to-V2 migration guide in SECURITY.md.

## [2026-03-17] — Gap Fixes

### Changed
- **Nested `ExecutionBlock` on WebhookPayload** — `WebhookPayload` now uses a nested `execution` block (`ExecutionBlock` model) instead of flat `execution_state`, `safe_to_execute`, `trust_source` fields at the root. Finality data moved from `data.finality` into `execution.finality`. `derive_execution_metadata()` now returns an `ExecutionBlock` (not a tuple).
- **`check_finality_generic()` in finality.py** — New function that accepts raw values (chain_id, block_number, tx_hash) without requiring an `ERC3009Transfer` model. The original `check_finality()` now delegates to it. Enables finality checking for any event type in the unified processor.
- **AbiGenericDecoder extracts payment fields** — Best-effort payment field extraction (`_extract_payment_fields`) scans decoded event fields for amount-like, from-like, and to-like keys. C3 payment gating now works for dynamic triggers, not just ERC-3009 events.

### Added
- **`required_agent_class` on Trigger model** — ERC-8004 agent class gate at the trigger level (not just endpoint policy level).
- **`version` field on Trigger and TriggerTemplate** — Trigger definition versioning (default: "1.0.0").
- **Migration 025 (`025_skill_spec_alignment.sql`)** — Adds `version`, `status`/lifecycle, `required_agent_class` columns to triggers; `version` to trigger_templates.

## [2026-03-17] — Sprint Delivery

### Added
- **Unified processing loop (C2)** — New `_process_unified()` in `processor.py` merges separate ERC-3009 and dynamic trigger code paths into a single pipeline using `DecodedEvent`. Feature-flagged via `UNIFIED_PROCESSOR=true`. Dynamic triggers now gain: finality checking, full policy evaluation, finality depth gating, execution state metadata, notify mode, tracing spans, and Prometheus metrics.
- **Per-trigger payment gating (C3)** — Triggers can require decoded events to contain payment data meeting a threshold before dispatch. New fields on `Trigger`: `require_payment`, `payment_token`, `min_payment_amount`. New fields on `DecodedEvent`: `payment_amount`, `payment_token`, `payment_from`, `payment_to`. Migration `024_trigger_payment_gating.sql`.
- **Execution state everywhere** — New `execution_state_from_status()` helper in `tripwire/types/models.py` maps DB status to `(ExecutionState, safe_to_execute, TrustSource)`. All event and delivery API responses now include `execution_state`, `safe_to_execute`, and `trust_source` fields. Stats endpoint includes `execution_state_breakdown` dict. MCP `search_events` returns execution state fields per event; `get_trigger_status` returns `last_event_execution_state`.
- **Reputation gating for paid MCP tools** — `register_middleware`, `create_trigger`, and `activate_template` now require `min_reputation >= 10.0`. Dynamic triggers with `reputation_threshold > 0` reject events from low-reputation agents.
- **Execution latency tracking** — Migration `022_audit_latency.sql` adds `execution_latency_ms` column to `audit_log`; MCP server records execution latency per tool call.
- **Agent metrics materialized view** — Migration `023_agent_metrics_view.sql` creates `agent_metrics` materialized view; new `GET /stats/agent-metrics` endpoint exposes per-agent metrics.
- **Decoder abstraction** — New `tripwire/ingestion/decoders/` package introducing a `Decoder` protocol, `DecodedEvent` dataclass, `ERC3009Decoder`, and `AbiGenericDecoder`. Processor uses decoder wrappers; existing decoder functions remain untouched for backward compatibility.
- **Execution state and decoder tests** — `tests/test_execution_state.py` with 15 tests covering status mapping, decoder protocol compliance, and both decoder wrappers.

## [Unreleased]

### Added
- **SIWE wallet authentication** — Replaced API-key auth with EIP-191 wallet signatures and Sign-In with Ethereum (SIWE). Nonces are stored in Redis with a configurable tolerance window (`AUTH_TIMESTAMP_TOLERANCE_SECONDS`).
- **x402 payment-gated registration** — Endpoint registration now requires an on-chain USDC micro-payment via the x402 protocol. New config: `TRIPWIRE_TREASURY_ADDRESS`, `X402_FACILITATOR_URL`, `X402_REGISTRATION_PRICE`, `X402_NETWORK`.
- **Ownership enforcement on all routes** — Every API route now validates that the caller's wallet owns the resource being accessed. Eliminates IDOR across endpoints, subscriptions, and delivery logs.
- **Row-Level Security (RLS) policies** — Supabase RLS policies enforce wallet-scoped access at the database layer, providing defense-in-depth behind the application-level ownership checks.
- **ERC-8004 identity resolution** — On-chain identity and reputation lookups via CREATE2-deployed registries (`ERC8004_IDENTITY_REGISTRY`, `ERC8004_REPUTATION_REGISTRY`) with configurable cache TTL.
- **Facilitator webhook validation** — Inbound callbacks from the x402 facilitator are verified using `FACILITATOR_WEBHOOK_SECRET`.
- **WebSocket subscriber (opt-in)** — Optional secondary ingestion path via WebSocket subscriptions for ~200-500ms latency, controlled by `WS_SUBSCRIBER_ENABLED`.
- **Dead-letter queue (DLQ)** — Failed deliveries are queued for automatic retry with configurable polling interval, max retries, and alert webhook.
- **Finality poller** — Background worker that polls chain RPCs to confirm transaction finality before triggering webhook delivery.
- **SDK type safety improvements** — TypeScript SDK now uses branded types for wallet addresses, endpoint IDs, and chain identifiers. Eliminates stringly-typed foot-guns.

### Changed
- **Convoy single-path delivery** — Consolidated webhook delivery to use Convoy as the sole dispatch path, removing the dual Svix/httpx split. Simplifies retry logic and observability.
- **Error hierarchy** — Introduced a structured exception hierarchy (`TripWireError` base class with typed subclasses) replacing ad-hoc `HTTPException` raises. All errors now carry machine-readable codes.
- **Dev server separation** — Development server (`APP_ENV=development`) runs with relaxed validation; production requires `supabase_url`, `supabase_service_role_key`, `convoy_api_key`, and `tripwire_treasury_address`.
- **SecretStr for all secrets** — `SUPABASE_SERVICE_ROLE_KEY`, `CONVOY_API_KEY`, `WEBHOOK_SIGNING_SECRET`, `GOLDSKY_API_KEY`, `GOLDSKY_WEBHOOK_SECRET`, and `FACILITATOR_WEBHOOK_SECRET` are now `pydantic.SecretStr` fields. Secrets are masked in logs and `.model_dump()` output.
- **Generic processor architecture** — Refactored event processing into a generic processor with pluggable policy evaluation, replacing the monolithic handler.

### Removed
- **API key authentication** — Removed the legacy `X-API-Key` header flow and `api_keys` table. All auth is now wallet-based.
- **Svix integration** — Fully removed Svix client, configuration, and migration artifacts in favor of self-hosted Convoy.
- **Dead code cleanup** — Removed unused repository methods, duplicate utility functions, and stale re-exports.

### Security
- **IDOR prevention** — All endpoints enforce wallet-scoped ownership; no resource can be read or mutated by a non-owner.
- **RLS at the DB layer** — Even if application middleware is bypassed, Supabase RLS policies prevent cross-wallet data access.
- **Webhook signature verification** — Inbound webhooks from Goldsky and the x402 facilitator are HMAC-verified before processing.
- **Secret masking** — All secret fields use `SecretStr`, preventing accidental logging or serialization of credentials.
- **Nonce replay protection** — SIWE nonces are single-use and stored in Redis with TTL expiry.
