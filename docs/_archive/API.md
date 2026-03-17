# TripWire API Reference

TripWire is an x402 execution middleware that monitors ERC-3009 USDC transfers on-chain and delivers structured webhook payloads to registered endpoints. This document is the authoritative reference for the TripWire HTTP API.

The **MCP API** is the primary interface for AI agents. The REST API documented in later sections is used for human-facing dashboards and server-to-server integrations.

---

## Table of Contents

1. [MCP API (AI Agent Interface)](#mcp-api-ai-agent-interface)
2. [Base URL](#base-url)
3. [Authentication](#authentication)
4. [Error Responses](#error-responses)
5. [Pagination](#pagination)
6. [Rate Limiting](#rate-limiting)
7. [Data Types and Enumerations](#data-types-and-enumerations)
8. [Auth](#auth)
9. [Endpoints](#endpoints)
10. [Subscriptions](#subscriptions)
11. [Events](#events)
12. [Deliveries](#deliveries)
13. [Stats](#stats)
14. [Ingest](#ingest)
15. [Facilitator](#facilitator)
16. [Health](#health)
17. [Webhook Payload Reference](#webhook-payload-reference)

---

## MCP API (AI Agent Interface)

The Model Context Protocol (MCP) server is the primary way AI agents interact with TripWire. It exposes 8 tools over a JSON-RPC 2.0 transport that let agents register middleware, create and manage triggers, browse the Bazaar template catalog, and query events -- all in a single protocol that LLM tool-calling understands natively.

---

### Connection

**Endpoint:** `POST /mcp/`

**Transport:** Streamable HTTP (JSON-RPC 2.0 over HTTP POST)

Every request is a single JSON-RPC 2.0 envelope:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "register_middleware",
    "arguments": { "url": "https://my-agent.example.com/webhook" }
  }
}
```

**Supported methods:**

| Method | Description |
|---|---|
| `initialize` | Handshake; returns protocol version (`2024-11-05`), server info, and capabilities |
| `tools/list` | Returns the schema for all 8 tools |
| `tools/call` | Executes a tool (requires authentication) |

All responses use HTTP 200 with JSON-RPC success or error payloads -- the HTTP status code is always 200; check the `result` or `error` field in the JSON body.

---

### Authentication

All `tools/call` invocations require an `Authorization` header:

```
Authorization: Bearer <ethereum_address>
```

The value is the agent's Ethereum address in hex (`0x` + 40 hex characters). The server normalizes it to lowercase.

MCP authentication uses a **3-tier model**:

| Tier | Mechanism | Tools |
|---|---|---|
| **PUBLIC** | No auth required | `initialize`, `tools/list` |
| **SIWX** | Wallet signature via SIWE (free, identity-gated) | `list_triggers`, `list_templates`, `get_trigger_status`, `search_events`, `delete_trigger` |
| **X402** | Per-call x402 micropayment required | `register_middleware`, `create_trigger`, `activate_template` |

For **SIWX** tier tools, the `Authorization: Bearer <ethereum_address>` header identifies the caller. The server normalizes the address to lowercase.

For **X402** tier tools, a valid x402 payment proof must accompany the request. The server verifies the ERC-3009 payment before executing the tool.

**ERC-8004 Identity Resolution:** When a tool has a non-zero `min_reputation` threshold, the server resolves the agent's ERC-8004 identity on Base (chain 8453) and checks `reputation_score` against the threshold. If the agent is unregistered or below threshold, the call is rejected with error code `-32001`.

**Reputation gating summary:**

| Minimum Reputation | Tools |
|---|---|
| 0.0 (any agent) | `list_triggers`, `list_templates`, `get_trigger_status`, `search_events` |
| 0.3 | `register_middleware`, `create_trigger`, `activate_template` |
| 0.5 | `delete_trigger` |

---

### Tools

#### 1. register_middleware

Register TripWire as middleware for your API. Creates an endpoint and optionally instantiates triggers from Bazaar template slugs or custom definitions.

**Minimum reputation:** 0.3

**Input schema:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `url` | `string` | Yes | -- | Your webhook/callback URL |
| `mode` | `string` | No | `"execute"` | Delivery mode: `"notify"` (Supabase Realtime) or `"execute"` (webhook POST) |
| `chains` | `integer[]` | No | `[8453]` | Chain IDs to monitor |
| `recipient` | `string` | No | Agent address | Recipient address to watch |
| `policies` | `object` | No | `{}` | Endpoint policies (`min_amount`, `max_amount`, `allowed_senders`, `blocked_senders`, `required_agent_class`, `min_reputation_score`, `finality_depth`) |
| `template_slugs` | `string[]` | No | `[]` | Bazaar template slugs to instantiate as triggers |
| `custom_triggers` | `object[]` | No | `[]` | Custom trigger definitions (see below) |

**custom_triggers item schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `event_signature` | `string` | Yes | Solidity event signature (e.g. `Transfer(address,address,uint256)`) |
| `name` | `string` | No | Human-readable trigger name |
| `abi` | `array` | No | ABI fragment for the event |
| `contract_address` | `string` | No | Contract to watch (null for any) |
| `chain_ids` | `integer[]` | No | Chain IDs to monitor (defaults to endpoint chains) |
| `filter_rules` | `array` | No | Filter predicates on decoded event fields |
| `webhook_event_type` | `string` | No | Event type string (default: `"payment.confirmed"`) |

**Output:**

```json
{
  "endpoint_id": "abc123xyz...",
  "webhook_secret": "hex-encoded-64-char-secret",
  "trigger_ids": ["trig1", "trig2"],
  "mode": "execute",
  "url": "https://my-agent.example.com/webhook"
}
```

The `webhook_secret` is returned exactly once. Store it immediately for HMAC verification of incoming webhooks.

---

#### 2. create_trigger

Create a custom trigger for an existing endpoint.

**Minimum reputation:** 0.3

**Input schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `endpoint_id` | `string` | Yes | Target endpoint ID |
| `event_signature` | `string` | Yes | Solidity event signature (e.g. `Transfer(address,address,uint256)`) |
| `name` | `string` | No | Human-readable trigger name |
| `abi` | `array` | No | ABI fragment for the event |
| `contract_address` | `string` | No | Contract to watch (null for any) |
| `chain_ids` | `integer[]` | No | Chain IDs to monitor (defaults to endpoint's chains) |
| `filter_rules` | `array` | No | Filter predicates on decoded event fields |
| `webhook_event_type` | `string` | No | Event type string |
| `reputation_threshold` | `number` | No | Min reputation for senders to pass this trigger |

**Output:**

```json
{
  "trigger_id": "trig_abc123",
  "endpoint_id": "ep_xyz789",
  "event_signature": "Transfer(address,address,uint256)",
  "active": true
}
```

---

#### 3. list_triggers

List all triggers owned by the calling agent.

**Minimum reputation:** 0.0

**Input schema:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `active_only` | `boolean` | No | `true` | Only return active triggers |

**Output:**

```json
{
  "triggers": [
    {
      "id": "trig_abc123",
      "name": "USDC Transfer Monitor",
      "endpoint_id": "ep_xyz789",
      "event_signature": "Transfer(address,address,uint256)",
      "chain_ids": [8453],
      "active": true,
      "created_at": "2026-03-01T00:00:00+00:00"
    }
  ],
  "count": 1
}
```

---

#### 4. delete_trigger

Deactivate a trigger (soft delete). The agent must own the trigger.

**Minimum reputation:** 0.5

**Input schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `trigger_id` | `string` | Yes | Trigger ID to deactivate |

**Output:**

```json
{
  "trigger_id": "trig_abc123",
  "active": false
}
```

---

#### 5. list_templates

Browse available trigger templates from the Bazaar catalog.

**Minimum reputation:** 0.0

**Input schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `category` | `string` | No | Filter by category (e.g. `"defi"`, `"payments"`, `"nft"`) |

**Output:**

```json
{
  "templates": [
    {
      "slug": "usdc-transfer",
      "name": "USDC Transfer",
      "description": "Monitor ERC-3009 USDC transferWithAuthorization events",
      "category": "payments",
      "event_signature": "AuthorizationUsed(address,bytes32)",
      "default_chains": [8453],
      "parameter_schema": {},
      "reputation_threshold": 0.0,
      "install_count": 42
    }
  ],
  "count": 1
}
```

---

#### 6. activate_template

Instantiate a Bazaar template with custom parameters for an existing endpoint.

**Minimum reputation:** 0.3

**Input schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `slug` | `string` | Yes | Template slug from the Bazaar |
| `endpoint_id` | `string` | Yes | Target endpoint ID (must be owned by caller) |
| `params` | `object` | No | Custom parameters: `chain_ids`, `contract_address`, `filter_rules` |

**Output:**

```json
{
  "trigger_id": "trig_tmpl_abc",
  "template_slug": "usdc-transfer",
  "endpoint_id": "ep_xyz789",
  "event_signature": "AuthorizationUsed(address,bytes32)",
  "active": true
}
```

---

#### 7. get_trigger_status

Check trigger health, configuration, and event count.

**Minimum reputation:** 0.0

**Input schema:**

| Field | Type | Required | Description |
|---|---|---|---|
| `trigger_id` | `string` | Yes | Trigger ID to check (must be owned by caller) |

**Output:**

```json
{
  "trigger_id": "trig_abc123",
  "name": "USDC Transfer Monitor",
  "event_signature": "Transfer(address,address,uint256)",
  "chain_ids": [8453],
  "active": true,
  "event_count": 157,
  "created_at": "2026-03-01T00:00:00+00:00"
}
```

`event_count` is `-1` if the count query fails.

---

#### 8. search_events

Query recent events across all of the agent's endpoints.

**Minimum reputation:** 0.0

**Input schema:**

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `limit` | `integer` | No | `50` | Max results (1--100) |
| `status` | `string` | No | -- | Filter by event status (e.g. `"confirmed"`, `"pending"`) |
| `chain_id` | `integer` | No | -- | Filter by chain ID |

**Output:**

```json
{
  "events": [
    {
      "id": "evt_abc123",
      "endpoint_id": "ep_xyz789",
      "tx_hash": "0x...",
      "chain_id": 8453,
      "status": "confirmed",
      "block_number": 12345678,
      "created_at": "2026-03-13T00:00:00Z"
    }
  ],
  "count": 1
}
```

---

### x402 Bazaar Manifest

```
GET /.well-known/x402-manifest.json
```

**Authentication:** None required.

Returns the x402 service manifest for Bazaar discovery. AI agents and x402 facilitators use this endpoint to discover TripWire's MCP tools and pricing.

**Response:**

```json
{
  "@context": "https://x402.org/context",
  "name": "TripWire",
  "description": "Programmable onchain event triggers for AI agents",
  "version": "1.0.0",
  "identity": {
    "protocol": "ERC-8004",
    "registry": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
  },
  "mcp": {
    "endpoint": "/mcp",
    "transport": "streamable-http",
    "tools": [
      "register_middleware", "create_trigger", "list_triggers",
      "delete_trigger", "list_templates", "activate_template",
      "get_trigger_status", "search_events"
    ]
  },
  "services": [
    { "name": "register_middleware", "price": "$0.003", "network": "eip155:8453" },
    { "name": "create_trigger", "price": "$0.003", "network": "eip155:8453" },
    { "name": "activate_template", "price": "$0.001", "network": "eip155:8453" }
  ],
  "supported_chains": [
    { "chain_id": 8453, "name": "Base" },
    { "chain_id": 1, "name": "Ethereum" },
    { "chain_id": 42161, "name": "Arbitrum" }
  ]
}
```

---

### MCP Error Codes

All errors are returned inside the JSON-RPC `error` object. The HTTP status is always `200`.

| Code | Constant | Description |
|---|---|---|
| `-32700` | Parse error | Malformed JSON body |
| `-32600` | Invalid request | Missing `jsonrpc: "2.0"` or `method` field |
| `-32601` | Method not found | Unknown JSON-RPC method or unknown tool name |
| `-32602` | Invalid params | Missing required tool parameters |
| `-32603` | Internal error | Unhandled exception during tool execution |
| `-32000` | Auth required | Missing or invalid `Authorization: Bearer <address>` header |
| `-32001` | Reputation too low | Agent's ERC-8004 reputation score is below the tool's `min_reputation` threshold |

**Application-level error codes** are returned in the tool result (inside `content[0].text`) when the operation fails logically rather than at the protocol level:

| `code` field | Meaning |
|---|---|
| `NOT_FOUND` | Trigger, endpoint, or template not found |
| `FORBIDDEN` | Agent does not own the requested resource |
| `INTERNAL` | Unexpected failure during tool execution |

When a tool returns an error, `isError` is `true` in the response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"error\": \"Trigger not found\", \"code\": \"NOT_FOUND\"}"
      }
    ],
    "isError": true
  }
}
```

---

### Example Flows

#### Complete: Register middleware with a Bazaar template

This flow registers TripWire as middleware, creates an endpoint in `execute` mode on Base, and activates the `usdc-transfer` template in a single call.

**1. Initialize the MCP session:**

```json
POST /mcp/
Authorization: Bearer 0xAgentAddress1234567890abcdef1234567890ab

{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "initialize",
  "params": {}
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2024-11-05",
    "capabilities": { "tools": { "listChanged": false } },
    "serverInfo": { "name": "tripwire-mcp", "version": "1.0.0" }
  }
}
```

**2. Register middleware with a template:**

```json
POST /mcp/
Authorization: Bearer 0xAgentAddress1234567890abcdef1234567890ab

{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "register_middleware",
    "arguments": {
      "url": "https://my-agent.example.com/webhook",
      "mode": "execute",
      "chains": [8453],
      "template_slugs": ["usdc-transfer"],
      "policies": {
        "min_amount": "1000000",
        "finality_depth": 5
      }
    }
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"endpoint_id\": \"ep_abc123\", \"webhook_secret\": \"a1b2c3...64hex\", \"trigger_ids\": [\"trig_xyz\"], \"mode\": \"execute\", \"url\": \"https://my-agent.example.com/webhook\"}"
      }
    ],
    "isError": false
  }
}
```

Store the `webhook_secret` -- it is not returned again. TripWire will POST signed webhooks to your URL when matching onchain events are detected.

---

#### Create a custom trigger for Uniswap V3 swaps

This flow creates a custom trigger that watches for Uniswap V3 Swap events on a specific pool contract.

```json
POST /mcp/
Authorization: Bearer 0xAgentAddress1234567890abcdef1234567890ab

{
  "jsonrpc": "2.0",
  "id": 3,
  "method": "tools/call",
  "params": {
    "name": "create_trigger",
    "arguments": {
      "endpoint_id": "ep_abc123",
      "event_signature": "Swap(address,address,int256,int256,uint160,uint128,int24)",
      "name": "USDC/ETH Pool Swaps",
      "contract_address": "0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640",
      "chain_ids": [1],
      "filter_rules": [
        { "field": "amount0", "op": "gt", "value": "1000000000" }
      ]
    }
  }
}
```

Response:

```json
{
  "jsonrpc": "2.0",
  "id": 3,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"trigger_id\": \"trig_swap1\", \"endpoint_id\": \"ep_abc123\", \"event_signature\": \"Swap(address,address,int256,int256,uint160,uint128,int24)\", \"active\": true}"
      }
    ],
    "isError": false
  }
}
```

---

## Base URL

All API routes (except `/auth`, `/health`, `/ready`) are mounted under:

```
https://<your-deployment>/api/v1
```

Auth and health routes are at the root:

```
https://<your-deployment>/auth/nonce
https://<your-deployment>/health
https://<your-deployment>/health/detailed
https://<your-deployment>/ready
```

---

## Authentication

Most API endpoints require SIWE (Sign-In with Ethereum, EIP-4361) wallet authentication. The authentication flow is stateless per-request and replay-resistant via server-issued nonces stored in Redis.

### Authentication Flow

1. **Obtain a nonce** — `GET /auth/nonce`. The nonce is valid for 5 minutes and can only be used once.
2. **Build the SIWE message** — Construct the EIP-4361 message locally using the nonce, your address, and the request details.
3. **Sign the message** — Sign the SIWE message with `personal_sign` (EIP-191) using your wallet private key.
4. **Attach headers** — Include the five authentication headers on every authenticated request.

### SIWE Message Statement

The statement field embedded in the SIWE message is constructed from the HTTP method, path, and a SHA-256 hash of the raw request body:

```
{METHOD} {PATH} {sha256(request_body_bytes)}
```

For requests with no body (e.g. GET requests), the hash is the SHA-256 of an empty byte string.

### Required Authentication Headers

All endpoints marked as **Authenticated** require these five headers:

| Header | Description |
|---|---|
| `X-TripWire-Address` | Caller's Ethereum wallet address in checksummed or lowercase hex (`0x...`) |
| `X-TripWire-Signature` | EIP-191 `personal_sign` hex signature (`0x...`) over the SIWE message |
| `X-TripWire-Nonce` | Nonce obtained from `GET /auth/nonce` |
| `X-TripWire-Issued-At` | ISO-8601 timestamp when the SIWE message was constructed |
| `X-TripWire-Expiration` | ISO-8601 expiration timestamp; the server rejects requests past this time |

### Verification Steps (Server-Side)

1. Reads the request body and computes its SHA-256 hash.
2. Reconstructs the SIWE message using `{METHOD} {PATH} {body_hash}` as the statement.
3. Recovers the signer address from the EIP-191 signature.
4. Compares the recovered address to `X-TripWire-Address` (case-insensitive).
5. Atomically deletes the nonce from Redis; rejects if absent or already consumed.
6. Validates that the current time is before the expiration timestamp.

### Ingest Endpoint Authentication

The `/api/v1/ingest/*` endpoints do not use SIWE. They accept a server-to-server `Authorization: Bearer <secret>` header validated against the respective webhook secret configured in the server environment (`GOLDSKY_WEBHOOK_SECRET` for `/ingest/goldsky` and `/ingest/event`, `FACILITATOR_WEBHOOK_SECRET` for `/ingest/facilitator`).

---

## Error Responses

All errors return a JSON body with a `detail` field. Some database errors also include an `error_code` field.

```json
{
  "detail": "Human-readable description of the error",
  "error_code": "23505"
}
```

### Standard HTTP Status Codes

| Code | Meaning |
|---|---|
| `400` | Bad request — invalid input, missing required fields, or no fields to update |
| `401` | Unauthorized — missing or invalid authentication headers, expired or consumed nonce, bad signature |
| `403` | Forbidden — authenticated but not authorized to access the requested resource |
| `404` | Not found — the requested resource does not exist |
| `409` | Conflict — unique constraint violation (e.g. duplicate endpoint) |
| `422` | Unprocessable — validation error on request body fields, or foreign/check constraint violation |
| `429` | Too many requests — rate limit exceeded (see `Retry-After` header) |
| `502` | Bad gateway — upstream provider error (e.g. Convoy unreachable) |
| `503` | Service unavailable — network connectivity or timeout error to a dependent service |
| `500` | Internal server error — unexpected condition |

---

## Pagination

List endpoints that can return large result sets use **cursor-based (keyset) pagination** ordered by `created_at` descending.

### Query Parameters

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cursor` | `string` | `null` | ID of the last item from the previous page |
| `limit` | `integer` | `50` | Number of items to return. Minimum: `1`, maximum: `200` |

### Response Shape

```json
{
  "data": [...],
  "cursor": "<id-of-last-item-or-null>",
  "has_more": true
}
```

When `has_more` is `true`, pass the returned `cursor` value as the `cursor` query parameter to fetch the next page. When `cursor` is `null` and `has_more` is `false`, you have reached the last page.

### Cursor Mechanics

The cursor is the `id` of the last record in the returned page. The server uses the corresponding `created_at` timestamp for keyset comparison (`created_at < cursor_row.created_at`), so pagination is stable even if new records are inserted during traversal.

---

## Rate Limiting

Rate limits are enforced per-caller using [slowapi](https://github.com/laurentS/slowapi). The rate limit key is derived from the authenticated wallet address (`X-TripWire-Address` header) when present, falling back to the client IP address.

| Endpoint group | Limit |
|---|---|
| CRUD endpoints (`/endpoints`, `/subscriptions`, `/events`, `/deliveries`, `/stats`) | 30 requests per minute |
| Ingest endpoints (`/ingest/*`) | 100 requests per minute |
| Auth nonce (`/auth/nonce`) | 30 requests per minute |

When the limit is exceeded the server responds with `429 Too Many Requests` and sets the `Retry-After` header to the number of seconds until the window resets (defaulting to 60).

```json
{
  "detail": "Rate limit exceeded: ..."
}
```

---

## Data Types and Enumerations

### ChainId

Supported EVM chains. Values are integer chain IDs.

| Name | Value |
|---|---|
| Ethereum Mainnet | `1` |
| Base | `8453` |
| Arbitrum One | `42161` |

### EndpointMode

| Value | Description |
|---|---|
| `"notify"` | Delivers webhook notifications. Supports subscriptions with fine-grained filters. |
| `"execute"` | Wires a Convoy application for guaranteed webhook delivery with retries. A `webhook_secret` is returned once at creation and never stored. |

### WebhookEventType

| Value | Description |
|---|---|
| `"payment.confirmed"` | Transfer reached the required finality depth on-chain |
| `"payment.pending"` | Transfer detected on-chain but not yet finalized |
| `"payment.pre_confirmed"` | Transfer detected at the x402 facilitator layer before on-chain submission |
| `"payment.failed"` | Transfer processing failed |
| `"payment.reorged"` | A previously confirmed block containing the transfer was re-organized away |

### Delivery Status Values

| Value | Description |
|---|---|
| `"pending"` | Queued for delivery |
| `"sent"` | Delivered to the provider (Convoy) |
| `"delivered"` | Confirmed received by the destination URL |
| `"failed"` | All retry attempts exhausted |

### EndpointPolicies Object

Policies gate which transfers are dispatched to an endpoint. All fields are optional.

| Field | Type | Description |
|---|---|---|
| `min_amount` | `string \| null` | Minimum transfer amount in USDC atomic units (6 decimals) |
| `max_amount` | `string \| null` | Maximum transfer amount in USDC atomic units |
| `allowed_senders` | `string[] \| null` | Allowlist of `from_address` values (EIP-55 hex). If set, only transfers from these addresses are dispatched. |
| `blocked_senders` | `string[] \| null` | Blocklist of `from_address` values |
| `required_agent_class` | `string \| null` | ERC-8004 agent class string that the sender must match |
| `min_reputation_score` | `float \| null` | Minimum ERC-8004 reputation score (0–100) |
| `finality_depth` | `integer` | Number of confirmations required before dispatching. Default `3`, range `1`–`64`. |

### Endpoint Object

```json
{
  "id": "abc123",
  "url": "https://example.com/webhook",
  "mode": "execute",
  "chains": [8453, 42161],
  "recipient": "0xRecipientAddress",
  "owner_address": "0xOwnerAddress",
  "registration_tx_hash": "0x...",
  "registration_chain_id": 8453,
  "policies": {
    "min_amount": null,
    "max_amount": null,
    "allowed_senders": null,
    "blocked_senders": null,
    "required_agent_class": null,
    "min_reputation_score": null,
    "finality_depth": 3
  },
  "active": true,
  "convoy_project_id": "convoy-app-id",
  "convoy_endpoint_id": "convoy-endpoint-id",
  "created_at": "2024-01-01T00:00:00Z",
  "updated_at": "2024-01-01T00:00:00Z"
}
```

**Important:** For `execute`-mode endpoints, a `webhook_secret` is returned **exactly once** in the `POST /api/v1/endpoints` creation response. The secret is **not persisted in the database** and is never returned on any subsequent read. Store it securely immediately upon creation.

`registration_tx_hash` and `registration_chain_id` are populated only when an x402 payment was verified at registration time.

**Event-endpoint mapping:** Events can match multiple endpoints via the `event_endpoints` join table. When an event matches more than one registered endpoint, it is dispatched to all of them independently.

### Subscription Object

```json
{
  "id": "sub123",
  "endpoint_id": "abc123",
  "filters": {
    "chains": [8453],
    "senders": ["0x..."],
    "recipients": ["0x..."],
    "min_amount": "1000000",
    "agent_class": "PaymentAgent"
  },
  "active": true,
  "created_at": "2024-01-01T00:00:00Z"
}
```

### Event Object

```json
{
  "id": "evt123",
  "endpoint_id": "abc123",
  "type": "payment.confirmed",
  "data": { "..." : "..." },
  "created_at": "2024-01-01T00:00:00Z"
}
```

### Delivery Object

```json
{
  "id": "del123",
  "endpoint_id": "abc123",
  "event_id": "evt123",
  "provider_message_id": "convoy-msg-id",
  "status": "delivered",
  "created_at": "2024-01-01T00:00:00Z"
}
```

---

## Auth

### Get Nonce

Issue a single-use cryptographic nonce for SIWE message construction.

```
GET /auth/nonce
```

**Authentication:** None required.

**Rate limit:** 30 requests per minute.

**Response — 200 OK**

```json
{
  "nonce": "url-safe-random-string"
}
```

The nonce is stored in Redis with a 5-minute TTL. It is consumed atomically on first use and cannot be reused.

---

## Endpoints

Endpoints are the webhook destinations registered by wallet owners. Each endpoint is owned by the wallet that authenticated the creation request. All routes in this section require wallet authentication.

### Register Endpoint

```
POST /api/v1/endpoints
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

**x402 Payment Gate:** When the server is configured with a treasury address, this route requires an x402 `exact` scheme EVM payment before the request is processed. The payment transaction hash and chain ID are stored on the created endpoint in `registration_tx_hash` and `registration_chain_id`.

The caller's verified wallet address is automatically set as `owner_address` regardless of what is passed in the request body.

For `execute`-mode endpoints, the server synchronously creates a Convoy application and endpoint. The generated `webhook_secret` is included in the response exactly once and is not retrievable afterward.

**Request Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `url` | `string` | Yes | Destination HTTPS URL for webhook delivery |
| `mode` | `"notify" \| "execute"` | Yes | Endpoint operating mode |
| `chains` | `integer[]` | Yes | List of chain IDs to monitor. Minimum 1 element. |
| `recipient` | `string` | Yes | EIP-55 Ethereum address of the transfer recipient to monitor |
| `owner_address` | `string` | Yes | EIP-55 Ethereum address of the endpoint owner |
| `policies` | `EndpointPolicies` | No | Dispatch policy constraints. Defaults to no restrictions. |

**Response — 201 Created**

Returns the created `Endpoint` object.

**Status Codes**

| Code | Condition |
|---|---|
| `201` | Endpoint created successfully |
| `400` | Invalid URL or malformed request body |
| `401` | Authentication failure |
| `409` | An endpoint with the same identity already exists |

---

### List Endpoints

```
GET /api/v1/endpoints
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Returns all active endpoints owned by the authenticated wallet.

**Response — 200 OK**

```json
{
  "data": [ "<Endpoint>", "..." ],
  "count": 3
}
```

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success (may be an empty list) |
| `401` | Authentication failure |

---

### Get Endpoint

```
GET /api/v1/endpoints/{endpoint_id}
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | The endpoint ID |

**Response — 200 OK**

Returns the `Endpoint` object.

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `401` | Authentication failure |
| `403` | Endpoint exists but belongs to a different wallet |
| `404` | Endpoint not found |

---

### Update Endpoint

```
PATCH /api/v1/endpoints/{endpoint_id}
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Partially updates an endpoint. At least one field must be provided. Only fields explicitly included in the request body are updated.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | The endpoint ID |

**Request Body**

All fields are optional. Omitted fields are left unchanged.

| Field | Type | Description |
|---|---|---|
| `url` | `string \| null` | New destination URL |
| `mode` | `"notify" \| "execute" \| null` | New operating mode |
| `chains` | `integer[] \| null` | New list of monitored chain IDs |
| `policies` | `EndpointPolicies \| null` | Replacement policy object (replaces the entire policies object) |
| `active` | `boolean \| null` | Activate or deactivate the endpoint |

**Response — 200 OK**

Returns the updated `Endpoint` object.

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `400` | No fields provided in the request body |
| `401` | Authentication failure |
| `403` | Endpoint belongs to a different wallet |
| `404` | Endpoint not found |

---

### Deactivate Endpoint

```
DELETE /api/v1/endpoints/{endpoint_id}
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Soft-deletes the endpoint by setting `active = false`. The endpoint record and all associated events and deliveries are retained for audit and history purposes.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | The endpoint ID |

**Response — 204 No Content**

No response body.

**Status Codes**

| Code | Condition |
|---|---|
| `204` | Endpoint deactivated |
| `401` | Authentication failure |
| `403` | Endpoint belongs to a different wallet |
| `404` | Endpoint not found |

---

## Subscriptions

Subscriptions apply only to `notify`-mode endpoints and define filter criteria that must match before a transfer event is dispatched to that endpoint.

### Create Subscription

```
POST /api/v1/endpoints/{endpoint_id}/subscriptions
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | The parent endpoint ID. Must be active and in `notify` mode. |

**Request Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `filters` | `SubscriptionFilter` | Yes | Filter criteria for this subscription |

**SubscriptionFilter fields**

All filter fields are optional. Omitted fields impose no constraint.

| Field | Type | Description |
|---|---|---|
| `chains` | `integer[] \| null` | Only dispatch for transfers on these chain IDs |
| `senders` | `string[] \| null` | Only dispatch for transfers from these sender addresses |
| `recipients` | `string[] \| null` | Only dispatch for transfers to these recipient addresses |
| `min_amount` | `string \| null` | Minimum transfer amount in USDC atomic units |
| `agent_class` | `string \| null` | Required ERC-8004 agent class string on the sender |

**Response — 201 Created**

Returns the created `Subscription` object.

**Status Codes**

| Code | Condition |
|---|---|
| `201` | Subscription created |
| `400` | Endpoint is not in `notify` mode |
| `401` | Authentication failure |
| `403` | Endpoint belongs to a different wallet |
| `404` | Endpoint not found or not active |
| `409` | Subscription already exists |

---

### List Subscriptions

```
GET /api/v1/endpoints/{endpoint_id}/subscriptions
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Returns all active subscriptions for the specified endpoint.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | The parent endpoint ID |

**Response — 200 OK**

```json
[
  "<Subscription>",
  "..."
]
```

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success (may be an empty array) |
| `401` | Authentication failure |
| `403` | Endpoint belongs to a different wallet |
| `404` | Endpoint not found |

---

### Delete Subscription

```
DELETE /api/v1/subscriptions/{subscription_id}
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Deactivates a subscription by setting `active = false`. Ownership is verified through the parent endpoint.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `subscription_id` | `string` | The subscription ID |

**Response — 204 No Content**

No response body.

**Status Codes**

| Code | Condition |
|---|---|
| `204` | Subscription deactivated |
| `401` | Authentication failure |
| `403` | Parent endpoint belongs to a different wallet |
| `404` | Subscription not found, or parent endpoint not found |

---

## Events

Events represent detected ERC-3009 transfer occurrences. Each event is scoped to an endpoint and is only accessible by the wallet that owns that endpoint.

### List Events

```
GET /api/v1/events
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Returns events across all endpoints owned by the authenticated wallet, ordered by `created_at` descending. Supports cursor pagination and optional filters.

**Query Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cursor` | `string` | `null` | Pagination cursor (event ID from the previous page) |
| `limit` | `integer` | `50` | Items per page (1–200) |
| `event_type` | `WebhookEventType` | `null` | Filter to a specific event type |
| `chain_id` | `integer` | `null` | Filter to a specific chain ID |

**Response — 200 OK**

```json
{
  "data": [
    {
      "id": "evt123",
      "endpoint_id": "abc123",
      "type": "payment.confirmed",
      "data": { "..." : "..." },
      "created_at": "2024-01-01T00:00:00Z"
    }
  ],
  "cursor": "evt_last_id_or_null",
  "has_more": false
}
```

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success (empty if no endpoints exist or no events match) |
| `401` | Authentication failure |

---

### Get Event

```
GET /api/v1/events/{event_id}
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `event_id` | `string` | The event ID |

**Response — 200 OK**

```json
{
  "id": "evt123",
  "endpoint_id": "abc123",
  "type": "payment.confirmed",
  "data": { "..." : "..." },
  "created_at": "2024-01-01T00:00:00Z"
}
```

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `401` | Authentication failure |
| `403` | Event belongs to an endpoint owned by a different wallet, or the event has no associated endpoint |
| `404` | Event not found, or parent endpoint not found |

---

### List Events for Endpoint

```
GET /api/v1/endpoints/{endpoint_id}/events
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Returns events for a specific endpoint ordered by `created_at` descending.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | The endpoint ID |

**Query Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `cursor` | `string` | `null` | Pagination cursor (event ID) |
| `limit` | `integer` | `50` | Items per page (1–200) |

**Response — 200 OK**

```json
{
  "data": [ "<EventResponse>", "..." ],
  "cursor": "evt_last_id_or_null",
  "has_more": false
}
```

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `401` | Authentication failure |
| `403` | Endpoint belongs to a different wallet |
| `404` | Endpoint not found |

---

## Deliveries

Deliveries track individual webhook dispatch attempts, including their status and provider-level message IDs.

### List Deliveries

```
GET /api/v1/deliveries
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Returns deliveries across all endpoints owned by the authenticated wallet. Supports optional filters and cursor pagination.

**Query Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `endpoint_id` | `string` | `null` | Filter by a specific endpoint (ownership is verified) |
| `event_id` | `string` | `null` | Filter by a specific event |
| `status` | `string` | `null` | Filter by delivery status (`pending`, `sent`, `delivered`, `failed`) |
| `cursor` | `string` | `null` | Pagination cursor (delivery ID) |
| `limit` | `integer` | `50` | Items per page (1–200) |

**Response — 200 OK**

```json
{
  "data": [
    {
      "id": "del123",
      "endpoint_id": "abc123",
      "event_id": "evt123",
      "provider_message_id": "convoy-msg-id",
      "status": "delivered",
      "created_at": "2024-01-01T00:00:00Z"
    }
  ],
  "cursor": "del_last_id_or_null",
  "has_more": false
}
```

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `401` | Authentication failure |
| `403` | `endpoint_id` filter refers to an endpoint owned by a different wallet |
| `404` | `endpoint_id` filter refers to a nonexistent endpoint |

---

### Get Delivery

```
GET /api/v1/deliveries/{delivery_id}
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `delivery_id` | `string` | The delivery ID |

**Response — 200 OK**

Returns the delivery object.

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `401` | Authentication failure |
| `403` | Parent endpoint belongs to a different wallet |
| `404` | Delivery not found, or parent endpoint not found |

---

### List Deliveries for Endpoint

```
GET /api/v1/endpoints/{endpoint_id}/deliveries
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | The endpoint ID |

**Query Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `status` | `string` | `null` | Filter by delivery status |
| `cursor` | `string` | `null` | Pagination cursor (delivery ID) |
| `limit` | `integer` | `50` | Items per page (1–200) |

**Response — 200 OK**

```json
{
  "data": [ "<DeliveryResponse>", "..." ],
  "cursor": "del_last_id_or_null",
  "has_more": false
}
```

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `401` | Authentication failure |
| `403` | Endpoint belongs to a different wallet |
| `404` | Endpoint not found |

---

### Get Delivery Stats for Endpoint

```
GET /api/v1/endpoints/{endpoint_id}/deliveries/stats
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Returns aggregated delivery counts and the success rate for a specific endpoint.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | The endpoint ID |

**Response — 200 OK**

```json
{
  "endpoint_id": "abc123",
  "total": 120,
  "pending": 2,
  "sent": 5,
  "delivered": 110,
  "failed": 3,
  "success_rate": 0.9583
}
```

| Field | Type | Description |
|---|---|---|
| `endpoint_id` | `string` | Endpoint ID |
| `total` | `integer` | Total delivery attempts |
| `pending` | `integer` | Deliveries currently queued |
| `sent` | `integer` | Deliveries handed off to the provider |
| `delivered` | `integer` | Deliveries confirmed received by the destination |
| `failed` | `integer` | Deliveries that exhausted all retries |
| `success_rate` | `float` | `delivered / total` as a fraction in the range 0.0–1.0 |

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `401` | Authentication failure |
| `403` | Endpoint belongs to a different wallet |
| `404` | Endpoint not found |

---

### Retry Delivery

```
POST /api/v1/deliveries/{delivery_id}/retry
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Retries a failed delivery by re-submitting it to Convoy. Only deliveries in `failed` status may be retried. On success the delivery status is reset to `pending`.

**Path Parameters**

| Parameter | Type | Description |
|---|---|---|
| `delivery_id` | `string` | The delivery ID |

**Response — 202 Accepted**

```json
{
  "detail": "Retry requested",
  "delivery_id": "del123"
}
```

**Status Codes**

| Code | Condition |
|---|---|
| `202` | Retry enqueued successfully |
| `400` | Delivery is not in `failed` status; or the endpoint has no Convoy project configured; or the delivery has no `provider_message_id` |
| `401` | Authentication failure |
| `403` | Parent endpoint belongs to a different wallet |
| `404` | Delivery not found, or parent endpoint not found |
| `502` | Convoy returned an error when the retry was requested |

---

## Stats

### Get Stats

```
GET /api/v1/stats
```

**Authentication:** Required (SIWE).

**Rate limit:** 30 requests per minute.

Returns aggregate processing statistics scoped to the authenticated wallet's endpoints.

**Response — 200 OK**

```json
{
  "total_events": 512,
  "events_last_hour": 14,
  "active_endpoints": 3,
  "last_event_at": "2024-01-01T12:00:00Z"
}
```

| Field | Type | Description |
|---|---|---|
| `total_events` | `integer` | Total number of events across all owned endpoints |
| `events_last_hour` | `integer` | Events created in the last 60 minutes |
| `active_endpoints` | `integer` | Number of currently active endpoints owned by this wallet |
| `last_event_at` | `string \| null` | ISO-8601 timestamp of the most recent event, or `null` if none exist |

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Success |
| `401` | Authentication failure |

---

## Ingest

Ingest endpoints receive ERC-3009 transfer data from external sources (Goldsky webhook sink). They do **not** use SIWE authentication. Instead they require `Authorization: Bearer <secret>` validated against the server-configured webhook secret.

In development environments where no secret is configured the authorization check is skipped. In all other environments the secret must be set or the server returns `500`.

---

### Ingest Goldsky Batch

```
POST /api/v1/ingest/goldsky
```

**Authentication:** `Authorization: Bearer <GOLDSKY_WEBHOOK_SECRET>`

**Rate limit:** 100 requests per minute.

Receives a batch of decoded ERC-3009 `AuthorizationUsed` log events from Goldsky's webhook sink. Goldsky sends either a single event object or an array of objects; both forms are accepted.

Each log row contains fields decoded by Goldsky's `_gs_log_decode()`:

| Field | Description |
|---|---|
| `transaction_hash` | On-chain transaction hash |
| `block_number` | Block number |
| `block_hash` | Block hash |
| `log_index` | Log index within the block |
| `block_timestamp` | Block timestamp |
| `address` | USDC contract address that emitted the log |
| `chain_id` | Chain ID integer |
| `decoded` | Object containing `authorizer` (address) and `nonce` (bytes32 hex) |

**Request Body**

A JSON array of log row objects, or a single log row object.

**Response — 200 OK**

```json
{
  "processed": 5,
  "results": [
    { "status": "dispatched", "tx_hash": "0x...", "event_id": "evt123" },
    { "status": "duplicate", "tx_hash": "0x...", "event_id": null }
  ]
}
```

| Field | Type | Description |
|---|---|---|
| `processed` | `integer` | Number of events in the batch that were processed |
| `results` | `object[]` | Per-event processing result objects |

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Batch processed (individual failures are reported per-item in `results`) |
| `401` | Missing or invalid `Authorization` header |
| `429` | Rate limit exceeded |
| `500` | `GOLDSKY_WEBHOOK_SECRET` is not configured in a non-development environment |

---

### Ingest Single Event

```
POST /api/v1/ingest/event
```

**Authentication:** `Authorization: Bearer <GOLDSKY_WEBHOOK_SECRET>`

**Rate limit:** 100 requests per minute.

Processes a single raw event object. Intended for testing or manual event submission. Uses the same authentication and processing pipeline as the batch endpoint.

**Request Body**

A single raw event object in the same format as an individual row from the Goldsky batch payload.

**Response — 200 OK**

```json
{
  "status": "dispatched",
  "tx_hash": "0x...",
  "event_id": "evt123"
}
```

| Field | Type | Description |
|---|---|---|
| `status` | `string` | Processing outcome (e.g. `"dispatched"`, `"duplicate"`, `"no_match"`) |
| `tx_hash` | `string \| null` | Transaction hash of the processed event |
| `event_id` | `string \| null` | TripWire event ID assigned to this transfer |

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Event processed |
| `401` | Missing or invalid `Authorization` header |
| `429` | Rate limit exceeded |
| `500` | `GOLDSKY_WEBHOOK_SECRET` is not configured in a non-development environment |

---

## Facilitator

### Ingest Facilitator Pre-Settlement Event

```
POST /api/v1/ingest/facilitator
```

**Authentication:** `Authorization: Bearer <FACILITATOR_WEBHOOK_SECRET>`

**Rate limit:** 100 requests per minute.

Receives a structured ERC-3009 authorization from the x402 facilitator **before** the transaction is submitted on-chain. The facilitator has already verified the ERC-3009 signature. TripWire runs the fast path only: nonce deduplication, identity resolution, policy evaluation, and dispatch (target latency ~100 ms).

Because no transaction has been mined yet, there is no `tx_hash` or `block_number`. TripWire assigns a deterministic pseudo-tx-hash for internal tracking. When the real transaction lands on-chain and is ingested through the Goldsky path, the two events can be correlated by their ERC-3009 `nonce`.

**Request Body**

| Field | Type | Required | Description |
|---|---|---|---|
| `from_address` | `string` | Yes | EIP-55 Ethereum address of the sender (`0x` + 40 hex chars) |
| `to_address` | `string` | Yes | EIP-55 Ethereum address of the recipient |
| `amount` | `string` | Yes | Transfer amount in USDC atomic units (6 decimals) represented as a string |
| `nonce` | `string` | Yes | ERC-3009 bytes32 hex nonce |
| `chain_id` | `integer` | Yes | Chain ID. Must be one of: `1`, `8453`, `42161` |
| `token` | `string` | Yes | USDC contract address for the given chain (`0x` + 40 hex chars) |
| `valid_after` | `integer` | Yes | Unix timestamp after which the ERC-3009 authorization is valid |
| `valid_before` | `integer` | Yes | Unix timestamp before which the ERC-3009 authorization is valid |
| `signature_verified` | `boolean` | Yes | Must be `true`. The facilitator asserts it has verified the ERC-3009 signature. Requests with `false` are rejected with `422`. |

**USDC Contract Addresses**

| Chain | Address |
|---|---|
| Base (8453) | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Ethereum (1) | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` |
| Arbitrum One (42161) | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` |

**Response — 200 OK**

```json
{
  "status": "dispatched",
  "event_id": "evt123",
  "tx_hash": "0x000000000000000000000000<uuid-hex>"
}
```

| Field | Type | Description |
|---|---|---|
| `status` | `string` | Processing outcome: `"dispatched"`, `"duplicate"`, `"no_match"`, or `"error"` |
| `event_id` | `string \| null` | TripWire event ID, present when a matching endpoint was found and dispatched |
| `tx_hash` | `string \| null` | Pseudo-tx-hash assigned for later correlation with the on-chain event |

**Status Codes**

| Code | Condition |
|---|---|
| `200` | Event processed |
| `401` | Missing or invalid `Authorization` header |
| `422` | `signature_verified` is `false`; or `chain_id` is not one of the supported values; or `token` is not a known USDC address |
| `429` | Rate limit exceeded |
| `500` | `FACILITATOR_WEBHOOK_SECRET` is not configured in a non-development environment |

---

## Health

Health endpoints require no authentication and are not subject to application-level rate limits. They are intended for load balancers, uptime monitors, and container orchestrators.

### Liveness Probe

```
GET /health
```

Returns `200 OK` if the HTTP server process is running. Does not probe downstream dependencies.

**Response — 200 OK**

```json
{
  "status": "ok",
  "service": "tripwire",
  "version": "1.0.0"
}
```

---

### Detailed Health Check

```
GET /health/detailed
```

Probes each downstream component and reports their individual statuses. Returns `503` if any component is unhealthy.

**Response — 200 OK (all healthy)**

```json
{
  "status": "healthy",
  "version": "1.0.0",
  "uptime_seconds": 3600.0,
  "components": {
    "supabase": { "status": "healthy" },
    "webhook_provider": { "status": "healthy", "type": "convoy" },
    "identity_resolver": { "status": "healthy", "type": "ERC8004Resolver" }
  }
}
```

**Response — 503 Service Unavailable (component unhealthy)**

Same shape as above, with `"status": "unhealthy"` at the top level. The affected component object includes an `"error"` field with a description.

| Top-level `status` | Meaning |
|---|---|
| `"healthy"` | All components reported healthy |
| `"degraded"` | Some components are neither healthy nor unhealthy |
| `"unhealthy"` | One or more components are unhealthy |

**Status Codes**

| Code | Condition |
|---|---|
| `200` | All components healthy |
| `503` | One or more components unhealthy |

---

### Readiness Probe

```
GET /ready
```

Returns `200 OK` only after the application's lifespan startup has completed — all repositories, processors, and background tasks (finality poller, DLQ handler) have been initialized. Returns `503` if startup is still in progress.

**Response — 200 OK**

```json
{ "ready": true }
```

**Response — 503 Service Unavailable**

```json
{ "ready": false }
```

---

## Webhook Payload Reference

When TripWire dispatches a webhook to a registered `execute`-mode endpoint, it sends a signed `POST` request to the endpoint's URL. The body is signed using HMAC-SHA256 with the endpoint's `webhook_secret`.

### Payload Structure

```json
{
  "id": "evt123",
  "idempotency_key": "idem-key-string",
  "type": "payment.confirmed",
  "mode": "execute",
  "timestamp": 1704067200,
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0x...",
      "block_number": 12345678,
      "from_address": "0xSenderAddress",
      "to_address": "0xRecipientAddress",
      "amount": "1000000",
      "nonce": "0x...",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": {
      "confirmations": 12,
      "required_confirmations": 12,
      "is_finalized": true
    },
    "identity": {
      "address": "0xSenderAddress",
      "agent_class": "PaymentAgent",
      "deployer": "0xDeployerAddress",
      "capabilities": ["transfer"],
      "reputation_score": 95.0,
      "registered_at": 1700000000,
      "metadata": {}
    }
  }
}
```

### Field Reference

| Field | Type | Description |
|---|---|---|
| `id` | `string` | Unique event ID |
| `idempotency_key` | `string` | Stable key for idempotent processing on the receiver side |
| `type` | `WebhookEventType` | One of the event types listed in [WebhookEventType](#webhookeventtype) |
| `mode` | `EndpointMode` | Always `"execute"` for Convoy-delivered webhooks |
| `timestamp` | `integer` | Unix timestamp when the event was created |
| `data.transfer` | `object` | On-chain transfer details |
| `data.finality` | `object \| null` | Finality confirmation details. `null` for `payment.pre_confirmed` events (no block yet). |
| `data.identity` | `object \| null` | ERC-8004 agent identity of the sender. `null` when the sender is not a registered agent or identity resolution is unavailable. |

### Transfer Fields

| Field | Type | Description |
|---|---|---|
| `chain_id` | `integer` | Chain ID where the transfer occurred |
| `tx_hash` | `string` | On-chain transaction hash (pseudo-hash for pre-confirmed events) |
| `block_number` | `integer` | Block number containing the transaction |
| `from_address` | `string` | Sender address |
| `to_address` | `string` | Recipient address |
| `amount` | `string` | Transfer amount in USDC atomic units (6 decimals). Divide by `1_000_000` for the human-readable value. |
| `nonce` | `string` | ERC-3009 bytes32 hex nonce |
| `token` | `string` | USDC contract address |

### Finality Fields

| Field | Type | Description |
|---|---|---|
| `confirmations` | `integer` | Number of block confirmations at the time of dispatch |
| `required_confirmations` | `integer` | Threshold set in the endpoint's `finality_depth` policy |
| `is_finalized` | `boolean` | `true` when `confirmations >= required_confirmations` |
