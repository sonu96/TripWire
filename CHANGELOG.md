# Changelog

All notable changes to TripWire are documented in this file.

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
