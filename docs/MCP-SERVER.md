# TripWire MCP Server

MCP (Model Context Protocol) server for AI agent integration with TripWire's programmable onchain event trigger platform.

## Overview

The MCP server is mounted as a FastAPI sub-application at `/mcp`. It speaks JSON-RPC 2.0 over HTTP POST and implements MCP protocol version `2024-11-05`.

All requests go to a single endpoint:

```
POST /mcp
Content-Type: application/json
```

The server exposes 8 tools for trigger management, template browsing, and event querying. Two built-in methods (`initialize` and `tools/list`) require no authentication. Tool invocations go through a 3-tier authentication system that combines wallet signatures (SIWE) and per-call micropayments (x402).

MCP is the **control plane**: agents use it to configure what events to watch and where to deliver them. **Goldsky Turbo is the data plane**: it indexes the target chains in real time and delivers matching event logs to TripWire's `/ingest` endpoint via webhook. When an agent registers triggers through MCP, no additional blockchain infrastructure is required — events flow automatically from Goldsky's indexing pipeline into TripWire's event processor, which evaluates them against registered triggers and dispatches webhooks.

MCP tools map to the [TWSS-1 Skill Spec](SKILL-SPEC.md) lifecycle: `register_middleware` and `create_trigger` create skill definitions, `activate_template` instantiates skills from the Bazaar, and `search_events` returns results with the TWSS-1 [execution output contract](SKILL-SPEC.md#6-skill-output-contract) (`execution.state`, `execution.safe_to_execute`, `execution.trust_source`).

### Server Info

| Field             | Value           |
|-------------------|-----------------|
| Protocol version  | `2024-11-05`    |
| Server name       | `tripwire-mcp`  |
| Server version    | `1.0.0`         |
| Transport         | JSON-RPC 2.0 over HTTP POST |
| Capabilities      | `tools` (listChanged: false) |

---

## Authentication Tiers

Every tool is assigned one of three authentication tiers. The tier determines what headers the caller must include.

### PUBLIC

No authentication required. Used for protocol handshake and tool discovery.

- Methods: `initialize`, `tools/list`

### SIWX (Sign-In With X)

Wallet signature via SIWE (EIP-4361). The caller must include the following headers:

| Header                     | Description                        |
|----------------------------|------------------------------------|
| `X-TripWire-Address`      | Wallet address (0x...)             |
| `X-TripWire-Signature`    | SIWE signature                     |
| `X-TripWire-Nonce`        | One-time nonce from `/auth/nonce`  |
| `X-TripWire-Issued-At`    | ISO 8601 timestamp                 |
| `X-TripWire-Expiration`   | ISO 8601 expiration time           |

The signature covers a SIWE message that includes the HTTP method, path, and SHA-256 hash of the request body. Nonces are single-use and consumed atomically via Redis.

After SIWE authentication succeeds, the server resolves the caller's ERC-8004 identity on Base (chain ID 8453). Two onchain registry contracts are queried — `IdentityRegistry` and `ReputationRegistry` — to populate the `MCPAuthContext` with:

- `identity`: full `AgentIdentity` record (name, metadata, registration block)
- `reputation_score`: integer 0–100

If a tool's `min_reputation` threshold is set above 0 and the caller's score is below it, the request is rejected with `-32001 REPUTATION_TOO_LOW`. X402-tier tools (`register_middleware`, `create_trigger`, `activate_template`) require `min_reputation >= 10.0`. SIWX-tier tools remain at `min_reputation=0`. Agents with a reputation score below 10 calling a paid tool will receive JSON-RPC error code `-32001` ("Reputation too low").

### X402 (Per-Call Payment)

Micropayment via the x402 protocol. The caller must include:

| Header       | Description                                |
|--------------|--------------------------------------------|
| `X-PAYMENT`  | x402 payment proof (ERC-3009 authorization)|

Payment is verified before tool execution but settled only after successful execution. If settlement fails, the tool result is withheld and the payment dedup key is cleaned up so the caller can retry. Replay protection uses a SHA-256 hash of the payment proof stored in Redis with a 24-hour TTL.

### Pricing Table

| Tool                | Auth Tier | Price   | Network        | Min Reputation |
|---------------------|-----------|---------|----------------|----------------|
| `register_middleware` | X402    | $0.003  | eip155:8453    | 10.0           |
| `create_trigger`      | X402    | $0.003  | eip155:8453    | 10.0           |
| `activate_template`   | X402    | $0.001  | eip155:8453    | 10.0           |
| `list_triggers`       | SIWX    | free    | --             | 0              |
| `delete_trigger`      | SIWX    | free    | --             | 0              |
| `list_templates`      | SIWX    | free    | --             | 0              |
| `get_trigger_status`  | SIWX    | free    | --             | 0              |
| `search_events`       | SIWX    | free    | --             | 0              |

All x402 payments are on Base (chain ID 8453) and paid to the treasury address configured in `TRIPWIRE_TREASURY_ADDRESS`.

---

## Tool Reference

### 1. register_middleware

Creates an endpoint and optionally creates triggers from template slugs or custom definitions. This is the primary onboarding tool for agents.

- **Auth tier:** X402 ($0.003)
- **Ownership:** The authenticated agent becomes the `owner_address` of the created endpoint and all triggers.

Once registered, new triggers are automatically picked up by the event processor. Events arrive from Goldsky Turbo's real-time indexing pipeline — no additional infrastructure setup required.

**Input schema:**

| Parameter         | Type       | Required | Default     | Description                                          |
|-------------------|------------|----------|-------------|------------------------------------------------------|
| `url`             | string     | yes      | --          | Webhook/callback URL                                 |
| `mode`            | string     | no       | `"execute"` | `"notify"` (Supabase Realtime) or `"execute"` (POST) |
| `chains`          | int[]      | no       | `[8453]`    | Chain IDs to monitor                                 |
| `recipient`       | string     | no       | agent address | Recipient address to watch                         |
| `policies`        | object     | no       | `{}`        | Endpoint policies (see EndpointPolicies)             |
| `template_slugs`  | string[]   | no       | `[]`        | Template slugs to instantiate as triggers            |
| `custom_triggers` | object[]   | no       | `[]`        | Custom trigger definitions (see below)               |

**Custom trigger object:**

| Field              | Type    | Required | Description                           |
|--------------------|---------|----------|---------------------------------------|
| `event_signature`  | string  | yes      | Solidity event signature              |
| `name`             | string  | no       | Human-readable name                   |
| `abi`              | array   | no       | ABI fragment for the event            |
| `contract_address` | string  | no       | Contract to watch (null = any)        |
| `chain_ids`        | int[]   | no       | Chain IDs (defaults to endpoint chains)|
| `filter_rules`     | array   | no       | Filter predicates on decoded fields   |
| `webhook_event_type` | string | no      | Defaults to `"payment.confirmed"`     |

Available `webhook_event_type` values: `payment.confirmed`, `payment.pending`, `payment.pre_confirmed`, `payment.failed`, `payment.reorged`, `payment.finalized`.

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "register_middleware",
    "arguments": {
      "url": "https://myagent.example.com/webhook",
      "mode": "execute",
      "chains": [8453],
      "template_slugs": ["x402-usdc-payment"],
      "custom_triggers": [
        {
          "event_signature": "Transfer(address,address,uint256)",
          "name": "ERC-20 Transfer",
          "contract_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        }
      ]
    }
  }
}
```

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"endpoint_id\": \"abc123...\", \"webhook_secret\": \"deadbeef...\", \"trigger_ids\": [\"trig1\", \"trig2\"], \"mode\": \"execute\", \"url\": \"https://myagent.example.com/webhook\"}"
      }
    ],
    "isError": false
  }
}
```

### 2. create_trigger

Creates a custom trigger for an existing endpoint. The caller must own the target endpoint.

- **Auth tier:** X402 ($0.003)
- **Ownership:** Endpoint must be owned by the authenticated agent (`owner_address` match).

**Input schema:**

| Parameter             | Type    | Required | Default              | Description                                  |
|-----------------------|---------|----------|----------------------|----------------------------------------------|
| `endpoint_id`         | string  | yes      | --                   | Target endpoint ID                           |
| `event_signature`     | string  | yes      | --                   | Solidity event signature                     |
| `name`                | string  | no       | null                 | Human-readable trigger name                  |
| `abi`                 | array   | no       | `[]`                 | ABI fragment for the event                   |
| `contract_address`    | string  | no       | null                 | Contract to watch (null = any)               |
| `chain_ids`           | int[]   | no       | endpoint's chains    | Chain IDs to monitor                         |
| `filter_rules`        | array   | no       | `[]`                 | Filter predicates on decoded event fields    |
| `webhook_event_type`  | string  | no       | `"payment.confirmed"`| Event type sent in webhook payload (see event types below) |
| `reputation_threshold`| number  | no       | `0.0`                | Min reputation score for senders             |

Available `webhook_event_type` values: `payment.confirmed`, `payment.pending`, `payment.pre_confirmed`, `payment.failed`, `payment.reorged`, `payment.finalized`.

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "method": "tools/call",
  "params": {
    "name": "create_trigger",
    "arguments": {
      "endpoint_id": "abc123",
      "event_signature": "Approval(address,address,uint256)",
      "name": "USDC Approval",
      "chain_ids": [8453, 1]
    }
  }
}
```

**Example response:**

```json
{
  "jsonrpc": "2.0",
  "id": 2,
  "result": {
    "content": [
      {
        "type": "text",
        "text": "{\"trigger_id\": \"trig_xyz\", \"endpoint_id\": \"abc123\", \"event_signature\": \"Approval(address,address,uint256)\", \"active\": true}"
      }
    ],
    "isError": false
  }
}
```

### 3. list_triggers

Lists all triggers owned by the calling agent.

- **Auth tier:** SIWX (free)
- **Ownership:** Only returns triggers where `owner_address` matches the authenticated agent.

**Input schema:**

| Parameter     | Type    | Required | Default | Description                   |
|---------------|---------|----------|---------|-------------------------------|
| `active_only` | boolean | no       | `true`  | Only return active triggers   |

**Example response (content.text parsed):**

```json
{
  "triggers": [
    {
      "id": "trig_xyz",
      "name": "USDC Transfer",
      "endpoint_id": "abc123",
      "event_signature": "Transfer(address,address,uint256)",
      "chain_ids": [8453],
      "active": true,
      "created_at": "2026-03-15T10:30:00+00:00"
    }
  ],
  "count": 1
}
```

### 4. delete_trigger

Soft-deletes a trigger by setting `active = false`. The caller must own the trigger.

- **Auth tier:** SIWX (free)
- **Ownership:** Trigger must be owned by the authenticated agent.

**Input schema:**

| Parameter    | Type   | Required | Description              |
|--------------|--------|----------|--------------------------|
| `trigger_id` | string | yes      | Trigger ID to deactivate |

**Example response (content.text parsed):**

```json
{
  "trigger_id": "trig_xyz",
  "active": false
}
```

### 5. list_templates

Browses available trigger templates from the Bazaar. Returns only public templates.

- **Auth tier:** SIWX (free)

**Input schema:**

| Parameter  | Type   | Required | Description                                     |
|------------|--------|----------|-------------------------------------------------|
| `category` | string | no       | Filter by category (e.g. `"defi"`, `"payments"`, `"nft"`) |

**Example response (content.text parsed):**

```json
{
  "templates": [
    {
      "slug": "x402-usdc-payment",
      "name": "x402 USDC Payment",
      "description": "Watch for ERC-3009 transferWithAuthorization events",
      "category": "payments",
      "event_signature": "TransferWithAuthorization(address,address,uint256,uint256,uint256,bytes32)",
      "default_chains": [8453],
      "parameter_schema": [],
      "reputation_threshold": 0.0,
      "install_count": 42
    }
  ],
  "count": 1
}
```

### 6. activate_template

Instantiates a Bazaar template with custom parameters for an existing endpoint. The caller must own the target endpoint.

- **Auth tier:** X402 ($0.001)
- **Ownership:** Endpoint must be owned by the authenticated agent.

**Input schema:**

| Parameter     | Type   | Required | Description                                              |
|---------------|--------|----------|----------------------------------------------------------|
| `slug`        | string | yes      | Template slug                                            |
| `endpoint_id` | string | yes      | Target endpoint ID                                       |
| `params`      | object | no       | Custom parameters: `chain_ids`, `contract_address`, `filter_rules` |

Custom `params.filter_rules`, if provided, replace the template's default filters entirely rather than merging.

**Example request:**

```json
{
  "jsonrpc": "2.0",
  "id": 6,
  "method": "tools/call",
  "params": {
    "name": "activate_template",
    "arguments": {
      "slug": "x402-usdc-payment",
      "endpoint_id": "abc123",
      "params": {
        "chain_ids": [8453, 42161],
        "contract_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
      }
    }
  }
}
```

**Example response (content.text parsed):**

```json
{
  "trigger_id": "trig_tmpl",
  "template_slug": "x402-usdc-payment",
  "endpoint_id": "abc123",
  "event_signature": "TransferWithAuthorization(address,address,uint256,uint256,uint256,bytes32)",
  "active": true
}
```

### 7. get_trigger_status

Returns trigger health information and event count. The caller must own the trigger.

- **Auth tier:** SIWX (free)
- **Ownership:** Trigger must be owned by the authenticated agent.

**Input schema:**

| Parameter    | Type   | Required | Description          |
|--------------|--------|----------|----------------------|
| `trigger_id` | string | yes      | Trigger ID to check  |

**Example response (content.text parsed):**

```json
{
  "trigger_id": "trig_xyz",
  "name": "USDC Transfer",
  "event_signature": "Transfer(address,address,uint256)",
  "chain_ids": [8453],
  "active": true,
  "event_count": 137,
  "last_event_execution_state": "confirmed",
  "created_at": "2026-03-15T10:30:00+00:00"
}
```

The `event_count` field returns `-1` if the count query fails.

The `last_event_execution_state` field is a string or `null`. It reports the execution state of the most recent event matched by this trigger, using the same values as the `execution_state` field in `search_events` (`"provisional"`, `"confirmed"`, `"finalized"`, `"reorged"`). It is `null` if the trigger has not yet matched any events.

### 8. search_events

Queries recent events across all of the caller's active endpoints. Uses the `event_endpoints` join table.

- **Auth tier:** SIWX (free)
- **Ownership:** Only returns events linked to endpoints owned by the authenticated agent.

**Input schema:**

| Parameter  | Type    | Required | Default | Description                                |
|------------|---------|----------|---------|--------------------------------------------|
| `limit`    | integer | no       | `50`    | Max results (clamped to 1-100)             |
| `status`   | string  | no       | --      | Filter by status (e.g. `"confirmed"`, `"pending"`) |
| `chain_id` | integer | no       | --      | Filter by chain ID                         |

**Example response (content.text parsed):**

```json
{
  "events": [
    {
      "id": "evt_abc",
      "tx_hash": "0xdead...",
      "chain_id": 8453,
      "status": "confirmed",
      "block_number": 12345678,
      "created_at": "2026-03-15T10:31:00+00:00",
      "execution_state": "confirmed",
      "safe_to_execute": false,
      "trust_source": "onchain"
    }
  ],
  "count": 1
}
```

Each event in the response now includes three execution metadata fields:

| Field             | Type    | Description                                                                                              |
|-------------------|---------|----------------------------------------------------------------------------------------------------------|
| `execution_state` | string  | One of `provisional`, `confirmed`, `finalized`, `reorged`. Derived from event type and finality data     |
| `safe_to_execute` | boolean | `true` only when the event is finalized (enough confirmations or `payment.finalized` event type)         |
| `trust_source`    | string  | `"facilitator"` for pre-confirmed events (off-chain attestation), `"onchain"` for all others             |

These fields follow the same derivation rules documented in the [Webhook Payload Shape](#webhook-payload-shape) section. Agents should gate irreversible side-effects on `safe_to_execute == true`.

---

## Webhook Payload Shape

Webhook payloads delivered to agents (via trigger execution or returned by `search_events`) include execution metadata fields that indicate how much trust to place in the event:

| Field             | Type     | Description                                                                                              |
|-------------------|----------|----------------------------------------------------------------------------------------------------------|
| `version`         | string   | Payload schema version (currently `"v1"`)                                                                |
| `execution_state` | string   | One of `provisional`, `confirmed`, `finalized`, `reorged`. Derived from the event type and finality data |
| `safe_to_execute` | boolean  | `true` only when the event is finalized (enough confirmations or `payment.finalized` event type)         |
| `trust_source`    | string   | `"facilitator"` for pre-confirmed events (off-chain attestation), `"onchain"` for all others             |

Derivation rules:

- `payment.pre_confirmed` → `provisional`, `safe_to_execute=false`, `trust_source="facilitator"`
- `payment.reorged` / `payment.failed` → `reorged`, `safe_to_execute=false`, `trust_source="onchain"`
- `payment.finalized` or finality data shows `is_finalized=true` → `finalized`, `safe_to_execute=true`, `trust_source="onchain"`
- All other confirmed events → `confirmed`, `safe_to_execute=false`, `trust_source="onchain"`

Agents should gate irreversible side-effects (e.g., releasing goods, granting access) on `safe_to_execute == true`.

---

## x402 Bazaar

The x402 Bazaar is a service discovery mechanism. TripWire publishes a manifest at:

```
GET /.well-known/x402-manifest.json
```

The manifest advertises available paid services, MCP tools, auth configuration, and supported chains. Agents and clients can fetch this to discover what TripWire offers and how to authenticate.

### Manifest Structure

```json
{
  "@context": "https://x402.org/context",
  "name": "TripWire",
  "description": "Programmable onchain event triggers for AI agents...",
  "version": "1.0.0",
  "identity": {
    "protocol": "ERC-8004",
    "registry": "<ERC-8004 registry address>"
  },
  "auth": {
    "siwe": {
      "nonce_endpoint": "/auth/nonce",
      "domain": "<SIWE domain>"
    },
    "x402": {
      "facilitator": "<facilitator URL>",
      "network": "<CAIP-2 network>",
      "pay_to": "<treasury address>"
    }
  },
  "mcp": {
    "endpoint": "/mcp",
    "transport": "json-rpc",
    "tools": [
      { "name": "register_middleware", "auth_tier": "x402", "price": "$0.003" },
      { "name": "list_triggers", "auth_tier": "siwx" }
    ]
  },
  "services": [
    {
      "name": "register_middleware",
      "description": "...",
      "endpoint": "/mcp",
      "method": "POST",
      "scheme": "exact",
      "price": "$0.003",
      "network": "eip155:8453",
      "pay_to": "<treasury address>"
    }
  ],
  "supported_chains": [
    { "chain_id": 8453, "name": "Base" },
    { "chain_id": 1, "name": "Ethereum" },
    { "chain_id": 42161, "name": "Arbitrum" }
  ]
}
```

The `identity.protocol` field is set to `"ERC-8004"` to signal that TripWire uses the ERC-8004 onchain agent identity registry for caller identity discovery. Agents with an ERC-8004 registration on Base have their `AgentIdentity` and `reputation_score` resolved and attached to every authenticated MCP request.

### Discovery Flow

1. Agent fetches `GET /.well-known/x402-manifest.json`
2. Reads `auth.siwe.nonce_endpoint` to get a SIWE nonce
3. Reads `auth.x402` for payment facilitator config
4. Reads `mcp.tools` to discover available tools and their auth tiers
5. Calls `POST /mcp` with `tools/list` for full input schemas
6. Calls tools with appropriate auth headers

---

## Tool Execution Flow

MCP tools operate on the **control plane** only — they create, modify, and query trigger configuration stored in TripWire's database. The **data plane** is entirely separate: Goldsky Turbo indexes the configured chains and pushes event logs to TripWire's `/ingest` endpoint via webhook. TripWire does not poll RPC nodes; all event data originates from Goldsky's indexing pipeline.

Every `tools/call` request follows this pipeline:

1. **Parse** -- JSON body is parsed; `jsonrpc`, `method`, `params` are extracted. Malformed JSON returns `-32700 Parse error`.

2. **Resolve tool** -- The tool name is looked up in the registry. Unknown tools return `-32601 Method not found`.

3. **Authenticate** -- `build_auth_context()` runs the appropriate auth flow based on the tool's `AuthTier`:
   - PUBLIC: no-op
   - SIWX: verify SIWE headers, recover wallet address, consume nonce
   - X402: verify `X-PAYMENT` header, check replay protection, verify payment proof via facilitator

4. **Agent address check** -- Non-PUBLIC tools require a resolved `agent_address`. If missing, returns `-32000`.

5. **Reputation check** -- If the tool has `min_reputation > 0`, the agent's reputation score is compared against the threshold. The score is sourced from the ERC-8004 `ReputationRegistry` contract on Base (chain ID 8453) via a raw JSON-RPC call; results are cached for 300 seconds to avoid per-request onchain lookups. Below threshold returns `-32001`.

6. **Rate limit** -- Per-address rate limiting: 60 calls/minute per wallet address, enforced via Redis INCR with 60-second TTL. Exceeding the limit returns `-32003`. If Redis is unavailable, rate limiting fails open.

7. **Execute** -- The tool handler is called with `(params, auth_context, repos)`. Unhandled exceptions return `-32603 Internal error`.

8. **Settle** -- For X402 tools, if the tool returned a successful result (no `"error"` key), `settle_payment()` is called to finalize the x402 payment via the facilitator. If settlement fails, the tool result is withheld and the dedup key is cleaned up.

9. **Audit log** -- Every tool call is logged to the `audit_log` table via fire-and-forget write, recording: action, actor address, auth tier, payment status, arguments, and success/failure.

10. **Return** -- The result is wrapped in MCP `content` format: `{ "content": [{ "type": "text", "text": "<JSON>" }], "isError": bool }`.

---

## Error Codes

### Standard JSON-RPC Errors

| Code     | Name              | Meaning                                    |
|----------|-------------------|--------------------------------------------|
| `-32700` | Parse error       | Malformed JSON body                        |
| `-32600` | Invalid request   | Missing `jsonrpc: "2.0"` or `method` field |
| `-32601` | Method not found  | Unknown method or unknown tool name        |
| `-32602` | Invalid params    | (reserved, not currently raised)           |
| `-32603` | Internal error    | Unhandled exception in tool handler or auth|

### Application Errors

| Code     | Name              | Meaning                                                    |
|----------|-------------------|------------------------------------------------------------|
| `-32000` | Auth required     | Missing or invalid SIWE headers; no agent address resolved |
| `-32001` | Reputation too low| Agent reputation below tool's `min_reputation` threshold   |
| `-32002` | Payment required  | Missing, invalid, replayed, or failed x402 payment         |
| `-32003` | Rate limited      | Exceeded 60 tool calls per minute for this address         |

All errors are returned with HTTP status 200 (per JSON-RPC convention). The error object includes `code` and `message`; some include a `data` field with additional context.

---

## Rate Limiting

- **Limit:** 60 tool calls per minute per wallet address.
- **Backend:** Redis `INCR` with 60-second `EXPIRE`.
- **Key format:** `mcp:rate:{agent_address}`
- **Failure mode:** If Redis is unavailable, rate limiting fails open (requests are allowed through). Authentication must still pass.
- **Scope:** Applies to all `tools/call` invocations where an `agent_address` is resolved. Does not apply to `initialize` or `tools/list`.

---

## Audit Logging

Every `tools/call` invocation is logged to the `audit_log` table, regardless of success or failure. The write is fire-and-forget (does not block the response).

**Audit record fields:**

| Field          | Value                                           |
|----------------|-------------------------------------------------|
| `action`       | `mcp.tools.<tool_name>`                         |
| `actor`        | Agent wallet address, or `"anonymous"`          |
| `resource_type`| `mcp_tool`                                      |
| `resource_id`  | Tool name                                       |
| `details`      | `{ arguments, auth_tier, payment_verified, success, execution_latency_ms }` |
| `ip_address`   | Client IP from request                          |

---

## Known Issues

1. **Unused webhook_secret in register_middleware.** The handler generates a `webhook_secret` via `secrets.token_hex(32)` and returns it in the response, but it is never stored in the endpoint row or passed to Convoy. The secret is effectively useless -- Convoy is never configured to use it for HMAC signing on endpoints created through MCP.

2. **No validation on filter_rules operators.** The `TriggerFilter` model accepts any string for the `op` field. Invalid operators (e.g., `"banana"`) are accepted silently and stored in the database. They will fail at event evaluation time with no feedback to the caller at creation time.

3. **No timeout on tool handler execution.** Tool handlers run without any timeout. A slow database query or hung external call will block indefinitely. There is no `asyncio.wait_for` or equivalent wrapper around handler execution.

4. **Reputation gating is active for paid tools only.** X402-tier tools (`register_middleware`, `create_trigger`, `activate_template`) require `min_reputation >= 10.0`. SIWX-tier tools remain at `min_reputation=0`. Changing thresholds still requires a code change.

5. **Tool pricing is hardcoded.** Prices are set at module import time in `server.py` via `_register()` calls. Changing a tool's price requires redeploying the application. There is no admin API or database-driven pricing.
