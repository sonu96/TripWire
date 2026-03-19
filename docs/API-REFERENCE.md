# TripWire API Reference

Complete reference for the TripWire REST API. All authenticated endpoints use SIWE (Sign-In with Ethereum) wallet authentication.

---

## 1. Base URL and Versioning

All business-logic endpoints are prefixed with `/api/v1`. Operational endpoints (`/health`, `/ready`, `/metrics`) and discovery endpoints (`/.well-known/...`, `/auth/...`) live at the root.

```
Base URL: https://<host>:3402/api/v1
```

| Path prefix | Description |
|---|---|
| `/api/v1/endpoints` | Endpoint CRUD |
| `/api/v1/events` | Event history |
| `/api/v1/deliveries` | Delivery tracking |
| `/api/v1/ingest` | Goldsky and facilitator ingestion |
| `/api/v1/stats` | Processing statistics and agent metrics |
| `/api/v1/auth/session` | Keeper session management (open, query, close) |
| `/auth` | Nonce issuance (root-level, no `/api/v1`) |
| `/.well-known` | x402 manifest + TWSS-1 skill spec (root-level) |
| `/health`, `/ready`, `/metrics` | Operational (root-level) |
| `/mcp` | MCP server (root-level) |

---

## 2. Authentication

TripWire uses **SIWE (Sign-In with Ethereum, EIP-4361)** wallet authentication. There are no API keys. Every authenticated REST API request requires five headers. For MCP requests, x402 V2 clients can alternatively use the `SIGN-IN-WITH-X` (SIWX) header -- see [MCP-SERVER.md](./MCP-SERVER.md) for details.

### Required Headers

| Header | Description |
|---|---|
| `X-TripWire-Address` | Caller's Ethereum address (`0x...`) |
| `X-TripWire-Signature` | EIP-191 `personal_sign` hex signature (`0x...`) |
| `X-TripWire-Nonce` | Nonce obtained from `GET /auth/nonce` (single-use, 5-min TTL) |
| `X-TripWire-Issued-At` | ISO-8601 timestamp when the message was signed |
| `X-TripWire-Expiration` | ISO-8601 expiration timestamp |

### Verification Flow

1. Read the request body and compute its SHA-256 hash.
2. Reconstruct the SIWE message: the statement is `{METHOD} {PATH} {BODY_SHA256}`.
3. Recover the signer from the EIP-191 signature.
4. Compare recovered address to `X-TripWire-Address` (case-insensitive).
5. Atomically consume the nonce from Redis (rejects replayed or expired nonces).
6. Validate `X-TripWire-Expiration` has not passed.

### SIWE Message Format

```
tripwire.dev wants you to sign in with your Ethereum account:
0xYourAddress

POST /api/v1/endpoints abc123def456...

URI: https://tripwire.dev
Version: 1
Chain ID: 8453
Nonce: <nonce-from-auth-endpoint>
Issued At: 2026-03-16T12:00:00Z
Expiration Time: 2026-03-16T12:05:00Z
```

### Quick Example

```bash
# 1. Get a nonce
NONCE=$(curl -s https://tripwire.dev/auth/nonce | jq -r '.nonce')

# 2. Build the SIWE message, sign it with your wallet, then call
curl -X POST https://tripwire.dev/api/v1/endpoints \
  -H "Content-Type: application/json" \
  -H "X-TripWire-Address: 0xYourAddress" \
  -H "X-TripWire-Signature: 0xSignatureHex" \
  -H "X-TripWire-Nonce: $NONCE" \
  -H "X-TripWire-Issued-At: 2026-03-16T12:00:00Z" \
  -H "X-TripWire-Expiration: 2026-03-16T12:05:00Z" \
  -d '{"url":"https://myapp.com/webhook","mode":"execute","chains":[8453],"recipient":"0xRecipient","owner_address":"0xYourAddress"}'
```

For full details on the SIWE implementation and security properties, see [SECURITY.md](./SECURITY.md).

---

## 3. Rate Limiting

| Scope | Limit | Applies to |
|---|---|---|
| Ingest | 100 requests/minute | `/api/v1/ingest/*` |
| CRUD | 30 requests/minute | All other authenticated endpoints |
| Nonce | 30 requests/minute per caller, 1000/minute global | `GET /auth/nonce` |

**Key derivation**: Rate limit keys are derived from `X-TripWire-Address` (via the `Authorization` header) when present, falling back to the client IP address for unauthenticated requests.

**429 response**: When a rate limit is exceeded, the server responds with HTTP 429 and a `Retry-After` header (in seconds).

```json
{
  "detail": "Rate limit exceeded: 30 per 1 minute"
}
```

---

## 4. Endpoints API

Manage webhook endpoints. All routes require SIWE authentication. The authenticated wallet becomes the `owner_address` and can only access its own endpoints.

### POST /api/v1/endpoints

Register a new webhook endpoint. For `execute` mode, a Convoy application and endpoint are created automatically.

**Rate limit**: 30/min

**Request body**:

```json
{
  "url": "https://myapp.com/webhook",
  "mode": "execute",
  "chains": [8453],
  "recipient": "0x1234567890abcdef1234567890abcdef12345678",
  "owner_address": "0xYourWalletAddress000000000000000000000000",
  "policies": {
    "min_amount": "1000000",
    "max_amount": null,
    "allowed_senders": null,
    "blocked_senders": null,
    "required_agent_class": null,
    "min_reputation_score": null,
    "finality_depth": null
  }
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | string | Yes | Webhook delivery URL. Must be HTTPS in production. |
| `mode` | `"notify"` or `"execute"` | Yes | Delivery mode. `execute` = Convoy webhook, `notify` = Supabase Realtime. |
| `chains` | int[] | Yes | Chain IDs to listen on (1 = Ethereum, 8453 = Base, 42161 = Arbitrum). |
| `recipient` | EthAddress | Yes | The onchain address receiving payments. |
| `owner_address` | EthAddress | Yes | Must match the authenticated wallet. |
| `policies` | object | No | Policy engine configuration (defaults applied if omitted). |

**Policies object**:

| Field | Type | Default | Description |
|---|---|---|---|
| `min_amount` | string or null | null | Minimum transfer amount (USDC 6 decimals). |
| `max_amount` | string or null | null | Maximum transfer amount. |
| `allowed_senders` | EthAddress[] or null | null | Whitelist of sender addresses. |
| `blocked_senders` | EthAddress[] or null | null | Blacklist of sender addresses. |
| `required_agent_class` | string or null | null | ERC-8004 agent class filter. When set, TripWire resolves the sender's onchain ERC-8004 identity and rejects events where the sender's `agent_class` does not match this value. Senders with no onchain identity are also rejected. |
| `min_reputation_score` | float (0-100) or null | null | Minimum ERC-8004 reputation score (0–100). Resolved from the onchain registry's 0–10000 basis-point value. Events from senders whose reputation falls below this threshold — or who have no onchain identity — are rejected. |
| `finality_depth` | int (1-64) or null | null | Block confirmations required. When null, uses the chain default (Ethereum: 12, Base: 3, Arbitrum: 1). |

**Response** (201 Created):

```json
{
  "id": "abc123def456789012345",
  "url": "https://myapp.com/webhook",
  "mode": "execute",
  "chains": [8453],
  "recipient": "0x1234567890abcdef1234567890abcdef12345678",
  "owner_address": "0xYourWalletAddress000000000000000000000000",
  "registration_tx_hash": null,
  "registration_chain_id": null,
  "policies": {
    "min_amount": "1000000",
    "max_amount": null,
    "allowed_senders": null,
    "blocked_senders": null,
    "required_agent_class": null,
    "min_reputation_score": null,
    "finality_depth": null
  },
  "active": true,
  "convoy_project_id": "proj_abc123",
  "convoy_endpoint_id": "ep_def456",
  "webhook_secret": "a1b2c3d4e5f6...64hexchars",
  "created_at": "2026-03-16T12:00:00Z",
  "updated_at": "2026-03-16T12:00:00Z"
}
```

> **Important**: `webhook_secret` is returned **only once** at creation time. It is never stored in TripWire's database and cannot be retrieved again. Store it securely -- you will need it to verify HMAC signatures on incoming webhooks.

**URL validation**: Endpoint URLs are validated for safety. Blocked: localhost, loopback, link-local, private IP ranges, and DNS names that resolve to private IPs. HTTP is allowed only when `APP_ENV=development`.

### GET /api/v1/endpoints

List all active endpoints belonging to the authenticated wallet.

**Rate limit**: 30/min

**Response** (200):

```json
{
  "data": [
    {
      "id": "abc123def456789012345",
      "url": "https://myapp.com/webhook",
      "mode": "execute",
      "chains": [8453],
      "recipient": "0x...",
      "owner_address": "0x...",
      "policies": { "..." },
      "active": true,
      "convoy_project_id": "proj_abc123",
      "convoy_endpoint_id": "ep_def456",
      "webhook_secret": null,
      "created_at": "2026-03-16T12:00:00Z",
      "updated_at": "2026-03-16T12:00:00Z"
    }
  ],
  "count": 1
}
```

> Note: `webhook_secret` is always `null` on list/get responses. It is only returned at creation time.

### GET /api/v1/endpoints/{endpoint_id}

Get a single endpoint by ID. Returns 403 if the endpoint does not belong to the authenticated wallet.

**Rate limit**: 30/min

**Response** (200): Same `Endpoint` object as above.

### PATCH /api/v1/endpoints/{endpoint_id}

Update an endpoint. Only provided fields are modified. Returns 403 if not the owner.

**Rate limit**: 30/min

**Request body** (all fields optional):

```json
{
  "url": "https://myapp.com/webhook-v2",
  "mode": "execute",
  "chains": [8453, 42161],
  "policies": {
    "min_amount": "5000000",
    "finality_depth": 12
  },
  "active": true
}
```

**Response** (200): Updated `Endpoint` object.

**Error**: 400 if no fields are provided.

### DELETE /api/v1/endpoints/{endpoint_id}

Soft-delete (deactivate) an endpoint. Sets `active = false`. Returns 403 if not the owner.

**Rate limit**: 30/min

**Response**: 204 No Content

---

## 5. Subscriptions API

Manage Supabase Realtime subscriptions for **notify-mode** endpoints. Events are pushed via Supabase Realtime instead of webhook delivery.

### POST /api/v1/endpoints/{endpoint_id}/subscriptions

Create a subscription for a notify-mode endpoint. Returns 400 if the endpoint is in `execute` mode.

**Rate limit**: 30/min

**Request body**:

```json
{
  "filters": {
    "chains": [8453],
    "senders": ["0xSenderAddress"],
    "recipients": ["0xRecipientAddress"],
    "min_amount": "1000000",
    "agent_class": "payment-bot"
  }
}
```

| Field | Type | Description |
|---|---|---|
| `filters.chains` | int[] or null | Chain IDs to filter. |
| `filters.senders` | string[] or null | Sender address whitelist. |
| `filters.recipients` | string[] or null | Recipient address whitelist. |
| `filters.min_amount` | string or null | Minimum amount filter (USDC 6 decimals). |
| `filters.agent_class` | string or null | ERC-8004 agent class filter. |

**Response** (201 Created):

```json
{
  "id": "sub_abc123def456789012",
  "endpoint_id": "abc123def456789012345",
  "filters": {
    "chains": [8453],
    "senders": ["0xSenderAddress"],
    "recipients": null,
    "min_amount": "1000000",
    "agent_class": null
  },
  "active": true,
  "created_at": "2026-03-16T12:00:00Z"
}
```

### GET /api/v1/endpoints/{endpoint_id}/subscriptions

List active subscriptions for an endpoint.

**Rate limit**: 30/min

**Response** (200): Array of `Subscription` objects.

### DELETE /api/v1/subscriptions/{subscription_id}

Deactivate a subscription. Verifies ownership through the parent endpoint.

**Rate limit**: 30/min

**Response**: 204 No Content

---

## 6. Events API

Query event history. Events are linked to endpoints via an `event_endpoints` many-to-many join table. All routes scope results to the authenticated wallet's endpoints.

### GET /api/v1/events

List events across all of the wallet's endpoints, with cursor pagination and optional filters.

**Rate limit**: 30/min

**Query parameters**:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cursor` | string | null | Event ID for keyset pagination. |
| `limit` | int (1-200) | 50 | Page size. |
| `event_type` | string | null | Filter by type (e.g. `payment.confirmed`). |
| `chain_id` | int | null | Filter by chain ID. |

**Event types**: `payment.confirmed`, `payment.pending`, `payment.pre_confirmed`, `payment.finalized`, `payment.failed`, `payment.reorged`, `wire.triggered`

**Response** (200):

```json
{
  "data": [
    {
      "id": "evt_abc123",
      "endpoint_id": "abc123def456789012345",
      "type": "payment.confirmed",
      "data": {
        "chain_id": 8453,
        "tx_hash": "0x...",
        "from_address": "0x...",
        "to_address": "0x...",
        "amount": "1000000"
      },
      "execution_state": "confirmed",
      "safe_to_execute": false,
      "trust_source": "onchain",
      "created_at": "2026-03-16T12:00:00Z"
    }
  ],
  "cursor": "evt_def456",
  "has_more": true
}
```

**Pagination**: Pass the returned `cursor` value as the `cursor` query parameter in the next request to fetch the next page. When `has_more` is `false`, there are no more results.

### GET /api/v1/events/{event_id}

Get a single event by ID. Verifies ownership through the parent endpoint.

**Rate limit**: 30/min

**Response** (200): Single `EventResponse` object (includes `execution_state`, `safe_to_execute`, and `trust_source` fields).

### GET /api/v1/endpoints/{endpoint_id}/events

List events for a specific endpoint with cursor pagination.

**Rate limit**: 30/min

**Query parameters**: `cursor`, `limit` (same as above).

**Response** (200): Same `EventListResponse` format (includes `execution_state`, `safe_to_execute`, and `trust_source` on each event).

### Execution State Fields on Events

All event responses (`GET /api/v1/events`, `GET /api/v1/events/{event_id}`, `GET /api/v1/endpoints/{endpoint_id}/events`) include three additional fields on every event object:

| Field | Type | Description |
|---|---|---|
| `execution_state` | string | Current execution state: `"provisional"`, `"confirmed"`, `"finalized"`, or `"reorged"`. |
| `safe_to_execute` | boolean | Whether the event is safe to act on. `true` only when the event has reached `"finalized"` state. |
| `trust_source` | string | Origin of the trust assertion: `"facilitator"` (pre-settlement from x402 facilitator) or `"onchain"` (confirmed via Goldsky indexing pipeline). |

---

## 7. Deliveries API

Track webhook delivery status and retry failed deliveries.

### GET /api/v1/deliveries

List deliveries across all of the wallet's endpoints, with optional filters and cursor pagination.

**Rate limit**: 30/min

**Query parameters**:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `endpoint_id` | string | null | Filter by endpoint. |
| `event_id` | string | null | Filter by event. |
| `status` | string | null | Filter by status (`pending`, `sent`, `delivered`, `failed`, `dead_lettered`). |
| `cursor` | string | null | Delivery ID for keyset pagination. |
| `limit` | int (1-200) | 50 | Page size. |

**Response** (200):

```json
{
  "data": [
    {
      "id": "del_abc123",
      "endpoint_id": "abc123def456789012345",
      "event_id": "evt_abc123",
      "provider_message_id": "convoy_msg_xyz",
      "status": "delivered",
      "execution_state": "finalized",
      "safe_to_execute": true,
      "created_at": "2026-03-16T12:00:05Z"
    }
  ],
  "cursor": "del_def456",
  "has_more": false
}
```

### GET /api/v1/deliveries/{delivery_id}

Get a single delivery by ID. Verifies ownership through the parent endpoint.

**Rate limit**: 30/min

**Response** (200): Single `DeliveryResponse` object (includes `execution_state` and `safe_to_execute` fields).

### GET /api/v1/endpoints/{endpoint_id}/deliveries

List deliveries for a specific endpoint.

**Rate limit**: 30/min

**Query parameters**: `status`, `cursor`, `limit`.

**Response** (200): Same `DeliveryListResponse` format (includes `execution_state` and `safe_to_execute` on each delivery).

### Execution State Fields on Deliveries

All delivery responses (`GET /api/v1/deliveries`, `GET /api/v1/deliveries/{delivery_id}`, `GET /api/v1/endpoints/{endpoint_id}/deliveries`) include two additional fields on every delivery object:

| Field | Type | Description |
|---|---|---|
| `execution_state` | string | Execution state of the associated event at the time of delivery: `"provisional"`, `"confirmed"`, `"finalized"`, or `"reorged"`. |
| `safe_to_execute` | boolean | Whether the associated event was safe to act on at delivery time. `true` only when `execution_state` is `"finalized"`. |

### GET /api/v1/endpoints/{endpoint_id}/deliveries/stats

Get delivery statistics for an endpoint.

**Rate limit**: 30/min

**Response** (200):

```json
{
  "endpoint_id": "abc123def456789012345",
  "total": 150,
  "pending": 2,
  "sent": 5,
  "delivered": 140,
  "failed": 3,
  "success_rate": 0.933
}
```

### POST /api/v1/deliveries/{delivery_id}/retry

Retry a failed delivery through Convoy. Only deliveries with `status: "failed"` can be retried. The endpoint must have a `convoy_project_id` configured.

**Rate limit**: 30/min

**Response** (202 Accepted):

```json
{
  "detail": "Retry requested",
  "delivery_id": "del_abc123"
}
```

**Errors**:
- 400 if the delivery is not in `failed` status.
- 400 if the endpoint has no Convoy project configured.
- 400 if the delivery has no `provider_message_id`.
- 502 if the Convoy retry request fails.

---

## 8. Ingest API

Internal endpoints for receiving blockchain events. Authenticated via `Authorization: Bearer <secret>` (not SIWE). These endpoints are not meant for direct developer use — they are called by the Goldsky Turbo indexing pipeline and the x402 facilitator respectively.

TripWire sits downstream of Goldsky Turbo for all onchain event data. Goldsky indexes the target chains, applies SQL transforms (using `_gs_log_decode` to JOIN `AuthorizationUsed` and `Transfer` log tables), and delivers pre-decoded event batches to TripWire's ingest endpoint via its webhook sink. TripWire does not consume raw RPC data for this path — it receives already-decoded fields from Goldsky.

### POST /api/v1/ingest/goldsky

Receive a batch of ERC-3009 events pre-decoded by Goldsky Turbo's webhook sink.

**Data plane context**: This endpoint is the target of a Goldsky Turbo webhook sink. Goldsky runs SQL transforms before delivery — specifically a `_gs_log_decode` query that JOINs `AuthorizationUsed` and `Transfer` log tables and surfaces decoded field values. The payload format reflects Goldsky's transform output, not raw `eth_getLogs` data. TripWire never sees undecoded log topics or data on this path.

**Auth**: `Authorization: Bearer <GOLDSKY_WEBHOOK_SECRET>` — set `GOLDSKY_WEBHOOK_SECRET` in TripWire's environment and configure the same value as the webhook secret in your Goldsky pipeline.

**Rate limit**: 100/min

**Request body**: Array of decoded log objects produced by Goldsky's SQL transform (or a single object). The `decoded` field contains the event parameters extracted by Goldsky — TripWire does not perform ABI decoding on this path.

```json
[
  {
    "transaction_hash": "0x...",
    "block_number": 12345678,
    "block_hash": "0x...",
    "log_index": 0,
    "block_timestamp": 1710590400,
    "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "chain_id": 8453,
    "decoded": {
      "authorizer": "0x...",
      "nonce": "0x..."
    }
  }
]
```

**Batch limit**: Maximum 1000 logs per request. Returns 400 if exceeded.

**Response** (200):

```json
{
  "processed": 1,
  "results": [
    {
      "status": "processed",
      "tx_hash": "0x...",
      "event_id": "evt_abc123"
    }
  ]
}
```

When the event bus is enabled, events are published to Redis Streams and the response reflects queued status:

```json
{
  "processed": 1,
  "results": [
    {
      "status": "queued",
      "stream_id": "1710590400000-0"
    }
  ]
}
```

### POST /api/v1/ingest/event

Process a single raw event. Same auth and format as `/ingest/goldsky` but for a single object.

**Auth**: `Authorization: Bearer <GOLDSKY_WEBHOOK_SECRET>`

**Rate limit**: 100/min

**Request body**: Single decoded log object.

**Response** (200):

```json
{
  "status": "processed",
  "tx_hash": "0x...",
  "event_id": "evt_abc123"
}
```

### POST /api/v1/ingest/facilitator

Receive a pre-settlement ERC-3009 authorization from the x402 facilitator. This path is entirely separate from the Goldsky pipeline — it does not go through Goldsky Turbo and does not use `GOLDSKY_WEBHOOK_SECRET`. The facilitator calls this endpoint directly, authenticated with `FACILITATOR_WEBHOOK_SECRET`. This is the fast path (~100ms) -- the facilitator has already verified the ERC-3009 signature. TripWire skips decode and finality, running only nonce dedup, identity resolution, policy evaluation, and dispatch.

**Unified lifecycle**: The facilitator path creates a `payment.pre_confirmed` event. When the same transfer later arrives via the Goldsky pipeline (matching on nonce + authorizer), TripWire promotes the existing event rather than creating a new one. Both paths share the same `event_id`, producing a single event that progresses through `pre_confirmed` -> `confirmed` -> `finalized`.

**Auth**: `Authorization: Bearer <FACILITATOR_WEBHOOK_SECRET>`

**Rate limit**: 100/min

**Request body**:

```json
{
  "from_address": "0x1234567890abcdef1234567890abcdef12345678",
  "to_address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
  "amount": "1000000",
  "nonce": "0x0000000000000000000000000000000000000000000000000000000000000001",
  "chain_id": 8453,
  "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
  "valid_after": 0,
  "valid_before": 9999999999,
  "signature_verified": true
}
```

| Field | Type | Required | Description |
|---|---|---|---|
| `from_address` | EthAddress | Yes | Sender address. |
| `to_address` | EthAddress | Yes | Recipient address. |
| `amount` | string | Yes | Transfer amount (USDC 6 decimals). |
| `nonce` | string | Yes | ERC-3009 bytes32 nonce. |
| `chain_id` | int | Yes | Must be a supported chain (1, 8453, 42161). |
| `token` | EthAddress | Yes | Must be a known USDC contract address. |
| `valid_after` | int | Yes | Unix timestamp after which auth is valid. |
| `valid_before` | int | Yes | Unix timestamp before which auth is valid. |
| `signature_verified` | bool | Yes | Must be `true` (422 if false). |

**Response** (200):

```json
{
  "status": "processed",
  "event_id": "evt_abc123",
  "tx_hash": "0x000000000000000000000000a1b2c3d4e5f6..."
}
```

---

## 9. Auth API

### Session Endpoints (Keeper Only)

Session endpoints provide a pre-authorized spending limit for MCP tool calls, eliminating per-call x402 payment negotiation. Sessions are Keeper-only and require `SESSION_ENABLED=true`.

All session endpoints require SIWE authentication and are mounted at `/api/v1/auth/session` (under the `/api/v1` prefix).

#### POST /api/v1/auth/session

Open a new Keeper session with a server-side spending limit.

**Auth**: SIWE (wallet authentication)

**Rate limit**: 30/min

**Request body**:

```json
{
  "budget": 10000000,
  "ttl_seconds": 900,
  "chain_id": 8453
}
```

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `budget` | int or null | No | Server default (10 USDC) | Budget in smallest USDC units (6 decimals). Clamped to server max (100 USDC). |
| `ttl_seconds` | int or null | No | Server default (900s) | Session lifetime in seconds. Clamped to server max (1800s). |
| `chain_id` | int or null | No | 8453 | Chain ID context for identity resolution. |

**Response** (200):

```json
{
  "session_id": "aB3dEf7GhIjK9LmNoPqRsTuVw",
  "wallet_address": "0xYourWalletAddress",
  "budget_total": 10000000,
  "budget_remaining": 10000000,
  "expires_at": "2026-03-18T12:15:00+00:00",
  "ttl_seconds": 900,
  "chain_id": 8453,
  "status": "active"
}
```

**Error**: 501 if `SESSION_ENABLED=false` or session system not initialized.

#### GET /api/v1/auth/session/{session_id}

Retrieve the current state of a session. Only the wallet that created the session can query it.

**Auth**: SIWE (wallet authentication)

**Rate limit**: 30/min

**Response** (200):

```json
{
  "session_id": "aB3dEf7GhIjK9LmNoPqRsTuVw",
  "wallet_address": "0xYourWalletAddress",
  "budget_total": 10000000,
  "budget_remaining": 7000000,
  "expires_at": "2026-03-18T12:15:00+00:00",
  "ttl_seconds": 900,
  "chain_id": 8453,
  "status": "active"
}
```

The `status` field is `"active"` or `"expired"` depending on whether `expires_at` has passed.

**Errors**: 403 if not the session owner. 404 if session not found.

#### DELETE /api/v1/auth/session/{session_id}

Close a session and return its final state. Only the wallet that created the session can close it.

**Auth**: SIWE (wallet authentication)

**Rate limit**: 30/min

**Response** (200):

```json
{
  "session_id": "aB3dEf7GhIjK9LmNoPqRsTuVw",
  "wallet_address": "0xYourWalletAddress",
  "budget_total": 10000000,
  "budget_remaining": 7000000,
  "expires_at": "2026-03-18T12:15:00+00:00",
  "ttl_seconds": 900,
  "chain_id": 8453,
  "status": "closed"
}
```

**Errors**: 403 if not the session owner. 404 if session not found or already closed.

---

### GET /auth/nonce

Generate a cryptographically random nonce for SIWE authentication. The nonce is stored in Redis with a 5-minute TTL and can only be used once.

**Auth**: None required.

**Rate limit**: 30/min per caller, 1000/min global.

> Note: This endpoint is mounted at the root (`/auth/nonce`), not under `/api/v1`.

**Response** (200):

```json
{
  "nonce": "aB3dEf7GhIjK9LmNoPqRsTuVwXyZ012345678901"
}
```

---

## 10. Stats API

### GET /api/v1/stats

Return processing statistics scoped to the authenticated wallet's endpoints.

**Rate limit**: 30/min

**Response** (200):

```json
{
  "total_events": 1542,
  "events_last_hour": 23,
  "active_endpoints": 3,
  "last_event_at": "2026-03-16T11:58:32Z",
  "execution_state_breakdown": {
    "finalized": 1420,
    "confirmed": 98,
    "provisional": 21,
    "reorged": 3
  }
}
```

| Field | Type | Description |
|---|---|---|
| `total_events` | int | Total events across all wallet endpoints. |
| `events_last_hour` | int | Events received in the last 60 minutes. |
| `active_endpoints` | int | Number of active endpoints. |
| `last_event_at` | string (ISO-8601) | Timestamp of the most recent event. |
| `execution_state_breakdown` | object | Count of events grouped by execution state (`finalized`, `confirmed`, `provisional`, `reorged`). States with zero events may be omitted. |

### GET /api/v1/stats/agent-metrics

Return aggregated per-agent metrics from a materialized view. Useful for monitoring agent activity across all of the wallet's endpoints.

**Rate limit**: 30/min

**Response** (200):

```json
{
  "data": [
    {
      "agent_address": "0x1234567890abcdef1234567890abcdef12345678",
      "total_events": 320,
      "finalized_events": 305,
      "successful_deliveries": 298,
      "active_triggers": 4
    },
    {
      "agent_address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
      "total_events": 87,
      "finalized_events": 85,
      "successful_deliveries": 82,
      "active_triggers": 1
    }
  ]
}
```

| Field | Type | Description |
|---|---|---|
| `agent_address` | string | The agent's Ethereum address. |
| `total_events` | int | Total events originated by this agent. |
| `finalized_events` | int | Events that reached `finalized` execution state. |
| `successful_deliveries` | int | Deliveries with `delivered` status for this agent's events. |
| `active_triggers` | int | Number of active triggers associated with this agent. |

---

## 11. Well-Known

### GET /.well-known/x402-manifest.json

x402 Bazaar service discovery manifest. Returns metadata about TripWire's MCP tools, supported chains, auth configuration, and x402-gated services with pricing.

**Auth**: None required.

> Note: This endpoint is mounted at the root, not under `/api/v1`.

**Response** (200):

```json
{
  "@context": "https://x402.org/context",
  "name": "TripWire",
  "description": "Programmable onchain event triggers for AI agents...",
  "version": "1.0.0",
  "identity": {
    "protocol": "ERC-8004",
    "registry": "0x..."
  },
  "auth": {
    "siwe": {
      "nonce_endpoint": "/auth/nonce",
      "domain": "tripwire.dev"
    },
    "x402": {
      "facilitator": "https://facilitator.x402.org",
      "network": "eip155:8453",
      "pay_to": "0x..."
    }
  },
  "mcp": {
    "endpoint": "/mcp",
    "transport": "json-rpc",
    "tools": [
      { "name": "create_trigger", "auth_tier": "X402" },
      { "name": "list_templates", "auth_tier": "SIWX" }
    ]
  },
  "services": [
    {
      "name": "create_trigger",
      "description": "...",
      "endpoint": "/mcp",
      "method": "POST",
      "scheme": "exact",
      "price": "1.00",
      "network": "eip155:8453",
      "pay_to": "0x..."
    }
  ],
  "supported_chains": [
    { "chain_id": 8453, "name": "Base" },
    { "chain_id": 1, "name": "Ethereum" },
    { "chain_id": 42161, "name": "Arbitrum" }
  ],
  "skill_spec": {
    "version": "1.0.0-draft",
    "url": "https://<host>:3402/.well-known/tripwire-skill-spec.json"
  }
}
```

### GET /discovery/resources

x402 V2 Bazaar discovery endpoint. Returns the same service discovery information as `GET /.well-known/x402-manifest.json` but follows the x402 V2 resource discovery convention. V2 clients should prefer this endpoint.

**Auth**: None required.

> Note: This endpoint is mounted at the root, not under `/api/v1`.

**Response** (200): Same structure as `GET /.well-known/x402-manifest.json` (see above).

**Product mode filtering**: When `PRODUCT_MODE` is set to `pulse` or `keeper` (instead of the default `both`), the discovery endpoints filter their responses to only advertise capabilities relevant to the active product. For example, a `pulse`-only deployment omits Keeper payment-specific services and vice versa.

### GET /.well-known/tripwire-skill-spec.json

[TWSS-1 Skill Spec](SKILL-SPEC.md) machine-readable schema. Returns the execution-aware skill output contract, three-layer gating model, two-phase execution model (prepare/commit), chain finality reference, and determinism guarantees.

**Auth**: None required.

---

## 12. Health Endpoints

These operational endpoints are at the root level (no `/api/v1` prefix) and require no authentication.

### GET /health

Basic liveness probe.

**Response** (200):

```json
{
  "status": "ok",
  "service": "tripwire",
  "version": "0.1.0"
}
```

### GET /health/detailed

Deep health check. Probes Supabase, webhook provider (Convoy), Redis, identity resolver, background tasks (finality poller, nonce archiver, DLQ handler), and the event bus worker pool.

**Response** (200 if all healthy, 503 if any unhealthy):

```json
{
  "status": "healthy",
  "version": "0.1.0",
  "uptime_seconds": 3612.4,
  "components": {
    "supabase": { "status": "healthy" },
    "webhook_provider": { "status": "healthy", "type": "convoy" },
    "redis": { "status": "healthy" },
    "identity_resolver": { "status": "healthy", "type": "LiveResolver" },
    "background_tasks": {
      "finality_poller": {
        "status": "healthy",
        "running": true,
        "seconds_since_last_run": 12.3,
        "error_count": 0,
        "last_error": null
      }
    },
    "worker_pool": {
      "status": "healthy",
      "workers": [{ "running": true }]
    }
  }
}
```

### GET /ready

Readiness probe. Returns 200 only after the lifespan startup completes.

**Response** (200):

```json
{ "ready": true }
```

**Response** (503 -- not ready):

```json
{ "ready": false }
```

### GET /metrics

Prometheus metrics endpoint. If `METRICS_BEARER_TOKEN` is configured, requires `Authorization: Bearer <token>`. Returns Prometheus text exposition format.

---

## 13. Error Responses

### HTTP Status Codes

| Code | Meaning |
|---|---|
| 400 | Bad request (validation error, empty update, batch too large) |
| 401 | Missing or invalid authentication (SIWE headers or Bearer token) |
| 403 | Authenticated but not authorized (endpoint ownership check failed) |
| 404 | Resource not found |
| 409 | Conflict (unique constraint violation, e.g. duplicate endpoint) |
| 422 | Unprocessable entity (foreign key or check constraint violation, invalid facilitator payload) |
| 429 | Rate limit exceeded (includes `Retry-After` header) |
| 500 | Internal server error |
| 502 | Upstream service error (Convoy retry failure, PostgREST error) |
| 503 | Service temporarily unavailable (network timeout, connectivity issue) |

### Error Response Format

All errors return JSON with a `detail` field:

```json
{
  "detail": "Endpoint not found"
}
```

Database errors include an `error_code` field:

```json
{
  "detail": "Conflict: unique constraint violation",
  "error_code": "23505"
}
```

### PostgREST Error Mapping

| PostgreSQL Code | HTTP Status | Description |
|---|---|---|
| `23505` | 409 | Unique constraint violation |
| `23503` | 422 | Foreign key violation |
| `23514` | 422 | Check constraint violation |
| `42501` | 403 | Insufficient privilege |
| `PGRST*` | 502 | PostgREST internal error |

### Validation Errors

Pydantic validation errors return 422 with details about which fields failed:

```json
{
  "detail": [
    {
      "loc": ["body", "chains"],
      "msg": "List should have at least 1 item after validation, not 0",
      "type": "too_short"
    }
  ]
}
```

---

## 14. Data Models

### Endpoint

```
id                    string       Nanoid (21 chars)
url                   string       Webhook delivery URL (HTTPS required in production)
mode                  "notify" | "execute"
chains                int[]        Chain IDs to monitor
recipient             EthAddress   Onchain recipient address (0x... 40 hex chars)
owner_address         EthAddress   Wallet that owns this endpoint
registration_tx_hash  string?      x402 payment transaction hash (if x402-gated)
registration_chain_id int?         Chain ID of the registration payment
policies              EndpointPolicies
active                bool         Soft-delete flag
convoy_project_id     string?      Convoy project ID (execute mode)
convoy_endpoint_id    string?      Convoy endpoint ID (execute mode)
webhook_secret        string?      HMAC secret (returned once at creation, never stored)
created_at            datetime
updated_at            datetime
```

### Subscription

```
id            string               Nanoid (21 chars)
endpoint_id   string               Parent endpoint ID
filters       SubscriptionFilter   Chain, sender, recipient, amount, agent_class filters
active        bool
created_at    datetime
```

### Event

```
id               string
endpoint_id      string?
type             WebhookEventType     payment.confirmed | payment.pending | payment.pre_confirmed | payment.finalized | payment.failed | payment.reorged
data             object               Transfer details, finality info, identity
execution_state  string               "provisional" | "confirmed" | "finalized" | "reorged"
safe_to_execute  bool                 true only when execution_state is "finalized"
trust_source     string               "facilitator" | "onchain"
created_at       string (ISO-8601)
```

### WebhookPayload

The payload delivered to your webhook endpoint:

```
id                         string              Unique event ID
idempotency_key            string              For deduplication on your side
type                       WebhookEventType
mode                       "notify" | "execute"
timestamp                  int                 Unix timestamp
version                    string              Payload schema version (currently "v1")
execution                  ExecutionBlock      Nested execution metadata (see below)
execution.state            ExecutionState      "provisional" | "confirmed" | "finalized" | "reorged"
execution.safe_to_execute  bool                Whether the event is safe to act on (true only when finalized)
execution.trust_source     TrustSource         "facilitator" | "onchain" — origin of the trust assertion
execution.finality         FinalityData?       confirmations, required_confirmations, is_finalized. null for pre_confirmed events.
data.transfer              TransferData        chain_id, tx_hash, block_number, from/to_address, amount, nonce, token
data.identity              AgentIdentity | null   ERC-8004 identity of the sender. `null` when the sender has no registered onchain identity. See `AgentIdentity` model below.
```

### AgentIdentity

Resolved from the [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) onchain agent identity registry, which went mainnet on January 29, 2026. Present on every webhook payload where the event sender has a registered onchain identity; `null` otherwise.

| Field | Type | Description |
|-------|------|-------------|
| `address` | string | Agent's wallet address |
| `agent_class` | string | ERC-8004 classification (e.g., `"trading-bot"`, `"payment-bot"`) |
| `deployer` | string | Contract deployer/owner address |
| `capabilities` | string[] | Onchain-declared capability strings |
| `reputation_score` | float | 0–100 (normalized from the registry's 0–10000 basis-point value) |
| `registered_at` | int | Agent token ID — a proxy for registration order (lower = registered earlier) |
| `metadata` | object | Raw fields: `agent_id` (registry token ID) and `tokenURI` (metadata URI) |

### Trigger

```
id                    string
owner_address         string        Wallet that created this trigger
endpoint_id           string        Target endpoint for matched events
name                  string?       Human-readable name
event_signature       string        Solidity event signature (e.g. "Transfer(address,address,uint256)")
topic0                string?       Precomputed keccak256 of event_signature
abi                   object[]      ABI fragment for decoding
contract_address      string?       Filter to a specific contract
chain_ids             int[]         Chain IDs to monitor
filter_rules          TriggerFilter[]   Field-level filters (field, op, value)
webhook_event_type    string        Event type string for matched events
reputation_threshold  float         Minimum sender reputation (0.0-100.0)
required_agent_class  string?       Required ERC-8004 agent class for sender (null = any)
version               string        Trigger definition version (default: "1.0.0")
batch_id              string?       Batch installation tracking
active                bool
created_at            datetime?
updated_at            datetime?
```

**Reputation enforcement**: When `reputation_threshold` is greater than 0, TripWire evaluates the sender's ERC-8004 reputation score at event processing time. Events from agents whose reputation falls below the threshold are rejected. Agents with no onchain identity are also rejected.

### TriggerTemplate

```
id                    string
name                  string
slug                  string        URL-safe identifier
version               string        Template version (default: "1.0.0")
description           string?
category              string        Template category (default: "general")
event_signature       string
topic0                string?
abi                   object[]
default_chains        int[]
default_filters       TriggerFilter[]
parameter_schema      object[]      Schema for user-configurable parameters
webhook_event_type    string
reputation_threshold  float
author_address        string?
is_public             bool
install_count         int
created_at            datetime?
updated_at            datetime?
```

---

## 15. MCP Reputation Requirements

Paid MCP tools require a minimum agent reputation score to invoke. This prevents low-reputation or unregistered agents from accessing premium functionality.

| MCP Tool | Auth Tier | Minimum Reputation |
|---|---|---|
| `register_middleware` | X402 | 10.0 |
| `create_trigger` | X402 | 10.0 |
| `activate_template` | X402 | 10.0 |

Agents with a reputation score below 10.0 (or with no registered ERC-8004 onchain identity) will receive a rejection error when calling these tools. Reputation is resolved from the ERC-8004 registry at invocation time.

Public and SIWX-tier MCP tools (e.g., `list_templates`, `list_triggers`) do not enforce a reputation threshold.
