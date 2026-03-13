# TripWire Webhooks

This document is the definitive reference for TripWire webhook delivery. It covers the full payload schema for every event type, both delivery modes, HMAC signature verification, retry and dead-letter behaviour, idempotency, and working code examples.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Event Types](#2-event-types)
3. [Payload Schema](#3-payload-schema)
4. [Per-Event JSON Examples](#4-per-event-json-examples)
5. [Dynamic Trigger Webhooks](#5-dynamic-trigger-webhooks)
6. [Delivery Modes](#6-delivery-modes)
7. [HMAC Signature Verification](#7-hmac-signature-verification)
8. [Retry Policy](#8-retry-policy)
9. [Dead Letter Queue](#9-dead-letter-queue)
10. [Idempotency](#10-idempotency)
11. [Endpoint Policies](#11-endpoint-policies)
12. [Example Webhook Handler](#12-example-webhook-handler)
13. [Testing Webhooks](#13-testing-webhooks)
14. [Best Practices](#14-best-practices)

---

## 1. Overview

TripWire generates webhooks from two sources: **payment events** (ERC-3009 USDC transfers) and **dynamic triggers** (custom onchain event monitors created via the MCP server or API).

### Payment event pipeline

When a USDC transfer is detected on a monitored chain (Ethereum, Base, or Arbitrum), TripWire runs the following pipeline and delivers an event to your registered endpoint:

1. An on-chain ERC-3009 `TransferWithAuthorization` log is ingested from Goldsky Turbo.
2. TripWire decodes the log, deduplicates the ERC-3009 nonce, and checks finality.
3. The sender's optional ERC-8004 on-chain agent identity is resolved in parallel with finality.
4. The transfer is matched against registered endpoints by recipient address and chain.
5. Per-endpoint policies are evaluated (amount limits, sender allow/block lists, reputation thresholds).
6. For **Execute-mode** endpoints the payload is signed with HMAC-SHA256 and dispatched via Convoy, which handles retries, delivery logs, and the dead-letter queue.
7. For **Notify-mode** endpoints an event row is inserted into the `realtime_events` Supabase table, which Supabase Realtime broadcasts over WebSocket to subscribed clients.

The fast path for pre-confirmed payments (submitted through the x402 facilitator before the transaction lands on-chain) targets a sub-100 ms end-to-end latency and fires a `payment.pre_confirmed` event immediately.

### Dynamic trigger pipeline

Triggers are the primary way to monitor arbitrary onchain events beyond USDC payments. The recommended way to create triggers is through the **MCP server** (mounted at `/mcp`), which exposes tools like `create_trigger` and `list_triggers` for AI agents. Triggers can also be created via the REST API or instantiated from x402 Bazaar templates.

1. A trigger definition specifies a contract address, event signature, chain, and optional filter conditions.
2. TripWire registers a Goldsky pipeline (or uses an existing one) to watch the specified contract and event.
3. When a matching onchain event is detected, it is decoded using the trigger's ABI fragment.
4. The decoded event is delivered as a `trigger.{name}` webhook to all subscribed endpoints.

See [Section 5](#5-dynamic-trigger-webhooks) for the full payload format.

---

## 2. Event Types

| Event type | Trigger |
|---|---|
| `payment.pre_confirmed` | The x402 facilitator has validated the ERC-3009 authorization. The transaction is not yet on-chain. Delivered on the fast path (~100 ms). |
| `payment.pending` | The transaction is on-chain but has not yet accumulated the required number of block confirmations. |
| `payment.confirmed` | The transaction has reached the required finality depth and is considered irreversible. This is the primary event for triggering business logic. |
| `payment.failed` | The transaction failed or was reverted on-chain. |
| `payment.reorged` | A previously confirmed transaction was removed during a chain reorganization. If you acted on a `payment.confirmed` event you must reverse the action. |
| `trigger.{name}` | A dynamic trigger matched an onchain event. The `{name}` portion is the trigger's registered name (e.g., `trigger.large_swap`, `trigger.nft_mint`). See [Section 5](#5-dynamic-trigger-webhooks). |

### Finality depths by chain

| Chain | Chain ID | Required confirmations |
|---|---|---|
| Ethereum | 1 | 12 |
| Base | 8453 | 3 |
| Arbitrum | 42161 | 1 |

These are the system defaults. Per-endpoint overrides are configured via the `finality_depth` policy field (see [Section 11](#11-endpoint-policies)).

---

## 3. Payload Schema

Every webhook delivers a single `WebhookPayload` JSON object as the HTTP request body.

### Top-level fields

| Field | Type | Always present | Description |
|---|---|---|---|
| `id` | `string` | Yes | Unique UUID for this webhook delivery. |
| `idempotency_key` | `string` | Yes | Stable key for deduplication across retries (see [Section 10](#10-idempotency)). |
| `type` | `string` | Yes | One of the event types in [Section 2](#2-event-types). |
| `mode` | `string` | Yes | `"execute"` or `"notify"` — the delivery mode of the receiving endpoint. |
| `timestamp` | `integer` | Yes | Unix timestamp (seconds) at which TripWire created this payload. |
| `data` | `object` | Yes | Event data. Contains `transfer`, and optionally `finality` and `identity`. |

### `data.transfer` (TransferData)

Always present.

| Field | Type | Description |
|---|---|---|
| `chain_id` | `integer` | Chain where the transfer occurred. One of `1`, `8453`, `42161`. |
| `tx_hash` | `string` | Transaction hash (`0x`-prefixed, 66 characters). Empty string for `payment.pre_confirmed` events where the transaction is not yet on-chain. |
| `block_number` | `integer` | Block number containing the transfer. `0` for `payment.pre_confirmed`. |
| `from_address` | `string` | Sender Ethereum address (`0x`-prefixed, checksummed). |
| `to_address` | `string` | Recipient Ethereum address. This matches your registered endpoint's `recipient`. |
| `amount` | `string` | Transfer amount in token base units as a decimal string. USDC uses 6 decimal places: `"1000000"` = 1.000000 USDC. The value is kept as a string to preserve precision. |
| `nonce` | `string` | ERC-3009 authorization nonce (bytes32 hex, `0x`-prefixed). Used for deduplication. |
| `token` | `string` | USDC contract address for the chain. |

USDC contract addresses:

| Chain | Address |
|---|---|
| Base (8453) | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Ethereum (1) | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` |
| Arbitrum (42161) | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` |

### `data.finality` (FinalityData)

Present on `payment.pending` and `payment.confirmed`. Absent on `payment.pre_confirmed` (no on-chain transaction yet), `payment.failed`, and `payment.reorged`.

| Field | Type | Description |
|---|---|---|
| `confirmations` | `integer` | Block confirmations accumulated at dispatch time. |
| `required_confirmations` | `integer` | Confirmations required for finality (chain default or endpoint policy override). |
| `is_finalized` | `boolean` | `true` only on `payment.confirmed`. |

### `data.identity` (AgentIdentity)

Optional. Present when the sending address has a registered ERC-8004 on-chain agent identity. Absent when the sender is an ordinary EOA or the identity resolver finds no registration.

| Field | Type | Description |
|---|---|---|
| `address` | `string` | The agent's Ethereum address. |
| `agent_class` | `string` | Agent classification string, e.g., `"solver"`, `"keeper"`, `"liquidator"`. |
| `deployer` | `string` | Address that deployed the agent contract. |
| `capabilities` | `string[]` | Declared capability strings, e.g., `["swap", "bridge"]`. |
| `reputation_score` | `float` | Score from `0.0` to `100.0`. |
| `registered_at` | `integer` | Unix timestamp of identity registration. |
| `metadata` | `object` | Arbitrary key-value metadata set by the agent deployer. |

---

## 4. Per-Event JSON Examples

All `amount` values are in USDC base units (6 decimals). `"1000000"` = 1 USDC.

### `payment.pre_confirmed`

Fired immediately when the x402 facilitator validates the ERC-3009 authorization signature. The transaction has not landed on-chain yet. `tx_hash` and `block_number` reflect the pre-signed authorization data, not a confirmed on-chain transaction. No `finality` object is present.

```json
{
  "id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "idempotency_key": "idem_a3c2d1e0f9b84712c3a1d0e2f8b74321",
  "type": "payment.pre_confirmed",
  "mode": "execute",
  "timestamp": 1741737600,
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "",
      "block_number": 0,
      "from_address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
      "to_address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
      "amount": "5000000",
      "nonce": "0x9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": null,
    "identity": {
      "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
      "agent_class": "solver",
      "deployer": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
      "capabilities": ["swap", "bridge"],
      "reputation_score": 91.5,
      "registered_at": 1700000000,
      "metadata": {"version": "2"}
    }
  }
}
```

### `payment.pending`

Fired when the transaction is confirmed on-chain but has not yet accumulated the required number of block confirmations. `is_finalized` is `false`.

```json
{
  "id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "idempotency_key": "idem_b4d3e2f1a0c97823d4b2e1f3a9c87432",
  "type": "payment.pending",
  "mode": "execute",
  "timestamp": 1741737605,
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0x4e3a1b2c5d6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
      "block_number": 23456789,
      "from_address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
      "to_address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
      "amount": "5000000",
      "nonce": "0x9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": {
      "confirmations": 1,
      "required_confirmations": 3,
      "is_finalized": false
    },
    "identity": null
  }
}
```

### `payment.confirmed`

Fired when the transaction reaches the required finality depth. `is_finalized` is `true`. This is the authoritative event for fulfilling orders, crediting accounts, or triggering workflows.

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "idempotency_key": "idem_b4d3e2f1a0c97823d4b2e1f3a9c87432",
  "type": "payment.confirmed",
  "mode": "execute",
  "timestamp": 1741737670,
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0x4e3a1b2c5d6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
      "block_number": 23456789,
      "from_address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
      "to_address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
      "amount": "5000000",
      "nonce": "0x9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": {
      "confirmations": 3,
      "required_confirmations": 3,
      "is_finalized": true
    },
    "identity": {
      "address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
      "agent_class": "solver",
      "deployer": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
      "capabilities": ["swap", "bridge"],
      "reputation_score": 91.5,
      "registered_at": 1700000000,
      "metadata": {"version": "2"}
    }
  }
}
```

### `payment.failed`

Fired when a transaction that TripWire was tracking failed or was reverted on-chain. No `finality` object is present.

```json
{
  "id": "1b9d6bcd-bbfd-4b2d-9b5d-ab8dfbbd4bed",
  "idempotency_key": "idem_c5e4f3a2b1d08934e5c3f2a4b0d98543",
  "type": "payment.failed",
  "mode": "execute",
  "timestamp": 1741737800,
  "data": {
    "transfer": {
      "chain_id": 1,
      "tx_hash": "0xa1b2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90",
      "block_number": 19876543,
      "from_address": "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12",
      "to_address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
      "amount": "10000000",
      "nonce": "0x1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
      "token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    },
    "finality": null,
    "identity": null
  }
}
```

### `payment.reorged`

Fired when a chain reorganization removes a block that contained a previously reported transfer. If your application already processed a `payment.confirmed` event for the same `idempotency_key`, the action must be reversed. No `finality` object is present.

```json
{
  "id": "6ba7b810-9dad-11d1-80b4-00c04fd430c8",
  "idempotency_key": "idem_d6f5a4b3c2e19045f6d4a3b5c1e09654",
  "type": "payment.reorged",
  "mode": "execute",
  "timestamp": 1741737950,
  "data": {
    "transfer": {
      "chain_id": 1,
      "tx_hash": "0xb2c3d4e5f60718293a4b5c6d7e8f90a1b2c3d4e5f60718293a4b5c6d7e8f90a1",
      "block_number": 19876550,
      "from_address": "0xAbCdEf1234567890AbCdEf1234567890AbCdEf12",
      "to_address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
      "amount": "25000000",
      "nonce": "0x2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b3c",
      "token": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
    },
    "finality": null,
    "identity": null
  }
}
```

---

## 5. Dynamic Trigger Webhooks

Dynamic triggers let you monitor arbitrary onchain events beyond the built-in ERC-3009 payment pipeline. When a trigger fires, TripWire delivers a webhook with type `trigger.{name}` instead of the `payment.*` types used for USDC transfers.

### Creating triggers

The primary way to create triggers is through the **MCP server** (mounted at `/mcp`), which exposes tools that AI agents can call directly:

- `create_trigger` -- define a new trigger with contract address, event signature, chain ID, and optional filter conditions.
- `list_triggers` -- enumerate existing triggers for the authenticated wallet.
- `delete_trigger` -- remove a trigger and its associated Goldsky pipeline.

Triggers can also be created via the REST API (`POST /api/v1/triggers`) or instantiated from **x402 Bazaar templates** (pre-built trigger configurations for common patterns like large swaps, NFT mints, or governance votes).

### Bazaar template triggers

Bazaar templates are published in the x402 manifest (`/.well-known/x402-manifest.json`). When a user instantiates a Bazaar template, the resulting trigger behaves identically to a custom trigger -- it generates the same `trigger.{name}` webhook format and follows the same delivery pipeline.

### Webhook payload format

Dynamic trigger webhooks use the same top-level `WebhookPayload` structure as payment events, but with a different `type` and `data` shape.

| Field | Type | Always present | Description |
|---|---|---|---|
| `id` | `string` | Yes | Unique UUID for this webhook delivery. |
| `idempotency_key` | `string` | Yes | Stable key for deduplication across retries. |
| `type` | `string` | Yes | `trigger.{name}` where `{name}` is the trigger's registered name. |
| `mode` | `string` | Yes | `"execute"` or `"notify"`. |
| `timestamp` | `integer` | Yes | Unix timestamp (seconds) at which TripWire created this payload. |
| `data` | `object` | Yes | Trigger event data. Contains `trigger`, `event`, and optionally `identity`. |

#### `data.trigger` (TriggerMetadata)

| Field | Type | Description |
|---|---|---|
| `trigger_id` | `string` | UUID of the trigger definition that matched this event. |
| `trigger_name` | `string` | Human-readable trigger name (matches the `{name}` in the event type). |
| `contract_address` | `string` | The contract address being monitored (`0x`-prefixed, checksummed). |
| `event_signature` | `string` | The Solidity event signature (e.g., `Transfer(address,address,uint256)`). |

#### `data.event` (DecodedEvent)

| Field | Type | Description |
|---|---|---|
| `chain_id` | `integer` | Chain where the event occurred. |
| `tx_hash` | `string` | Transaction hash (`0x`-prefixed, 66 characters). |
| `block_number` | `integer` | Block number containing the event. |
| `log_index` | `integer` | Log index within the transaction. |
| `decoded_fields` | `object` | Key-value map of decoded event parameters. Keys are the Solidity parameter names, values are strings (addresses are checksummed, integers are decimal strings). |

#### `data.identity` (AgentIdentity)

Optional. Same schema as payment webhook identity (see [Section 3](#3-payload-schema)). Present when the transaction sender has a registered ERC-8004 identity.

### Example: `trigger.large_swap`

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "idempotency_key": "idem_t8f7e6d5c4b3a29018f7e6d5c4b3a290",
  "type": "trigger.large_swap",
  "mode": "execute",
  "timestamp": 1741738000,
  "data": {
    "trigger": {
      "trigger_id": "trig_abc123def456",
      "trigger_name": "large_swap",
      "contract_address": "0x3fC91A3afd70395Cd496C647d5a6CC9D4B2b7FAD",
      "event_signature": "Swap(address,address,int256,int256,uint160,uint128,int24)"
    },
    "event": {
      "chain_id": 8453,
      "tx_hash": "0x5a6b7c8d9e0f1a2b3c4d5e6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b",
      "block_number": 23457000,
      "log_index": 3,
      "decoded_fields": {
        "sender": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
        "recipient": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        "amount0": "-5000000000",
        "amount1": "2500000000000000000",
        "sqrtPriceX96": "1234567890123456789",
        "liquidity": "9876543210",
        "tick": "-20100"
      }
    },
    "identity": null
  }
}
```

### Idempotency for trigger webhooks

Trigger webhook idempotency keys follow the same pattern as payment webhooks but include the trigger ID:

```
idempotency_key = "idem_" + sha256("{chain_id}:{tx_hash_lower}:{log_index}:{trigger_id}:{endpoint_id}")[:32]
```

---

## 6. Delivery Modes

Each endpoint is registered in exactly one mode. The mode is reflected in every payload's top-level `mode` field.

### Execute mode (`"execute"`)

The default mode. TripWire posts the signed `WebhookPayload` JSON to your registered URL via Convoy. Convoy owns reliability: it tracks each delivery attempt, retries on non-2xx responses, and maintains a dead-letter queue.

- Transport: HTTP POST with `Content-Type: application/json`.
- Authentication: HMAC-SHA256 signature (see [Section 7](#7-hmac-signature-verification)).
- Retry: Exponential backoff, up to 10 attempts (see [Section 8](#8-retry-policy)).
- Dead-letter: Yes (see [Section 9](#9-dead-letter-queue)).
- Expected response: Any `2xx` status code within 30 seconds.

Use Execute mode when your server must take action in response to payment events (fulfill an order, credit an account, trigger a downstream workflow).

### Notify mode (`"notify"`)

A push channel backed by Supabase Realtime. Instead of an HTTP delivery, TripWire inserts a row into the `realtime_events` PostgreSQL table. Supabase Realtime detects the INSERT and broadcasts it over a WebSocket to all subscribed clients in real time.

The row schema mirrors the webhook payload:

```json
{
  "id": "<event-uuid>",
  "endpoint_id": "<your-endpoint-id>",
  "type": "payment.confirmed",
  "data": {
    "transfer": { ... },
    "finality": { ... },
    "identity": { ... },
    "timestamp": 1741737670
  },
  "chain_id": 8453,
  "recipient": "0x70997970c51812dc3a010c7d01b50e0d17dc79c8",
  "created_at": "2026-03-12T00:01:10+00:00"
}
```

Notify-mode endpoints support **subscription filters** to receive only a subset of events:

| Filter | Type | Description |
|---|---|---|
| `chains` | `integer[]` | Only events from these chain IDs. |
| `senders` | `string[]` | Only events from these sender addresses (case-insensitive). |
| `recipients` | `string[]` | Only events to these recipient addresses (case-insensitive). |
| `min_amount` | `string` | Only events with `amount >= min_amount` (base units). |
| `agent_class` | `string` | Only events where the sender's ERC-8004 agent class matches. |

An endpoint with no subscriptions defined receives all events (backwards-compatible behaviour).

HMAC signature verification does not apply to Notify mode because the data is delivered over an authenticated Supabase channel, not an arbitrary HTTP endpoint.

---

## 7. HMAC Signature Verification

Every Execute-mode webhook request carries three TripWire-specific headers:

| Header | Format | Description |
|---|---|---|
| `X-TripWire-Signature` | `t={unix_timestamp},v1={hex_hmac_sha256}` | Primary verification header. |
| `X-TripWire-ID` | UUID string | Unique message ID for this delivery attempt. |
| `X-TripWire-Timestamp` | Unix timestamp string | Same timestamp value embedded in `X-TripWire-Signature`. |

### Signed content

The HMAC-SHA256 digest is computed over:

```
{timestamp}.{raw_request_body}
```

where `timestamp` is the decimal integer from `t=`, `.` is a literal ASCII period, and `raw_request_body` is the exact byte sequence received over the wire (do not parse or re-serialize before verifying).

### Verification algorithm

1. Extract `t=` (timestamp as integer) and all `v1=` (hex HMAC strings) from `X-TripWire-Signature`.
2. Reject if the current Unix time differs from the extracted timestamp by more than **300 seconds** (5 minutes). This prevents replay attacks.
3. Construct the signed content: `timestamp_bytes + b"." + raw_body_bytes` where `timestamp_bytes = str(timestamp).encode("utf-8")`.
4. Compute `HMAC-SHA256(key=secret.encode("utf-8"), msg=signed_content)` and hex-encode the digest.
5. Accept if any provided `v1=` value matches the computed digest using a **constant-time** comparison. Do not use `==`.

### Key rotation

The signature header supports multiple `v1=` entries:

```
X-TripWire-Signature: t=1741737670,v1=<old_sig>,v1=<new_sig>
```

During a secret rotation, TripWire signs with both the old and new key. Accept the delivery if either signature matches. Remove the old key after the rotation window has passed.

### Verification using the TripWire SDK

```python
import os
from tripwire_sdk import verify_webhook_signature, WebhookVerificationError

secret = os.environ["TRIPWIRE_WEBHOOK_SECRET"]

try:
    verify_webhook_signature(raw_body_bytes, request_headers_dict, secret)
except WebhookVerificationError as exc:
    # exc.reason is one of:
    #   "missing_signature_header"
    #   "malformed_signature_header"
    #   "timestamp_too_old"
    #   "signature_mismatch"
    return 400, str(exc)
```

A non-raising variant is also available:

```python
from tripwire_sdk import verify_webhook_signature_safe

if not verify_webhook_signature_safe(raw_body_bytes, request_headers_dict, secret):
    return 400, "invalid signature"
```

### Verification without the SDK (standard library only)

If you cannot install the SDK, implement verification directly with Python's standard library:

```python
import hashlib
import hmac
import time


TOLERANCE_SECONDS = 300


def verify_tripwire_signature(raw_body: bytes, headers: dict, secret: str) -> None:
    """Raise ValueError if verification fails."""
    # Normalize header keys to lowercase.
    norm = {k.lower(): v for k, v in headers.items()}

    sig_header = norm.get("x-tripwire-signature", "")
    if not sig_header:
        raise ValueError("Missing X-TripWire-Signature header")

    # Parse t= and v1= entries.
    timestamp = None
    signatures = []
    for part in sig_header.split(","):
        part = part.strip()
        if part.startswith("t="):
            timestamp = int(part[2:])
        elif part.startswith("v1="):
            sig = part[3:]
            if sig:
                signatures.append(sig)

    if timestamp is None or not signatures:
        raise ValueError("Malformed X-TripWire-Signature header")

    # Timestamp tolerance check.
    if abs(int(time.time()) - timestamp) > TOLERANCE_SECONDS:
        raise ValueError("Webhook timestamp outside tolerance window")

    # Compute expected HMAC.
    signed_content = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(
        secret.encode("utf-8"), signed_content, hashlib.sha256
    ).hexdigest()

    # Constant-time comparison against all provided signatures.
    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise ValueError("Signature mismatch")
```

---

## 8. Retry Policy

TripWire configures Convoy with the following delivery strategy when creating a project for your endpoint:

| Parameter | Value |
|---|---|
| Strategy | Exponential backoff |
| Base duration | 10 seconds |
| Maximum attempts | 10 |

Convoy retries delivery whenever your endpoint returns a non-2xx HTTP status code or the connection times out (30-second request timeout). Each retry interval is approximately `10 * 2^(attempt - 1)` seconds: 10 s, 20 s, 40 s, 80 s, and so on up to attempt 10.

After all 10 attempts are exhausted the delivery is marked `Failed` in Convoy and transitions to the dead-letter queue.

**Your endpoint must return a `2xx` response within 30 seconds.** Do all heavy processing asynchronously after acknowledging receipt. A `200 {"status": "ok"}` response body is sufficient.

---

## 9. Dead Letter Queue

### How deliveries enter the DLQ

A Convoy delivery enters the DLQ (status `Failed`) after exhausting all 10 retry attempts. TripWire's `DLQHandler` background service polls the Convoy API on a configurable interval (`dlq_poll_interval_seconds`) and processes failed deliveries.

### DLQ processing lifecycle

For each failed delivery the `DLQHandler`:

1. Fetches all `Failed` deliveries for the endpoint's Convoy project.
2. If the delivery's DLQ retry count is below `dlq_max_retries`, issues a batch retry via the Convoy API and increments the counter.
3. If the delivery has exceeded `dlq_max_retries`, marks the delivery status as `dead_lettered` in TripWire's local database and fires an alert.

### DLQ alert payload

When a delivery is permanently dead-lettered, TripWire POSTs the following JSON to the URL configured in `dlq_alert_webhook_url`:

```json
{
  "type": "dlq.dead_lettered",
  "endpoint_id": "ep_abc123",
  "delivery_id": "convoy-delivery-uid",
  "event_id": "convoy-event-uid",
  "error": "Delivery convoy-delivery-uid for event convoy-event-uid exceeded 3 retries and has been dead-lettered."
}
```

| Field | Type | Description |
|---|---|---|
| `type` | `string` | Always `"dlq.dead_lettered"`. |
| `endpoint_id` | `string` | TripWire endpoint ID. |
| `delivery_id` | `string` | Convoy event delivery UID. |
| `event_id` | `string` | Convoy event UID. |
| `error` | `string` | Human-readable description including the max retry count. |

### Manual retry

Dead-lettered deliveries can be force-retried via the TripWire API or directly through the Convoy dashboard. The Convoy batch retry endpoint is:

```
POST /api/v1/projects/{project_id}/eventdeliveries/batchretry
{"ids": ["delivery-uid-1", "delivery-uid-2"]}
```

### Configuration

| Setting | Description |
|---|---|
| `dlq_poll_interval_seconds` | How often the DLQ poller runs (e.g., `60`). |
| `dlq_max_retries` | How many DLQ-level retries before permanently dead-lettering a delivery. |
| `dlq_alert_webhook_url` | URL to POST alert payloads to. Leave empty to disable alerts. |

---

## 10. Idempotency

### Idempotency key structure

Every `WebhookPayload` includes an `idempotency_key` that is deterministically derived from the on-chain event:

```
idempotency_key = "idem_" + sha256("{chain_id}:{tx_hash_lower}:{log_index}:{endpoint_id}:{event_type}")[:32]
```

The same combination of `(chain_id, tx_hash, log_index, endpoint_id, event_type)` always produces the same key. This means:

- The same event delivered multiple times (retries, DLQ replays, network duplicates) will carry the identical `idempotency_key`.
- Different event types for the same on-chain transfer produce different keys. For example, a `payment.pending` event and the subsequent `payment.confirmed` event for the same transaction have different `idempotency_key` values.

### Handling duplicates

Your handler will receive duplicate deliveries in normal operation. Implement idempotency at your data store level:

```python
@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.body()
    verify_webhook_signature(body, dict(request.headers), WEBHOOK_SECRET)

    payload = WebhookPayload.model_validate_json(body)

    # Check whether this key was already processed.
    if await db.idempotency_keys.exists(payload.idempotency_key):
        return {"status": "ok"}  # Acknowledge without re-processing.

    await process_payment(payload)

    # Mark as processed atomically with the business logic where possible.
    await db.idempotency_keys.insert(payload.idempotency_key)

    return {"status": "ok"}
```

Recommendations:
- Use a database-level unique constraint on `idempotency_key` and treat a unique violation as a duplicate signal.
- Set a TTL on stored idempotency keys (e.g., 30 days) to bound storage growth.
- For critical operations, make the idempotency check and the business write atomic within a single database transaction.

---

## 11. Endpoint Policies

Policies filter which payments trigger webhooks before Convoy even attempts delivery. They are configured during endpoint registration in the `policies` field.

| Policy field | Type | Default | Description |
|---|---|---|---|
| `min_amount` | `string` | None | Minimum transfer amount (base units). Payments below this are ignored. |
| `max_amount` | `string` | None | Maximum transfer amount. Payments above this are ignored. |
| `allowed_senders` | `string[]` | None | Allowlist of sender addresses. If set, only these addresses can trigger delivery. |
| `blocked_senders` | `string[]` | None | Blocklist of sender addresses. Transfers from these are silently dropped. |
| `required_agent_class` | `string` | None | Only accept payments from agents with this ERC-8004 agent class. |
| `min_reputation_score` | `float` | None | Minimum agent reputation score (0–100). Only applies when the sender has an ERC-8004 identity. |
| `finality_depth` | `integer` | `3` | Block confirmations before sending `payment.confirmed`. Range: 1–64. |

Example endpoint registration with policies:

```json
{
  "url": "https://api.example.com/webhooks/tripwire",
  "mode": "execute",
  "chains": [8453, 1],
  "recipient": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
  "owner_address": "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",
  "policies": {
    "min_amount": "1000000",
    "allowed_senders": [
      "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045"
    ],
    "min_reputation_score": 50.0,
    "finality_depth": 5
  }
}
```

---

## 12. Example Webhook Handler

### Full FastAPI handler with idempotency

```python
"""webhook_handler.py — receive TripWire payment webhooks.

Install dependencies:
    pip install tripwire-sdk fastapi uvicorn

Run:
    TRIPWIRE_WEBHOOK_SECRET=your-secret uvicorn webhook_handler:app
"""

import os
from collections.abc import MutableMapping

from fastapi import FastAPI, HTTPException, Request
from tripwire_sdk import (
    WebhookEventType,
    WebhookPayload,
    WebhookVerificationError,
    verify_webhook_signature,
)

app = FastAPI()

WEBHOOK_SECRET = os.environ["TRIPWIRE_WEBHOOK_SECRET"]

# In production, use a persistent store (database, Redis, etc.).
_processed_keys: set[str] = set()


@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.body()
    headers = dict(request.headers)

    # Step 1: Verify the HMAC signature.
    try:
        verify_webhook_signature(body, headers, WEBHOOK_SECRET)
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=400, detail=f"Signature error: {exc.reason}")

    # Step 2: Parse the payload.
    payload = WebhookPayload.model_validate_json(body)

    # Step 3: Deduplicate by idempotency key.
    if payload.idempotency_key in _processed_keys:
        return {"status": "ok", "duplicate": True}

    # Step 4: Route by event type.
    match payload.type:
        case WebhookEventType.PAYMENT_PRE_CONFIRMED:
            # Fast path: ERC-3009 authorization validated, tx not yet on-chain.
            # Suitable for low-value, latency-sensitive use cases.
            await handle_pre_confirmed(payload)

        case WebhookEventType.PAYMENT_PENDING:
            # On-chain but not yet final. Record for tracking; do not fulfill yet.
            await handle_pending(payload)

        case WebhookEventType.PAYMENT_CONFIRMED:
            # Final. Safe to fulfill orders, credit accounts, etc.
            await handle_confirmed(payload)

        case WebhookEventType.PAYMENT_FAILED:
            await handle_failed(payload)

        case WebhookEventType.PAYMENT_REORGED:
            # Must reverse any action taken on the corresponding confirmed event.
            await handle_reorged(payload)

    # Step 5: Mark as processed.
    _processed_keys.add(payload.idempotency_key)

    return {"status": "ok"}


async def handle_pre_confirmed(payload: WebhookPayload) -> None:
    transfer = payload.data.transfer
    usdc_amount = int(transfer.amount) / 1_000_000
    print(f"Pre-confirmed: {usdc_amount:.6f} USDC from {transfer.from_address}")


async def handle_pending(payload: WebhookPayload) -> None:
    finality = payload.data.finality
    print(
        f"Pending: {finality.confirmations}/{finality.required_confirmations} "
        f"confirmations for {payload.data.transfer.tx_hash}"
    )


async def handle_confirmed(payload: WebhookPayload) -> None:
    transfer = payload.data.transfer
    identity = payload.data.identity
    usdc_amount = int(transfer.amount) / 1_000_000

    print(f"Confirmed: {usdc_amount:.6f} USDC from {transfer.from_address}")
    print(f"  Chain ID : {transfer.chain_id}")
    print(f"  Tx hash  : {transfer.tx_hash}")
    print(f"  Block    : {transfer.block_number}")

    if identity:
        print(f"  Agent    : {identity.agent_class} (score {identity.reputation_score})")

    # Fulfill the order, credit the account, trigger a workflow, etc.


async def handle_failed(payload: WebhookPayload) -> None:
    print(f"Failed: {payload.data.transfer.tx_hash}")


async def handle_reorged(payload: WebhookPayload) -> None:
    print(f"Reorged: {payload.data.transfer.tx_hash} — reversing prior action")
    # Reverse any fulfillment that was triggered by the corresponding confirmed event.
```

### Standard library verification (no SDK)

If you prefer not to install the SDK and want a self-contained verification function:

```python
import hashlib
import hmac
import time


def verify_tripwire_signature(raw_body: bytes, headers: dict, secret: str) -> None:
    """Raise ValueError on any verification failure."""
    norm = {k.lower(): v for k, v in headers.items()}

    sig_header = norm.get("x-tripwire-signature", "")
    if not sig_header:
        raise ValueError("Missing X-TripWire-Signature header")

    timestamp = None
    signatures = []
    for part in sig_header.split(","):
        part = part.strip()
        if part.startswith("t="):
            timestamp = int(part[2:])
        elif part.startswith("v1="):
            sig = part[3:]
            if sig:
                signatures.append(sig)

    if timestamp is None or not signatures:
        raise ValueError("Malformed X-TripWire-Signature header")

    if abs(int(time.time()) - timestamp) > 300:
        raise ValueError("Timestamp outside 5-minute tolerance window")

    signed_content = f"{timestamp}.".encode("utf-8") + raw_body
    expected = hmac.new(
        secret.encode("utf-8"), signed_content, hashlib.sha256
    ).hexdigest()

    if not any(hmac.compare_digest(expected, sig) for sig in signatures):
        raise ValueError("Signature mismatch")
```

---

## 13. Testing Webhooks

### Local development

Start TripWire in dev mode with wallet authentication bypassed:

```bash
python dev_server.py
```

To use a specific test wallet address:

```bash
DEV_WALLET_ADDRESS=0xYourTestAddress python dev_server.py
```

### Generating signed test payloads with the SDK

```python
from tripwire_sdk.verify import sign_payload, verify_webhook_signature
import json

secret = "test-secret-do-not-use-in-production"

payload = json.dumps({
    "id": "test-event-id",
    "idempotency_key": "idem_abc123",
    "type": "payment.confirmed",
    "mode": "execute",
    "timestamp": 1741737670,
    "data": {
        "transfer": {
            "chain_id": 8453,
            "tx_hash": "0x4e3a1b2c5d6f7a8b9c0d1e2f3a4b5c6d7e8f9a0b1c2d3e4f5a6b7c8d9e0f1a2b",
            "block_number": 23456789,
            "from_address": "0xd8dA6BF26964aF9D7eEd9e03E53415D37aA96045",
            "to_address": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
            "amount": "5000000",
            "nonce": "0x9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        },
        "finality": {
            "confirmations": 3,
            "required_confirmations": 3,
            "is_finalized": True
        },
        "identity": None
    }
})

# Generate headers.
headers = sign_payload(payload, secret)
# {
#   "X-TripWire-ID":        "<uuid>",
#   "X-TripWire-Timestamp": "<unix_ts>",
#   "X-TripWire-Signature": "t=<unix_ts>,v1=<hex_hmac>",
# }

# Send to your local handler.
import httpx
response = httpx.post(
    "http://localhost:8000/webhook",
    content=payload,
    headers=headers,
)
print(response.status_code, response.json())

# Verify locally without an HTTP round-trip.
assert verify_webhook_signature(payload, headers, secret) is True
```

---

## 14. Best Practices

### Respond fast, process asynchronously

Your handler has a 30-second window to return a `2xx` response before Convoy times out and schedules a retry. Return `200 {"status": "ok"}` immediately after verifying the signature and enqueuing the work. Run all downstream logic (database writes, external API calls, email sends) in a background task.

### Always verify the signature

Never trust a webhook body without verifying the HMAC-SHA256 signature first. An unverified handler is exploitable by anyone who knows your endpoint URL. Signature verification must happen before any business logic or database access.

### Implement strict idempotency

The same event will be delivered more than once in normal operation (retries, DLQ replays, duplicate ingestion). Use the `idempotency_key` as the deduplication key. Ideally enforce uniqueness with a database constraint so that concurrent duplicate deliveries cannot both succeed.

### React to `payment.confirmed`, not `payment.pending` or `payment.pre_confirmed`

- `payment.pre_confirmed` carries no on-chain confirmation and is suitable only for very low-value, latency-sensitive use cases (e.g., unlocking a preview or starting a short timer).
- `payment.pending` indicates the transaction is on-chain but not yet final.
- `payment.confirmed` is the only event that guarantees the transfer is irreversible at the configured finality depth. Fulfill orders, credit accounts, and trigger workflows only on this event.

### Handle `payment.reorged` defensively

Ethereum and Base can reorg, especially at low finality depths. If you act on a `payment.confirmed` event and later receive `payment.reorged` for the same `idempotency_key`, you must be able to reverse the action. Design state transitions to be reversible for at least the reorg window of each chain.

### Protect your webhook secret

- Store the secret in an environment variable or a secrets manager. Never commit it to source control.
- The secret is returned only once at endpoint registration. Store it immediately and securely.
- If you suspect the secret is compromised, create a new endpoint with a fresh secret and deactivate the old one.

### Set aggressive policies to reduce noise

Use endpoint policies to filter at the TripWire level before delivery:
- Set `min_amount` to ignore dust transfers.
- Set `allowed_senders` if you only accept payments from known agents.
- Set `min_reputation_score` if you want to gate on ERC-8004 agent reputation.

Filtering at the source reduces the volume of events your handler must process and reduces the surface area for abuse.

### Monitor your DLQ

Configure `dlq_alert_webhook_url` to receive alerts when deliveries are permanently dead-lettered. A spike in DLQ events indicates your handler is returning errors or timing out. Investigate before the backlog grows.

### Use Execute mode for transactional workflows, Notify mode for observation

- Execute mode delivers signed HTTP webhooks with full retry guarantees. Use it when payment receipt must trigger reliable, auditable business logic.
- Notify mode delivers real-time events over Supabase WebSocket. Use it for dashboards, monitoring, analytics, or any read-only consumer that does not need HTTP-level delivery guarantees.
