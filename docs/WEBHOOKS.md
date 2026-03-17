# TripWire Webhook Delivery

## Overview

TripWire delivers onchain events to applications via webhooks. Events arrive at TripWire pre-decoded from [Goldsky Turbo](https://goldsky.com) — TripWire does not poll RPC nodes. Goldsky indexes raw logs from Base, Ethereum, and Arbitrum, runs SQL transforms (joining `AuthorizationUsed` and `Transfer` events), and pushes decoded payloads to TripWire's `/ingest` endpoint via webhook. TripWire then processes each event through deduplicate, finality check, identity resolution, and policy evaluation stages before delivering to registered endpoints.

### Unified Event Lifecycle

The facilitator fast path and Goldsky confirmation path produce a **single event**. The consumer sees the same `event_id` progress through three states:

```
Facilitator fires
  -> payment.pre_confirmed  (event_id=X, execution_state=provisional, safe_to_execute=false)

Goldsky confirms the onchain tx
  -> payment.confirmed      (same event_id=X, execution_state=confirmed, real tx_hash)

Finality depth threshold reached
  -> payment.finalized      (same event_id=X, execution_state=finalized, safe_to_execute=true)
```

When the facilitator path is not used (Goldsky-only flow), the event starts at `payment.pending` and is promoted to `payment.confirmed` and then `payment.finalized` by the finality poller.

### Ingest Pipeline

```
Chain (Base / Ethereum / Arbitrum)
  -> Goldsky Turbo (index raw logs + SQL transform)
    -> POST /ingest (decoded payload arrives at TripWire)
      -> Dedup / Finality / Identity / Policy
        -> Convoy (Execute mode) or Supabase Realtime (Notify mode)
          -> Developer endpoint
```

The **facilitator fast path** (`payment.pre_confirmed`) bypasses Goldsky entirely. The x402 facilitator POSTs the ERC-3009 authorization directly to TripWire after verifying the signature, before the transaction is submitted onchain. This path skips Goldsky, decode, and finality stages entirely. When the real transaction later lands onchain, Goldsky delivers it and TripWire promotes the same event to `payment.confirmed` (and subsequently `payment.finalized`).

### Event Types

| Event Type | Description |
|---|---|
| `payment.pre_confirmed` | Facilitator fast-path: ERC-3009 signature verified but tx not yet onchain |
| `payment.confirmed` | Transfer confirmed onchain (block depth meets chain-specific threshold) |
| `payment.finalized` | Transfer has reached the finality depth threshold (per-chain default or per-endpoint override) |
| `payment.pending` | Transfer detected but not yet confirmed |
| `payment.failed` | Transfer processing failed |
| `payment.reorged` | Transfer was reorganized out of the canonical chain |
| `wire.triggered` | Custom event type from the dynamic trigger registry |

---

## Delivery Modes

TripWire supports two delivery modes, configured per-endpoint at registration time.

### Execute Mode (Webhook POST)

Delivers events as signed HTTP POST requests to the endpoint's registered URL. All delivery is managed by [Convoy](https://getconvoy.io), a self-hosted webhook gateway that handles retries, HMAC signing, delivery logging, and dead-letter queuing.

### Notify Mode (Supabase Realtime)

Pushes events via Supabase Realtime WebSocket. Clients subscribe to database changes on the `events` table. Notify-mode endpoints support subscription filters (chains, senders, recipients, min_amount, agent_class) to control which events are forwarded. No webhook URL or secret is required.

---

## Execute Mode Details

All Execute-mode webhook delivery is routed through Convoy. There is no direct HTTP delivery path.

### Architecture

```
EventProcessor
  -> dispatch_event()
    -> ConvoyProvider.send()
      -> Convoy REST API (POST /api/v1/projects/{id}/events)
        -> Convoy worker delivers to endpoint URL with HMAC signature
```

### Convoy Configuration

Each registered Execute-mode endpoint gets its own Convoy project and endpoint:

1. `create_application()` creates a Convoy project with exponential retry strategy (10 retries, 10s base duration).
2. `create_endpoint()` registers the webhook URL with the HMAC signing secret.
3. `send_webhook()` publishes events targeted at the specific endpoint, with an idempotency key to prevent duplicate delivery.

### Convoy Circuit Breaker

TripWire wraps all Convoy API calls with a circuit breaker to avoid cascading failures when Convoy is unavailable.

| Parameter | Value | Description |
|---|---|---|
| Failure threshold | 5 | Consecutive failures before the circuit opens |
| Recovery timeout | 30s | Time the circuit stays open before allowing a probe request |
| Half-open probe timeout | 10s | Shorter HTTP timeout used for probe requests in half-open state |

**State machine**: `closed` (normal) -> `open` (after 5 consecutive failures, fast-fails all requests) -> `half_open` (after 30s, allows one probe request through). A successful probe resets the circuit to `closed`; a failed probe re-opens it.

When the circuit is open, `dispatch_event()` catches the `ConvoyCircuitOpenError` and logs a warning. The event is not lost -- it remains in the events table and the finality poller or DLQ handler will re-attempt delivery.

### Retry Policy

Convoy manages retries with exponential backoff:

- **Strategy**: Exponential backoff
- **Base duration**: 10 seconds
- **Max retries**: 10 (configured at project creation)
- **Total window**: Approximately 17 hours before exhaustion

After all retries are exhausted, the delivery enters the dead-letter queue.

### Process Event Timeout

When the event bus is enabled (`EVENT_BUS_ENABLED=true`), each `process_event()` call in the trigger worker is wrapped in a 30-second `asyncio.wait_for` timeout. If processing hangs (e.g., due to a slow RPC call or database query), the timeout fires and the message is counted as a failure. After 5 failures the message is routed to the Redis Streams DLQ and acknowledged from the source stream.

### Latency

- Standard pipeline: Events arrive pre-decoded from Goldsky Turbo and are processed through dedup, finality, identity, policy, and dispatch stages. Typical end-to-end latency is 20-80ms from event receipt to Convoy API call.
- Facilitator fast path (`payment.pre_confirmed`): Achieves approximately 100ms by bypassing Goldsky entirely and skipping the finality stage. The x402 facilitator POSTs the authorization payload directly to TripWire before the transaction hits the chain. The delivery itself still goes through Convoy at the same speed; the savings come from the shorter pipeline.

### Direct httpx Delivery (Planned)

A direct httpx POST path for latency-critical use cases is planned but **not implemented**. All delivery currently goes through Convoy exclusively.

---

## Webhook Payload Format

All webhook payloads follow a consistent envelope structure.

### Envelope

```json
{
  "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "idempotency_key": "idem_a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4",
  "type": "payment.confirmed",
  "mode": "execute",
  "timestamp": 1710547200,
  "version": "v1",
  "execution_state": "confirmed",
  "safe_to_execute": false,
  "trust_source": "onchain",
  "data": {
    "transfer": { ... },
    "finality": { ... },
    "identity": { ... }
  }
}
```

### Fields

| Field | Type | Description |
|---|---|---|
| `id` | string | Unique webhook delivery ID (UUID v4) |
| `idempotency_key` | string | Deterministic key derived from `(chain_id, tx_hash, log_index, endpoint_id, event_type)`. Use this to deduplicate on the receiver side. |
| `type` | string | One of the event types listed above |
| `mode` | string | `"execute"` or `"notify"` |
| `timestamp` | integer | Unix timestamp of payload creation |
| `version` | string | Payload schema version. Always `"v1"`. |
| `execution_state` | string | Current lifecycle state: `"provisional"`, `"confirmed"`, `"finalized"`, or `"reorged"` |
| `safe_to_execute` | boolean | `true` only when `execution_state` is `"finalized"`. Consumers should gate irreversible side-effects on this flag. |
| `trust_source` | string | `"facilitator"` for pre-confirmed events (off-chain signature verification), `"onchain"` for all others (Goldsky-confirmed). |
| `data` | object | Contains `transfer`, `finality`, and `identity` sub-objects |

#### Execution State Mapping

| Event Type | `execution_state` | `safe_to_execute` | `trust_source` |
|---|---|---|---|
| `payment.pre_confirmed` | `provisional` | `false` | `facilitator` |
| `payment.confirmed` | `confirmed` | `false` | `onchain` |
| `payment.finalized` | `finalized` | `true` | `onchain` |
| `payment.reorged` | `reorged` | `false` | `onchain` |
| `payment.failed` | `reorged` | `false` | `onchain` |

### Transfer Data (`data.transfer`)

```json
{
  "chain_id": 8453,
  "tx_hash": "0xabc123...",
  "block_number": 12345678,
  "from_address": "0x1111111111111111111111111111111111111111",
  "to_address": "0x2222222222222222222222222222222222222222",
  "amount": "1000000",
  "nonce": "0xdeadbeef...",
  "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
}
```

| Field | Type | Description |
|---|---|---|
| `chain_id` | integer | Chain ID (1 = Ethereum, 8453 = Base, 42161 = Arbitrum) |
| `tx_hash` | string | Transaction hash |
| `block_number` | integer | Block number containing the transaction |
| `from_address` | string | Sender address |
| `to_address` | string | Recipient address |
| `amount` | string | Transfer amount in smallest unit (USDC = 6 decimals, so `"1000000"` = 1 USDC) |
| `nonce` | string | ERC-3009 authorization nonce (bytes32 hex) |
| `token` | string | Token contract address (USDC) |

### Finality Data (`data.finality`)

Present when finality information is available. `null` for `payment.pre_confirmed` events (tx not yet onchain).

```json
{
  "confirmations": 12,
  "required_confirmations": 12,
  "is_finalized": true
}
```

| Field | Type | Description |
|---|---|---|
| `confirmations` | integer | Current block confirmations |
| `required_confirmations` | integer | Chain-specific finality depth (Ethereum: 12, Base: 3, Arbitrum: 1) |
| `is_finalized` | boolean | Whether the transaction has reached the required confirmation depth |

### Identity Data (`data.identity`)

Present when the sender has an ERC-8004 onchain agent identity. `null` if no identity is registered.

`identity` is `null` when the sender address has no ERC-8004 registration in the onchain agent registry. In that case, identity-based policies (`required_agent_class`, `min_reputation_score`) will cause the endpoint to be skipped.

```json
{
  "address": "0x1234...",
  "agent_class": "trading-bot",
  "deployer": "0xABCD...",
  "capabilities": ["swap", "limit-order", "portfolio-rebalance"],
  "reputation_score": 85.0,
  "registered_at": 42,
  "metadata": {"agent_id": 42, "agent_uri": "ipfs://..."}
}
```

| Field | Type | Description |
|---|---|---|
| `address` | string | The agent's wallet address (checksummed hex) |
| `agent_class` | string | Classification from the ERC-8004 registry (e.g., `"trading-bot"`, `"data-oracle"`, `"payment_agent"`). Set by the deployer at registration time. |
| `deployer` | string | The address that deployed or owns the agent contract — i.e., who registered the agent onchain. |
| `capabilities` | list[string] | Self-declared capabilities stored onchain (e.g., `["swap", "limit-order", "portfolio-rebalance"]`). Set by the deployer; not verified by TripWire. |
| `reputation_score` | float | 0–100 reputation score. Derived from the onchain 0–10000 basis-point value (divided by 100). |
| `registered_at` | integer | The agent's token ID (sequential NFT ID from the ERC-8004 registry). Lower values indicate earlier registration. |
| `metadata` | object | Raw metadata from the registry: `agent_id` (same as `registered_at`) and `agent_uri` (tokenURI, typically an IPFS link to off-chain agent metadata). |

### Example: `payment.pre_confirmed`

```json
{
  "id": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
  "idempotency_key": "idem_2c624232cdd221771294dfbb310aca00",
  "type": "payment.pre_confirmed",
  "mode": "execute",
  "timestamp": 1710547180,
  "version": "v1",
  "execution_state": "provisional",
  "safe_to_execute": false,
  "trust_source": "facilitator",
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0x000...pending",
      "block_number": 0,
      "from_address": "0x1111111111111111111111111111111111111111",
      "to_address": "0x2222222222222222222222222222222222222222",
      "amount": "5000000",
      "nonce": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": null,
    "identity": null
  }
}
```

### Example: `payment.confirmed`

```json
{
  "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
  "idempotency_key": "idem_8f14e45fceea167a5a36dedd4bea2543",
  "type": "payment.confirmed",
  "mode": "execute",
  "timestamp": 1710547200,
  "version": "v1",
  "execution_state": "confirmed",
  "safe_to_execute": false,
  "trust_source": "onchain",
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0xabc123def456...",
      "block_number": 12345678,
      "from_address": "0x1111111111111111111111111111111111111111",
      "to_address": "0x2222222222222222222222222222222222222222",
      "amount": "5000000",
      "nonce": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": {
      "confirmations": 3,
      "required_confirmations": 3,
      "is_finalized": false
    },
    "identity": {
      "address": "0x1111111111111111111111111111111111111111",
      "agent_class": "payment_agent",
      "deployer": "0x3333333333333333333333333333333333333333",
      "capabilities": ["transfer"],
      "reputation_score": 92.0,
      "registered_at": 1706547200,
      "metadata": {}
    }
  }
}
```

### Example: `payment.finalized`

```json
{
  "id": "d4e5f6a7-b8c9-0123-defg-456789abcdef",
  "idempotency_key": "idem_9a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d",
  "type": "payment.finalized",
  "mode": "execute",
  "timestamp": 1710547260,
  "version": "v1",
  "execution_state": "finalized",
  "safe_to_execute": true,
  "trust_source": "onchain",
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0xabc123def456...",
      "block_number": 12345678,
      "from_address": "0x1111111111111111111111111111111111111111",
      "to_address": "0x2222222222222222222222222222222222222222",
      "amount": "5000000",
      "nonce": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": {
      "confirmations": 12,
      "required_confirmations": 12,
      "is_finalized": true
    },
    "identity": {
      "address": "0x1111111111111111111111111111111111111111",
      "agent_class": "payment_agent",
      "deployer": "0x3333333333333333333333333333333333333333",
      "capabilities": ["transfer"],
      "reputation_score": 92.0,
      "registered_at": 1706547200,
      "metadata": {}
    }
  }
}
```

### Dynamic Trigger Payload (`wire.triggered`)

Dynamic trigger events use a slightly different payload structure. The `data` field contains the ABI-decoded event fields directly, and includes a `trigger_id`.

#### Reputation Gating

Dynamic triggers that have `reputation_threshold > 0` will filter events based on the sender's ERC-8004 reputation score. After identity resolution, if the resolved agent's `reputation_score` is below the trigger's `reputation_threshold`, the event is silently dropped and no webhook is dispatched. If the sender has no ERC-8004 identity (`data.identity` is `null`), the event is also dropped. This allows trigger creators to restrict webhooks to high-reputation agents without additional endpoint-level policy configuration.

#### Payload Structure

```json
{
  "id": "b2c3d4e5-f6a7-8901-bcde-f12345678901",
  "idempotency_key": "dyn_0xabc123_42_trigger_abc123",
  "type": "wire.triggered",
  "mode": "execute",
  "timestamp": 1710547200,
  "trigger_id": "trigger_abc123",
  "data": {
    "from": "0x1111111111111111111111111111111111111111",
    "to": "0x2222222222222222222222222222222222222222",
    "value": 1000000,
    "_chain_id": 8453,
    "_tx_hash": "0xabc123...",
    "_block_number": 12345678,
    "_log_index": 42
  }
}
```

---

## Signature Verification

All Execute-mode webhook deliveries are signed with HMAC-SHA256 by Convoy using the endpoint's secret.

### Headers

| Header | Description |
|---|---|
| `X-TripWire-Signature` | `t={unix_timestamp},v1={hex_hmac_sha256}` |
| `X-TripWire-ID` | Unique message ID |
| `X-TripWire-Timestamp` | Unix timestamp (same value as the `t=` in the signature header) |

### Signature Scheme

The signed content is constructed as:

```
{timestamp}.{raw_request_body}
```

The HMAC-SHA256 is computed over this byte string, keyed by the endpoint's webhook secret (UTF-8 encoded).

### Verification Algorithm

1. Extract the `X-TripWire-Signature` header.
2. Parse the timestamp (`t=`) and signature(s) (`v1=`).
3. Check that the timestamp is within the **5-minute tolerance window** (300 seconds). Reject if the age exceeds this.
4. Construct the signed content: `{timestamp}.{raw_body}`.
5. Compute `HMAC-SHA256(secret, signed_content)` and hex-encode the result.
6. Compare the computed signature against each `v1=` value using constant-time comparison.
7. Accept if any `v1=` signature matches.

### Key Rotation

The signature header supports multiple `v1=` entries:

```
X-TripWire-Signature: t=1710547200,v1=oldkeysig...,v1=newkeysig...
```

During a key rotation, Convoy signs with both the old and new keys. The verifier accepts the payload if either signature matches. This allows zero-downtime key rotation.

---

## SDK Verification

The `tripwire-sdk` Python package provides signature verification out of the box.

### Installation

```bash
pip install tripwire-sdk
```

### Raising on Failure

```python
import os
from fastapi import Request, HTTPException
from tripwire_sdk.verify import verify_webhook_signature, WebhookVerificationError

@app.post("/webhook")
async def handle_webhook(request: Request):
    payload = await request.body()
    headers = dict(request.headers)
    secret = os.environ["TRIPWIRE_WEBHOOK_SECRET"]

    try:
        verify_webhook_signature(payload, headers, secret)
    except WebhookVerificationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Signature valid -- process the event
    data = await request.json()
    print(f"Received {data['type']} event: {data['id']}")
```

### Boolean Check (No Exception)

```python
from tripwire_sdk.verify import verify_webhook_signature_safe

@app.post("/webhook")
async def handle_webhook(request: Request):
    payload = await request.body()
    headers = dict(request.headers)
    secret = os.environ["TRIPWIRE_WEBHOOK_SECRET"]

    if not verify_webhook_signature_safe(payload, headers, secret):
        raise HTTPException(status_code=400, detail="Invalid signature")

    data = await request.json()
    # ...
```

### Generating Test Signatures

For local development and testing, `sign_payload` produces valid TripWire signature headers without a running server:

```python
from tripwire_sdk.verify import sign_payload

payload = '{"type":"payment.confirmed","data":{...}}'
secret = "test_secret_hex"
headers = sign_payload(payload, secret)
# Returns: {"X-TripWire-ID": "...", "X-TripWire-Timestamp": "...", "X-TripWire-Signature": "t=...,v1=..."}
```

### WebhookVerificationError

The exception carries a machine-readable `reason` attribute:

| Reason | Description |
|---|---|
| `missing_signature_header` | `X-TripWire-Signature` header not present |
| `malformed_signature_header` | Header cannot be parsed (missing `t=` or `v1=`) |
| `timestamp_too_old` | Timestamp is outside the 5-minute tolerance window |
| `signature_mismatch` | HMAC does not match any provided `v1=` signature |

---

## Secret Lifecycle

The webhook signing secret follows a strict one-time disclosure policy:

1. **Generation**: When an Execute-mode endpoint is registered, TripWire generates a 32-byte random hex secret via `secrets.token_hex(32)`.
2. **Passed to Convoy**: The secret is sent to Convoy's `create_endpoint` API so Convoy can sign outgoing deliveries.
3. **Returned to developer**: The secret is included in the registration response body (`webhook_secret` field) exactly once.
4. **Not stored in TripWire DB**: The `webhook_secret` column was dropped from the endpoints table (migration `016_webhook_secret_drop.sql`). Convoy is the sole holder of the signing secret.
5. **Unrecoverable**: If the developer loses the secret, there is no way to retrieve it. A new endpoint must be registered to obtain a new secret.

**Save the `webhook_secret` from the registration response immediately.** It will not appear in any subsequent API call.

---

## Endpoint Matching

When an onchain event is processed, TripWire matches it against registered endpoints.

### Data Source and ABI Decoding

For standard ERC-3009 payment events, Goldsky Turbo handles decoding via its SQL transform pipeline and delivers fully structured payloads to `/ingest`. TripWire consumes these directly without additional ABI decoding.

For **dynamic trigger events** (`wire.triggered`), Goldsky delivers the raw log (topics + data bytes) to `/ingest`. TripWire performs ABI decoding locally using `eth-abi`, based on the ABI registered with the trigger. Goldsky's SQL transforms only cover the built-in ERC-3009 event set.

Internally, decoding is handled by a unified decoder abstraction: `ERC3009Decoder` wraps the existing `decode_transfer_event` for standard payment events, and `AbiGenericDecoder` wraps `decode_event_with_abi` for dynamic trigger events, producing a `DecodedEvent` envelope. This is an internal refactor -- the webhook payload format is unchanged.

### Match Criteria

An endpoint matches a transfer if all three conditions are met:

1. **Recipient**: The endpoint's `recipient` field matches the transfer's `to_address` (case-insensitive).
2. **Chain**: The transfer's `chain_id` is in the endpoint's `chains` list.
3. **Active**: The endpoint's `active` flag is `true`.

### Policy Evaluation

After matching, each endpoint's policies are evaluated. The transfer is only dispatched to endpoints that pass all policy checks:

| Policy | Description |
|---|---|
| `min_amount` | Transfer amount must be >= this value (in smallest unit) |
| `max_amount` | Transfer amount must be <= this value |
| `allowed_senders` | Transfer sender must be in this list |
| `blocked_senders` | Transfer sender must NOT be in this list |
| `required_agent_class` | Sender's ERC-8004 identity `agent_class` must match |
| `min_reputation_score` | Sender's ERC-8004 reputation score must be >= this value |
| `finality_depth` | Required confirmation depth before this endpoint receives webhooks (range: 1-64). See dispatch-time gate below. |

Endpoints that fail policy evaluation are logged and skipped. The event is still recorded.

#### Identity-Based Policy Evaluation

`required_agent_class` and `min_reputation_score` operate on the `data.identity` object resolved during event processing:

- **`required_agent_class`**: If set, the event is only delivered if `data.identity.agent_class` exactly matches the configured value. If `data.identity` is `null` (sender has no ERC-8004 registration), the check fails and the endpoint is skipped.
- **`min_reputation_score`**: If set, the event is only delivered if `data.identity.reputation_score` is greater than or equal to the configured threshold (0–100 float). If `data.identity` is `null`, the check fails and the endpoint is skipped.

Both policies can be combined. For example, an endpoint configured with `required_agent_class: "trading-bot"` and `min_reputation_score: 75.0` will only receive webhooks from registered trading-bot agents with a reputation score of at least 75.

### Finality Depth as a Dispatch-Time Gate

`EndpointPolicies.finality_depth` acts as a dispatch-time gate, not just a filter. When an event is processed, TripWire compares the current block confirmation count against each endpoint's configured `finality_depth`. If the endpoint's threshold has not been reached yet, that endpoint is **deferred** -- it will not receive the webhook on this pass. The finality poller picks up the event on a subsequent poll cycle once enough confirmations have accumulated.

This means different endpoints can receive webhooks for the same event at different times:

- Endpoint A (`finality_depth: 1`) receives `payment.confirmed` almost immediately.
- Endpoint B (`finality_depth: 12`) receives nothing until 12 confirmations are reached, then gets `payment.finalized`.

If no `finality_depth` is configured, the chain default applies (Ethereum: 12, Base: 3, Arbitrum: 1).

---

## Subscription Filtering

Notify-mode endpoints support subscription-based filtering. Subscriptions are created per-endpoint and control which events are pushed via Supabase Realtime.

### Filter Fields

All filters are optional. If a filter is not set, it matches all values. If multiple filters are set, all must pass (AND logic).

| Filter | Type | Description |
|---|---|---|
| `chains` | `list[int]` | Transfer chain_id must be in this list |
| `senders` | `list[string]` | Transfer from_address must be in this list (case-insensitive) |
| `recipients` | `list[string]` | Transfer to_address must be in this list (case-insensitive) |
| `min_amount` | `string` | Transfer amount must be >= this value |
| `agent_class` | `string` | Sender's ERC-8004 identity agent_class must match |

If an endpoint has no subscriptions defined, it receives all events (backwards-compatible behavior).

---

## Idempotency

Every webhook delivery includes a deterministic `idempotency_key` for receiver-side deduplication.

### Key Format

For standard payment events:

```
idem_{sha256(chain_id:tx_hash:log_index:endpoint_id:event_type)[:32]}
```

For dynamic trigger events:

```
dyn_{tx_hash}_{log_index}_{trigger_id}
```

### Usage

Receivers should store the `idempotency_key` and reject payloads with keys they have already processed. This protects against at-least-once delivery semantics (Convoy may redeliver on timeout or retry).

Additionally, TripWire passes the `idempotency_key` to Convoy as the event-level idempotency key, preventing duplicate events from being created in Convoy itself.

---

## Dead Letter Queue

TripWire has two DLQ layers: a **Convoy DLQ** for webhook delivery failures, and a **Redis Streams DLQ** for event processing failures (when `EVENT_BUS_ENABLED=true`).

### Convoy DLQ Handler

The `DLQHandler` runs as a background asyncio task, monitoring Convoy for failed deliveries and managing retries beyond Convoy's built-in retry policy.

#### How It Works

1. **Polling**: Polls Convoy for failed deliveries on a configurable interval (`DLQ_POLL_INTERVAL_SECONDS`).
2. **Per-endpoint scan**: For each active endpoint with a `convoy_project_id`, fetches failed deliveries from Convoy's event deliveries API (`status=Failed`).
3. **Persistent retry counts**: The `dlq_retry_count` column on the `webhook_deliveries` table (migration `021_dlq_retry_count.sql`) tracks how many DLQ-level retries each delivery has received. Counts survive process restarts.
4. **Batch retry**: Deliveries that have not exceeded the DLQ retry limit are retried via Convoy's batch retry endpoint (`force_resend`). The `dlq_retry_count` is incremented in the database on each attempt.
5. **Dead-letter**: Deliveries that exceed `DLQ_MAX_RETRIES` are marked as `dead_lettered` in TripWire's local `webhook_deliveries` table.
6. **Alert**: When a delivery is dead-lettered, an alert payload is POSTed to the configured `DLQ_ALERT_WEBHOOK_URL` (if set).

#### Alert Payload

```json
{
  "type": "dlq.dead_lettered",
  "endpoint_id": "ep_abc123",
  "delivery_id": "conv_delivery_456",
  "event_id": "conv_event_789",
  "error": "Delivery conv_delivery_456 for event conv_event_789 exceeded 3 retries and has been dead-lettered."
}
```

### Redis Streams DLQ Consumer

When `EVENT_BUS_ENABLED=true`, the trigger worker writes permanently-failed events (after 5 processing attempts) to the `tripwire:dlq` Redis stream. The `RedisDLQConsumer` is a background task that reads from this stream, logs the failures, increments a Prometheus counter (`tripwire_redis_dlq_total`), and fires alert webhooks.

#### Redis DLQ Alert Payload

```json
{
  "type": "redis_dlq.event_dead_lettered",
  "dlq_message_id": "1710547200000-0",
  "source_stream": "tripwire:events:0xabcdef...",
  "source_message_id": "1710547100000-0",
  "error_count": 5,
  "timestamp": 1710547200.0
}
```

The consumer polls every 30 seconds, processes messages in batches of 50, and periodically trims the DLQ stream to prevent unbounded growth (capped at 10,000 entries).

### Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `DLQ_POLL_INTERVAL_SECONDS` | 60 | How often (seconds) the Convoy DLQ handler polls |
| `DLQ_MAX_RETRIES` | 3 | Maximum DLQ-level retries before dead-lettering |
| `DLQ_ALERT_WEBHOOK_URL` | (empty) | URL to receive dead-letter alert POSTs (shared by both Convoy and Redis DLQ) |

### Observability

- **Metric**: `tripwire_dlq_backlog` gauge tracks the total number of failed Convoy deliveries across all endpoints.
- **Metric**: `tripwire_errors_total{error_type="dead_lettered"}` counter increments on each Convoy dead-letter event.
- **Metric**: `tripwire_redis_dlq_total` counter increments on each Redis Streams dead-letter event consumed.
- **Health check**: Both the `dlq_handler` and `redis_dlq_consumer` register with the health registry and report their last successful poll.

---

## Delivery Tracking API

All delivery tracking endpoints require wallet authentication. Only deliveries belonging to the authenticated wallet's endpoints are accessible.

### List Deliveries

```
GET /deliveries?endpoint_id={id}&event_id={id}&status={status}&cursor={cursor}&limit={n}
```

Returns a paginated list of deliveries. Supports cursor-based pagination (pass the last delivery's `id` as `cursor`).

**Query parameters** (all optional):

| Parameter | Description |
|---|---|
| `endpoint_id` | Filter by endpoint |
| `event_id` | Filter by event |
| `status` | Filter by status (`sent`, `delivered`, `failed`, `pending`, `dead_lettered`) |
| `cursor` | Delivery ID for cursor-based pagination |
| `limit` | Results per page (1-200, default 50) |

**Response:**

```json
{
  "data": [
    {
      "id": "del_abc123",
      "endpoint_id": "ep_xyz",
      "event_id": "evt_456",
      "provider_message_id": "conv_msg_789",
      "status": "sent",
      "execution_state": "confirmed",
      "safe_to_execute": false,
      "created_at": "2026-03-16T00:00:00Z"
    }
  ],
  "cursor": "del_abc123",
  "has_more": true
}
```

### Get Single Delivery

```
GET /deliveries/{delivery_id}
```

Returns a single delivery record. The response includes `execution_state` and `safe_to_execute` fields derived from the parent event's status, allowing consumers to check execution safety without a separate event lookup.

### List Deliveries by Endpoint

```
GET /endpoints/{endpoint_id}/deliveries?status={status}&cursor={cursor}&limit={n}
```

Same response format as the global list, scoped to a single endpoint. All delivery list and detail responses include `execution_state` and `safe_to_execute` derived from the parent event.

### Delivery Stats

```
GET /endpoints/{endpoint_id}/deliveries/stats
```

Returns aggregated delivery statistics for an endpoint.

**Response:**

```json
{
  "endpoint_id": "ep_xyz",
  "total": 1000,
  "pending": 5,
  "sent": 900,
  "delivered": 880,
  "failed": 15,
  "success_rate": 0.98
}
```

### Retry a Delivery

```
POST /deliveries/{delivery_id}/retry
```

Retries a failed delivery via Convoy. Returns `202 Accepted`.

**Constraints:**
- Only deliveries with status `failed` can be retried.
- The parent endpoint must have a `convoy_project_id` configured.
- The delivery must have a `provider_message_id` for Convoy to identify the delivery attempt.

**Response:**

```json
{
  "detail": "Retry requested",
  "delivery_id": "del_abc123"
}
```
