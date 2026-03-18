# TripWire Security Model

> Last updated: 2026-03-17

This document describes TripWire's authentication, authorization, and secret management as implemented in the codebase. It is intended as an honest reference for auditors and contributors. Known bugs and gaps are called out explicitly.

---

## Table of Contents

1. [Authentication Overview](#1-authentication-overview)
2. [SIWE Message Format](#2-siwe-message-format)
3. [Request Signing Flow](#3-request-signing-flow)
4. [Replay Prevention](#4-replay-prevention)
5. [MCP Authentication](#5-mcp-authentication)
6. [Identity-Based Access Control (ERC-8004)](#6-identity-based-access-control-erc-8004)
7. [Secret Flows](#7-secret-flows)
8. [Ownership Enforcement](#8-ownership-enforcement)
9. [Row Level Security](#9-row-level-security)
10. [x402 Payment Security](#10-x402-payment-security)
11. [Webhook Delivery Security](#11-webhook-delivery-security)
12. [Dev Mode](#12-dev-mode)

---

## 1. Authentication Overview

TripWire uses **wallet-based authentication exclusively**. There are no API keys, no session tokens, and no username/password credentials anywhere in the system.

Every authenticated request requires a **Sign-In with Ethereum (SIWE)** message signed by the caller's private key. The server verifies the EIP-191 `personal_sign` signature, recovers the signer address, and compares it to the claimed address. A server-issued nonce prevents replay attacks.

The auth dependency is `require_wallet_auth` in `tripwire/api/auth.py`. It is injected via FastAPI's `Depends()` on all CRUD and management routes.

Required headers on every authenticated request:

| Header | Description |
|---|---|
| `X-TripWire-Address` | Caller's Ethereum address (0x...) |
| `X-TripWire-Signature` | EIP-191 personal_sign hex signature |
| `X-TripWire-Nonce` | Server-issued nonce from `GET /auth/nonce` |
| `X-TripWire-Issued-At` | ISO-8601 timestamp when the message was signed |
| `X-TripWire-Expiration` | ISO-8601 expiration timestamp |

---

## 2. SIWE Message Format

Messages follow **EIP-4361** (Sign-In with Ethereum). The format is:

```
{domain} wants you to sign in with your Ethereum account:
{address}

{statement}

URI: https://{domain}
Version: 1
Chain ID: {chain_id}
Nonce: {nonce}
Issued At: {issued_at}
Expiration Time: {expiration_time}
```

The **statement** field is constructed as:

```
{HTTP_METHOD} {PATH} {SHA256_OF_BODY}
```

This binds the signature to a specific HTTP request (method, path, and body), preventing a signature for one request from being used against a different endpoint or with a different payload.

### KNOWN BUG: Chain ID Mismatch

The server-side `_build_siwe_message` in `tripwire/api/auth.py` defaults to **`chain_id=8453`** (Base mainnet) at line 33:

```python
def _build_siwe_message(
    ...
    chain_id: int = 8453,
) -> str:
```

The SDK's `_build_siwe_message` in `sdk/tripwire_sdk/signer.py` hardcodes **`Chain ID: 1`** (Ethereum mainnet) at line 40:

```python
f"Chain ID: 1\n"
```

**Impact:** Any request signed by the SDK will produce a different SIWE message than what the server reconstructs. Signature verification will fail because the recovered address will not match. The SDK and server are currently incompatible on this field.

**Severity:** High. The SDK cannot authenticate against the server until one side is updated to match the other.

---

## 3. Request Signing Flow

Step-by-step flow using the SDK (`sdk/tripwire_sdk/signer.py`):

1. **Fetch a nonce** -- `GET /auth/nonce` returns a `secrets.token_urlsafe(32)` nonce stored in Redis with a 5-minute TTL. The nonce endpoint is rate-limited to 30 requests/minute per IP and 1000/minute globally.

2. **Compute the body hash** -- `SHA-256` of the request body bytes (empty body hashes the empty string).

3. **Build the SIWE message** -- Using the caller's address, nonce, method, path, and body hash as the statement.

4. **Sign with EIP-191** -- `personal_sign` via `eth_account.messages.encode_defunct` + `Account.sign_message`.

5. **Attach headers** -- Set all five `X-TripWire-*` headers on the request.

6. **Server verifies** -- Reconstructs the same SIWE message, recovers the signer via `Account.recover_message`, compares addresses (case-insensitive), atomically consumes the nonce from Redis, and checks expiration.

SDK convenience function:

```python
from tripwire_sdk.signer import make_auth_headers

headers = make_auth_headers(
    key_or_account=private_key,
    address="0x...",
    path="/endpoints",
    nonce=nonce,
    method="POST",
    body_bytes=json.dumps(payload).encode(),
)
```

---

## 4. Replay Prevention

Nonces are the sole replay prevention mechanism for the REST API.

- **Generation**: `secrets.token_urlsafe(32)` -- 256 bits of entropy.
- **Storage**: Redis key `siwe:nonce:{nonce}` with value `"1"` and a 300-second (5-minute) TTL.
- **Consumption**: `await r.delete(f"siwe:nonce:{nonce}")` -- atomic. Returns 1 if the key existed, 0 if not. A return of 0 means the nonce was already used or expired, and the request is rejected.
- **Single-use**: Each nonce can authenticate exactly one request. After consumption it cannot be reused.

The nonce issuance endpoint (`GET /auth/nonce`) is rate-limited:
- 30 requests/minute per IP address
- 1000 requests/minute globally

Expiration is also checked: the `X-TripWire-Expiration` header is parsed as ISO-8601 and compared to `datetime.now(UTC)`. Expired signatures are rejected regardless of nonce validity.

---

## 5. MCP Authentication

The MCP server (`tripwire/mcp/server.py`) implements a **3-tier authentication model** for tool invocations over JSON-RPC.

### 5.1 Tier Overview

| Tier | Auth Required | Payment Required | Tools |
|---|---|---|---|
| `PUBLIC` | None | No | `initialize`, `tools/list` |
| `SIWX` | SIWE wallet signature | No | `list_triggers`, `delete_trigger`, `list_templates`, `get_trigger_status`, `search_events` |
| `X402` | x402 payment header | Yes | `register_middleware` ($0.003), `create_trigger` ($0.003), `activate_template` ($0.001) |

### 5.2 PUBLIC Tier

The `initialize` and `tools/list` JSON-RPC methods require no authentication. They are handled before the auth dispatch logic and return protocol metadata and the tool catalog respectively.

### 5.3 SIWX Tier (Wallet Signature)

Uses the same SIWE flow as the REST API. The MCP auth module (`tripwire/mcp/auth.py`) re-implements the verification logic in `_verify_siwe()` rather than calling the FastAPI dependency directly. The flow is identical: verify expiration, check issued-at tolerance (`auth_timestamp_tolerance_seconds`, default 300s), compute body hash, reconstruct the SIWE message, recover the signer, compare addresses, and atomically consume the nonce from Redis.

x402 V2 introduces **SIWX (Sign-In With X)** as the standardized replacement for custom SIWE header flows. SIWX is transmitted via the `SIGN-IN-WITH-X` header and provides a unified cross-chain identity primitive. TripWire accepts both the legacy custom SIWE headers (`X-TripWire-*`) and the V2 `SIGN-IN-WITH-X` header for backward compatibility.

After successful SIWE verification, the server performs an ERC-8004 identity resolution on the recovered address (defaulting to Base chain 8453) to populate the `MCPAuthContext` with identity and reputation data.

### 5.4 X402 Tier (Payment Required)

For paid tools, the caller must include an x402 payment header containing a payment proof. x402 V2 uses the `PAYMENT-SIGNATURE` header; the legacy V1 `X-PAYMENT` header is still accepted but deprecated.

Verification flow:

1. **Replay prevention**: SHA-256 hash of the payment header is used as a dedup key (`x402:payment:{hash}:{tool_name}`). An atomic `SET NX` with 24-hour TTL ensures only one request can claim a given payment proof.
2. **Payment verification**: The x402 facilitator client verifies the payment proof against the expected price and payment network (`eip155:8453`).
3. **Deferred settlement**: If verification succeeds, the payment is NOT settled immediately. Settlement happens only after the tool handler executes successfully, via `settle_payment()`.
4. **Settlement failure handling**: If settlement fails after successful tool execution, the tool result is **withheld** (not returned to the caller), the dedup key is deleted to allow retry, and a `PAYMENT_REQUIRED` error is returned.

### 5.5 Per-Address Rate Limiting

Every authenticated MCP tool call (SIWX or X402) is rate-limited per wallet address:

- **Limit**: 60 tool calls per minute per address
- **Mechanism**: Redis `INCR` on key `mcp:rate:{address}` with a 60-second expiry
- **Failure mode**: If Redis is unavailable, rate limiting **fails open** (requests are allowed through). Auth has already passed at this point.

### 5.6 Reputation Gating

Each `ToolDef` has a `min_reputation` field (float). If greater than 0, the server compares it against the caller's `reputation_score` from the `MCPAuthContext` (populated via ERC-8004 identity resolution).

**Current state**: All tools have `min_reputation=0.0`, so reputation gating is effectively disabled. The infrastructure is in place for future use.

### 5.7 JSON-RPC Error Codes

| Code | Constant | Meaning |
|---|---|---|
| -32000 | `AUTH_REQUIRED` | Missing or invalid SIWE headers |
| -32001 | `REPUTATION_TOO_LOW` | Caller's reputation below tool threshold |
| -32002 | `PAYMENT_REQUIRED` | Missing, invalid, or replayed x402 payment |
| -32003 | `RATE_LIMITED` | Per-address rate limit exceeded (60/min) |
| -32601 | `METHOD_NOT_FOUND` | Unknown tool or JSON-RPC method |
| -32603 | `INTERNAL_ERROR` | Auth service unavailable or tool execution failure |

---

## 6. Identity-Based Access Control (ERC-8004)

TripWire integrates with the ERC-8004 onchain AI agent identity registry to gate access based on agent class and reputation. Identity data is resolved from registry contracts on Base (chain 8453) via direct JSON-RPC calls — no self-reported claims are trusted.

### 6.1 Reputation Gating in MCP

After SIWE/X402 authentication succeeds, the MCP auth layer resolves the caller's ERC-8004 identity on Base (chain 8453) and populates `MCPAuthContext` with the agent's `agent_class` and `reputation_score`. Each `ToolDef` declares a `min_reputation` threshold. If `tool.min_reputation > 0` and the caller's `identity.reputation_score` falls below the threshold, the server returns JSON-RPC error `-32001` (`REPUTATION_TOO_LOW`) before the tool handler executes.

**Current state**: All tools have `min_reputation=0.0`, so reputation gating is wired but not enforced. The mechanism is ready for activation on a per-tool basis without code changes.

### 6.2 Identity Policies on Endpoints

`EndpointPolicies` supports two identity-based policy fields evaluated during event processing, **before** webhook dispatch:

- **`required_agent_class`**: The webhook fires only if the sender's ERC-8004 `agent_class` matches the policy value. Events from agents of a different class are silently filtered — no webhook is dispatched.
- **`min_reputation_score`**: The webhook fires only if the sender's ERC-8004 `reputation_score` meets or exceeds the threshold. Events from agents below the threshold are silently filtered.

These checks run in the event processor after deduplication and finality checks but before Convoy dispatch or direct httpx delivery. A filtered event is not retried or queued — it is dropped at evaluation time.

### 6.3 Trust Properties

ERC-8004 identity data has stronger trust guarantees than application-managed identity because:

- **Onchain source of truth**: Identity records (agent class, reputation score) are read directly from registry contracts via `eth_call`, not from any TripWire-internal database or self-reported API field.
- **External reputation**: Reputation scores are aggregated from onchain feedback submitted by other agents and protocols. TripWire does not compute or influence these scores.
- **Deterministic deployment**: Registry contracts are CREATE2-deployed at the same address across all supported chains, eliminating address confusion across networks.

### 6.4 Cache Staleness and Security Implications

Identity resolution results are cached in-process with a **300-second (5-minute) TTL**. This means:

- A reputation drop (e.g., from onchain slashing or negative feedback) takes up to 5 minutes to propagate to TripWire's access control decisions.
- A compromised or misbehaving agent whose reputation is slashed onchain can continue to pass reputation gates and trigger webhook deliveries for up to 5 minutes after the slash transaction is finalized.
- Agent class changes are similarly delayed — an agent reclassified onchain will retain its old class in TripWire's cache until the entry expires.

This is a deliberate tradeoff: the cache reduces RPC call volume and latency for the common case (identity data changes infrequently), at the cost of a bounded window of stale authorization decisions. For high-sensitivity endpoints, operators should set reputation thresholds with margin to account for this propagation delay.

---

## 7. Secret Flows

TripWire handles four distinct inbound/outbound secret flows, plus several configuration secrets.

### 7.1 Goldsky -> TripWire (Inbound Webhook)

**Secret**: `GOLDSKY_WEBHOOK_SECRET` (stored as `SecretStr` in settings)

**What Goldsky Turbo does before delivery**: Goldsky Turbo applies a SQL transform and event filter to the raw chain data before sending it to TripWire. Only logs matching the configured topic0 filter (e.g., the `Transfer` event selector for ERC-3009) are emitted; the payload arrives already decoded into structured fields. TripWire does not receive raw RLP-encoded logs — it receives pre-processed, filtered event records.

**Flow**: Goldsky sends these decoded ERC-3009 events to `POST /ingest/goldsky`. The `_verify_goldsky_request` dependency in `tripwire/api/routes/ingest.py` checks the `Authorization` header against `Bearer {secret}` using `hmac.compare_digest` (constant-time comparison).

**Dev bypass**: If the secret is empty and `APP_ENV=development`, the check is skipped. If the secret is empty and the environment is NOT development, the server returns HTTP 500.

### 7.2 TripWire -> Goldsky Edge (Outbound RPC)

**Secret**: `GOLDSKY_EDGE_API_KEY` (stored as `SecretStr` in settings)

**Flow**: TripWire makes outbound JSON-RPC calls to Goldsky Edge managed RPC endpoints for finality checking (`eth_blockNumber`) and identity resolution (`eth_call`). The shared RPC client in `tripwire/rpc.py` attaches the key as a `Bearer` token in the `Authorization` header on every request. The key is injected once at client construction time (lazy singleton) and applies to all subsequent calls across `finality_poller.py`, `identity/`, and `reputation/`.

**Optional**: If `GOLDSKY_EDGE_API_KEY` is empty, the `Authorization` header is omitted entirely. TripWire will still function against public RPC endpoints, but Goldsky Edge rate limits will apply.

### 7.3 Facilitator -> TripWire (Inbound)

**Secret**: `FACILITATOR_WEBHOOK_SECRET` (stored as `SecretStr` in settings)

**Flow**: The x402 facilitator sends pre-settlement ERC-3009 authorization data to `POST /ingest/facilitator`. The `_verify_facilitator_request` dependency in `tripwire/api/routes/facilitator.py` checks the `Authorization` header using the same pattern as Goldsky (Bearer token, `hmac.compare_digest`).

**Dev bypass**: Same as Goldsky -- skipped when secret is empty in development mode.

#### Unified Event Lifecycle (Facilitator-Goldsky Correlation)

The facilitator fast path and the Goldsky onchain path share a **unified event lifecycle** via the `record_nonce_or_correlate` Postgres function (migration 020).

1. **Facilitator path**: When the facilitator delivers a pre-settlement event, the processor records the nonce with `source="facilitator"` and a pre-generated `event_id`. The event is created with status `pre_confirmed`.

2. **Goldsky arrival**: When Goldsky later delivers the same nonce (the real onchain transaction), `record_nonce_or_correlate` detects the existing facilitator-claimed nonce via `SELECT FOR UPDATE` (preventing TOCTOU races) and returns correlation info (`existing_event_id`, `existing_source="facilitator"`).

3. **Promotion**: Instead of silently dropping the Goldsky event as a duplicate, the processor calls `_promote_pre_confirmed_event`, which updates the existing event row with real onchain data (tx_hash, block_number, block_hash, log_index), checks finality, and dispatches the appropriate webhook (`payment.confirmed` or `payment.finalized`).

This ensures consumers always receive a confirmation webhook after the facilitator's provisional notification, closing the gap where the Goldsky event was previously rejected as a duplicate.

#### Execution State Enrichment

Webhook payloads now carry three trust-boundary fields derived from the event type and finality status:

| Field | Type | Description |
|---|---|---|
| `execution_state` | `provisional` / `confirmed` / `finalized` / `reorged` | Current lifecycle state |
| `safe_to_execute` | `bool` | Whether the consumer should act on this event |
| `trust_source` | `facilitator` / `onchain` | Origin of the trust assertion |

The facilitator fast path explicitly produces `execution_state=provisional`, `safe_to_execute=false`, `trust_source=facilitator`. Consumers MUST NOT execute irreversible actions on provisional payloads. Only `payment.finalized` events (or confirmed events where `finality.is_finalized=true`) set `safe_to_execute=true` with `trust_source=onchain`.

### 7.4 TripWire -> Developer (Outbound Webhook Signing)

This is the most nuanced flow, and it has a known bug in the MCP path.

**REST API path** (`tripwire/api/routes/endpoints.py`):

1. When an endpoint is registered with `mode=execute`, a `secrets.token_hex(32)` webhook secret is generated.
2. The secret is passed to Convoy via `provider.create_endpoint(secret=webhook_secret)`.
3. Convoy stores the secret internally and uses it for HMAC signing of outbound webhooks.
4. The secret is returned to the caller **exactly once** in the registration response (`endpoint.webhook_secret = webhook_secret`).
5. The secret is **never stored in the TripWire database**. Migration 016 (`tripwire/db/migrations/016_webhook_secret_drop.sql`) explicitly dropped the `webhook_secret` column from the `endpoints` table.

**KNOWN BUG: MCP path** (`tripwire/mcp/tools.py`, `register_middleware` handler):

The MCP `register_middleware` tool generates a webhook secret at line 76:

```python
webhook_secret = secrets.token_hex(32)
```

But this secret is **never passed to Convoy**. The handler inserts the endpoint row into the database and returns the secret to the caller, but it never calls `provider.create_endpoint()` or any Convoy API. The secret is returned in the response but is **useless** -- Convoy never receives it, so webhooks for MCP-registered endpoints will not be HMAC-signed, and the developer has a secret that signs nothing.

**Impact**: Endpoints registered via MCP will not have Convoy project/endpoint IDs set. Webhook delivery via Convoy will be skipped entirely for these endpoints (the dispatcher checks for `convoy_project_id` and skips if missing).

### 7.5 Vestigial: webhook_signing_secret

`settings.py` defines `webhook_signing_secret: SecretStr = SecretStr("")` with the comment "Default HMAC secret, can be overridden per endpoint". This field is **never referenced** anywhere in the codebase outside of `settings.py` itself. It appears to be a leftover from a previous design where TripWire performed HMAC signing directly (before Convoy was adopted). It can be safely removed.

### 7.6 Secret Summary Table

| Secret | Storage | Type | Used By |
|---|---|---|---|
| `SUPABASE_SERVICE_ROLE_KEY` | Environment / `.env` | `SecretStr` | Supabase client authentication |
| `CONVOY_API_KEY` | Environment / `.env` | `SecretStr` | Convoy REST API Bearer token |
| `GOLDSKY_WEBHOOK_SECRET` | Environment / `.env` | `SecretStr` | Validates inbound Goldsky webhooks |
| `GOLDSKY_EDGE_API_KEY` | Environment / `.env` | `SecretStr` | Goldsky Edge managed RPC auth |
| `GOLDSKY_API_KEY` | Environment / `.env` | `SecretStr` | Goldsky CLI (pipeline management) |
| `FACILITATOR_WEBHOOK_SECRET` | Environment / `.env` | `SecretStr` | Validates inbound facilitator webhooks |
| `SENTRY_DSN` | Environment / `.env` | `SecretStr` | Sentry error tracking (optional) |
| `webhook_signing_secret` | Environment / `.env` | `SecretStr` | **VESTIGIAL -- never used** |
| Per-endpoint webhook secret | Convoy internal storage only | Generated `token_hex(32)` | HMAC signing of outbound webhooks |
| SIWE nonces | Redis (5-min TTL) | `token_urlsafe(32)` | Replay prevention |
| x402 payment dedup keys | Redis (24h TTL) | SHA-256 of payment header | Payment replay prevention |

### 7.7 Future: Envelope Encryption for Direct httpx Path

TripWire has a dual-path webhook delivery architecture: Convoy for managed delivery, and a direct httpx POST path for low-latency scenarios. The direct path currently does **not** perform HMAC signing. An envelope encryption scheme is planned but not yet implemented. See [Section 11.2](#112-direct-httpx-fast-path) for delivery security details.

---

## 8. Ownership Enforcement

All endpoint and trigger management routes enforce **application-layer ownership checks**. The pattern is consistent:

1. The `require_wallet_auth` dependency recovers and verifies the caller's wallet address.
2. The route handler fetches the target resource from the database.
3. A helper function compares the resource's `owner_address` to the caller's address (case-insensitive).
4. If they do not match, the route returns HTTP 403.

REST API (`tripwire/api/routes/endpoints.py`):

```python
def _verify_ownership(endpoint_row: dict, wallet_address: str) -> None:
    if endpoint_row.get("owner_address", "").lower() != wallet_address.lower():
        raise HTTPException(status_code=403, detail="Not authorized to access this endpoint")
```

This function is called in `get_endpoint`, `update_endpoint`, and `deactivate_endpoint`.

For `list_endpoints`, ownership is enforced via a Supabase query filter: `.eq("owner_address", wallet_auth.wallet_address)`.

MCP tools (`tripwire/mcp/tools.py`) use the same pattern, comparing `ctx.agent_address.lower()` against the resource's `owner_address.lower()`. The tools `create_trigger`, `delete_trigger`, `activate_template`, `get_trigger_status`, and `search_events` all verify ownership before returning data or mutating state.

---

## 9. Row Level Security

### Current State: RLS Policies Exist but Are Inert

Migration `011_rls_policies.sql` creates RLS policies on four tables:

- `endpoints` -- direct match on `owner_address`
- `subscriptions` -- join through `endpoints.owner_address`
- `events` -- join through `endpoints.owner_address`
- `webhook_deliveries` -- join through `endpoints.owner_address`

All policies use the PostgreSQL session variable `app.current_wallet`, set via a `SECURITY DEFINER` function `set_wallet_context(wallet_address text)` that calls `set_config('app.current_wallet', wallet_address, true)`.

A FastAPI dependency `get_supabase_scoped` is defined in `tripwire/api/__init__.py` that would call this function before returning the Supabase client:

```python
def get_supabase_scoped(request: Request, wallet: WalletAuthContext):
    sb = request.app.state.supabase
    sb.postgrest.auth(token=None, headers={"x-wallet-address": wallet.wallet_address})
    sb.rpc("set_wallet_context", {"wallet_address": wallet.wallet_address}).execute()
    return sb
```

**However, `get_supabase_scoped` is never called from any route handler.** All routes use `get_supabase` (the unscoped variant), which returns the Supabase client with the `service_role` key. The service role key bypasses RLS entirely in Supabase.

**Impact**: RLS policies are deployed to the database but have no effect. All data access goes through the service role, which is exempt from RLS. Ownership enforcement relies entirely on application-layer checks in route handlers (section 7).

**Assessment**: The application-layer checks are present and consistent across all routes. RLS would provide defense-in-depth if `get_supabase_scoped` were wired into the route dependencies. As it stands, a bug in any single route handler's ownership check could expose data across tenants. Wiring RLS is a recommended hardening step.

---

## 10. x402 Payment Security

x402 payment gating is applied in two places:

### 10.1 REST API Endpoint Registration

The `POST /endpoints` route can be gated by x402 payment. Payment verification is handled by middleware that sets `request.state.payment_tx_hash` and `request.state.payment_chain_id` on the request. If present, the endpoint row is updated with the registration transaction hash and chain ID.

Configuration:
- `x402_registration_price`: Default `$1.00`
- `x402_network`: Default `eip155:8453` (Base mainnet)
- `tripwire_treasury_address`: USDC recipient address (required in production)

### 10.2 MCP Tool Calls

X402-tier MCP tools (`register_middleware`, `create_trigger`, `activate_template`) require an x402 payment header. x402 V2 callers should use the `PAYMENT-SIGNATURE` header; the legacy V1 `X-PAYMENT` header is still accepted but deprecated. The payment is verified against the tool's declared price via the x402 facilitator, and settled only after successful tool execution.

### 10.3 Per-Trigger Payment Gating (Phase C3)

Dynamic triggers can require that decoded events contain payment metadata before dispatch. This is distinct from x402 tool-level payment — it gates the *event delivery pipeline*, not the MCP call.

| Field | Type | Description |
|---|---|---|
| `require_payment` | bool | Enable payment gating on this trigger |
| `payment_token` | address/null | Required token contract (null = any) |
| `min_payment_amount` | string/null | Minimum amount in smallest unit |

Payment gating runs *before* deduplication in the unified processor pipeline, so rejected events do not consume nonces. See [TWSS-1 Three-Layer Gating](SKILL-SPEC.md#4-three-layer-gating) for the full `can_pay? -> can_trust? -> is_safe?` model.

### 10.4 ERC-3009 Model

x402 payments use **ERC-3009 `transferWithAuthorization`** -- gasless USDC transfers where the payer signs an authorization that is submitted onchain by the facilitator. The facilitator verifies the ERC-3009 signature before calling TripWire's `/ingest/facilitator` endpoint.

The facilitator endpoint validates:
- `signature_verified` must be `true` (rejects unverified authorizations)
- `token` must be a known USDC contract address
- `chain_id` must be a supported chain
- `from_address` and `to_address` must be valid Ethereum addresses (regex-validated)

### 10.4 Webhook Event Types

The following event types are emitted through the webhook delivery pipeline:

| Event Type | Trigger | `safe_to_execute` |
|---|---|---|
| `payment.pre_confirmed` | Facilitator delivers pre-settlement authorization | `false` |
| `payment.pending` | Goldsky delivers onchain event, finality not yet reached | `false` |
| `payment.confirmed` | Goldsky delivers onchain event, basic confirmation | `false` |
| `payment.finalized` | Event reaches chain-specific finality depth | `true` |
| `payment.failed` | Processing failure | `false` |
| `payment.reorged` | Block containing the event was reorged | `false` |

Consumers should gate irreversible actions on `payment.finalized` (or check `safe_to_execute=true`).

---

## 11. Webhook Delivery Security

### 11.1 Convoy Circuit Breaker

The Convoy client (`tripwire/webhook/convoy_client.py`) implements a **circuit breaker** to protect against cascading failures when Convoy is unavailable.

| Parameter | Value | Description |
|---|---|---|
| Failure threshold | 5 consecutive failures | Trips the circuit from `closed` to `open` |
| Recovery timeout | 30 seconds | Time before allowing a probe request (`half_open`) |
| Half-open timeout | 10 seconds | Shorter HTTP timeout for the probe request |

**State machine**: `closed` (normal) -> `open` (fast-fail all requests) -> `half_open` (allow one probe). A successful probe transitions back to `closed`; a failed probe returns to `open`.

When the circuit is open, all Convoy calls raise `ConvoyCircuitOpenError` immediately without making a network request. This prevents request pile-up and allows the direct httpx fast path to continue operating independently.

The circuit state is exposed via a Prometheus gauge (`tripwire_convoy_circuit_state`: 0=closed, 1=open, 2=half_open) and the `get_circuit_state()` diagnostic function.

### 11.2 Direct httpx Fast Path

The direct httpx POST path bypasses Convoy entirely for low-latency scenarios. It currently does **not** perform HMAC signing. An envelope encryption scheme is planned but not yet implemented. The circuit breaker does not affect the direct path -- it continues to operate even when Convoy is down.

---

## 12. Dev Mode

`dev_server.py` provides a development-only auth bypass. It:

1. Forces `APP_ENV=development` via `os.environ.setdefault`.
2. Overrides the `require_wallet_auth` dependency globally with a function that returns a hardcoded `WalletAuthContext` (default: Hardhat account #0, `0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`).
3. Prints a prominent banner warning that auth is bypassed.

The dev wallet address is configurable via the `DEV_WALLET_ADDRESS` environment variable.

**Important**: `dev_server.py` is a separate entrypoint. The production entrypoint (`tripwire/main.py`) does not import or reference it. The auth bypass is isolated to this single file.

Additionally, both the Goldsky and facilitator webhook authentication dependencies skip verification when the respective secret environment variable is empty and `APP_ENV=development`. This allows local development without configuring external service credentials.

---

## 13. x402 V1 to V2 Migration

x402 V2 introduces updated header conventions and authentication primitives. TripWire is migrating to support both V1 and V2 during the transition period.

### Header Changes

| Purpose | V1 (Deprecated) | V2 | Status |
|---|---|---|---|
| Payment proof | `X-PAYMENT` | `PAYMENT-SIGNATURE` | Both accepted; V1 deprecated |
| Wallet identity (SIWX) | Custom `X-TripWire-*` headers | `SIGN-IN-WITH-X` | Both accepted; custom headers deprecated for MCP |

### Authentication Changes

- **SIWX replaces custom SIWE**: x402 V2 standardizes wallet authentication under the SIWX (Sign-In With X) primitive, transmitted via the `SIGN-IN-WITH-X` header. This replaces TripWire's custom `X-TripWire-*` SIWE header scheme for the MCP path. The REST API continues to use `X-TripWire-*` headers.
- **Bazaar V2 endpoint**: `GET /discovery/resources` serves as the V2 Bazaar discovery endpoint, complementing the existing `GET /.well-known/x402-manifest.json` (V1).

### Migration Timeline

- **Current**: Both V1 and V2 headers are accepted. V1 headers are deprecated but functional.
- **Future**: V1 headers will be removed in a future release. Callers should migrate to `PAYMENT-SIGNATURE` and `SIGN-IN-WITH-X` headers.

---

## Known Issues Summary

| Issue | Location | Severity | Status | Description |
|---|---|---|---|---|
| Chain ID mismatch | `tripwire/api/auth.py:33` vs `sdk/tripwire_sdk/signer.py:40` | High | Open | Server uses 8453, SDK hardcodes 1. SDK-signed requests will fail verification. |
| MCP register_middleware webhook secret | `tripwire/mcp/tools.py:76` | High | Open | Generates a secret but never passes it to Convoy. Returned secret is useless. No Convoy project/endpoint created. |
| RLS policies inert | `tripwire/api/__init__.py:13` | Medium | Open | `get_supabase_scoped` exists but is never called. All routes use the service role, bypassing RLS. |
| ~~Vestigial webhook_signing_secret~~ | ~~`tripwire/config/settings.py`~~ | ~~Low~~ | **Resolved** | Removed from settings.py. Was dead configuration never referenced in any code path. |

### Resolved Issues

| Issue | Resolution | Migration |
|---|---|---|
| Nonce TOCTOU race (facilitator vs Goldsky) | Fixed via `SELECT FOR UPDATE` in the `record_nonce_or_correlate` Postgres function. The conflicting row is locked before reading, eliminating the read-then-write race window. | `020_unified_event_lifecycle.sql` |
