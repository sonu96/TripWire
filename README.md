# TripWire

**Programmable onchain event triggers for AI agents.**

> Goldsky tells you what happened. TripWire decides if it's safe to act. Convoy makes sure you hear about it.

Turn onchain events into safe execution signals -- instantly or after finality. TripWire is the infrastructure layer between onchain events and application execution: it indexes, verifies, enriches, and delivers so your application never has to poll a chain or parse a log.

TripWire runs as a **dual-product platform**:

- **Pulse** -- generic onchain event triggers. Monitor any EVM event (ERC-20 transfers, DeFi swaps, NFT mints) across Base, Ethereum, and Arbitrum. Events progress through `trigger.matched` -> `trigger.confirmed` -> `trigger.finalized`.
- **Keeper** -- x402 payment webhooks. Purpose-built for ERC-3009 `transferWithAuthorization` micropayments with a ~100ms facilitator fast path. Supports **sessions** -- pre-authorized spending limits so agents can make multiple MCP tool calls without per-call x402 payment negotiation.

Deploy as Pulse-only, Keeper-only, or both (default) via the `PRODUCT_MODE` environment variable.

---

## What TripWire Does

- **Indexes onchain events via Goldsky Turbo** across Base, Ethereum, and Arbitrum. Any EVM event -- ERC-3009 payments, ERC-20 transfers, DeFi swaps, NFT mints -- can be a trigger.
- **Unified processing pipeline.** A single code path decodes, deduplicates, checks finality, resolves ERC-8004 identity, evaluates policies, and gates on payment metadata -- for both built-in ERC-3009 events and dynamic triggers created via MCP or API.
- **Per-trigger payment gating.** Triggers can require that the decoded event contains a payment meeting a minimum threshold before dispatch proceeds. Gate on token contract, minimum amount, or both.
- **Keeper sessions.** Pre-authorized spending limits stored in Redis with atomic Lua-script budget decrements. Agents open a session once, then make multiple MCP tool calls using the `X-TripWire-Session` header instead of per-call x402 payments. Feature-flagged via `SESSION_ENABLED`.
- **Delivers signed webhooks via Convoy** with at-least-once guarantees. HMAC-signed payloads, exponential backoff retries (10 attempts), and a dead-letter queue ensure nothing is silently dropped.

---

## Execution Modes

TripWire's core differentiator: the same event progresses through execution states, and your application decides when to act based on the `safe_to_execute` flag.

```
Path                         Latency     execution_state    safe_to_execute
───────────────────────────  ──────────  ─────────────────  ───────────────
Instant (x402 fast path)     ~100ms      provisional        false
Finalized (Goldsky path)     ~1-13s      finalized          true
```

A single `event_id` progresses through: `pre_confirmed` -> `confirmed` -> `finalized`

Your webhook receives an update at each stage. Show a spinner on `provisional`. Commit the transaction on `finalized`. Roll back on `reorged`.

---

## Architecture

```
L0  Chain             Base / Ethereum / Arbitrum (ERC-3009, any EVM event)
     |
L1  Goldsky Turbo     Indexes events, delivers raw logs via webhook to /ingest
     |
L2  TripWire Engine   Decode -> Filter -> Payment Gate -> Dedup -> Finality ∥ Identity -> Policy
     |
L3  Convoy Delivery   HMAC-signed webhooks, 10x retries, backoff, DLQ
     |
L4  Your App          Receives verified payload, executes business logic
     |
L5  MCP Server        AI agent interface -- 8 tools for trigger CRUD via JSON-RPC
```

Two ingestion paths feed the same pipeline:

| Path               | Latency | Source                                  |
|--------------------|---------|-----------------------------------------|
| Goldsky Turbo      | 2-4s    | Webhook sink from indexed chain data    |
| x402 Facilitator   | ~100ms  | Pre-settlement hook (fast path)         |

Two delivery modes:

| Mode        | Transport                | Guarantees                              |
|-------------|--------------------------|------------------------------------------|
| **Execute** | Convoy webhook POST      | HMAC-signed, 10x retry, DLQ, delivery logs |
| **Notify**  | Supabase Realtime push   | Lightweight event stream for dashboards  |

---

## Quick Start

```bash
git clone https://github.com/your-org/tripwire.git
cd tripwire
cp .env.example .env          # Fill in Supabase + Convoy credentials
pip install -e ".[dev]"
python dev_server.py           # Starts on port 3402 with auth bypass
```

Verify the server is running:

```bash
curl http://localhost:3402/health
# {"status": "ok", "service": "tripwire", "version": "1.0.0"}
```

The dev server bypasses wallet authentication and uses Hardhat account #0 (`0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266`) so you can test endpoints without signing SIWE messages.

For production:

```bash
APP_ENV=production python -m tripwire.main
```

---

## MCP Server

TripWire exposes 8 tools via the [Model Context Protocol](https://modelcontextprotocol.io/) at `/mcp`, enabling AI agents to manage triggers programmatically over JSON-RPC 2.0.

### 3-Tier Authentication

| Tier     | Mechanism              | Tools                                                                          |
|----------|------------------------|--------------------------------------------------------------------------------|
| PUBLIC   | None                   | `initialize`, `tools/list`                                                     |
| SIWX     | Wallet signature       | `list_triggers`, `delete_trigger`, `list_templates`, `get_trigger_status`, `search_events` |
| X402     | Per-call micropayment  | `register_middleware` ($0.003), `create_trigger` ($0.003), `activate_template` ($0.001) |

### All 8 Tools

| Tool                  | Auth | Description                                                       |
|-----------------------|------|-------------------------------------------------------------------|
| `register_middleware` | X402 | Register TripWire as middleware. Creates endpoint + triggers.     |
| `create_trigger`      | X402 | Create a custom trigger for any event signature.                  |
| `list_triggers`       | SIWX | List your active triggers.                                        |
| `delete_trigger`      | SIWX | Deactivate a trigger (soft delete).                               |
| `list_templates`      | SIWX | Browse trigger templates from the Bazaar.                         |
| `activate_template`   | X402 | Instantiate a Bazaar template with custom params.                 |
| `get_trigger_status`  | SIWX | Check trigger health and recent event count.                      |
| `search_events`       | SIWX | Query recent events for your endpoints.                           |

### Example: Register Middleware

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
      "template_slugs": ["x402-payment"],
      "custom_triggers": [
        {
          "event_signature": "Transfer(address,address,uint256)",
          "name": "Large USDC transfers",
          "contract_address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
          "filter_rules": [{"field": "value", "op": "gte", "value": "1000000000"}]
        }
      ]
    }
  }
}
```

Returns `endpoint_id`, `webhook_secret`, `trigger_ids`, `mode`, and `url` in a single call.

---

## Webhook Payload

Every delivery follows the v1 payload schema. The `execution_state` and `safe_to_execute` fields are the signals your application logic should branch on.

```json
{
  "id": "evt_a1b2c3d4e5f6",
  "idempotency_key": "sha256:8453:0x9f86d081...b0f00a08:7",
  "type": "payment.finalized",
  "mode": "execute",
  "timestamp": 1710700800,
  "version": "v1",
  "execution": {
    "state": "finalized",
    "safe_to_execute": true,
    "trust_source": "onchain",
    "finality": {
      "confirmations": 3,
      "required_confirmations": 3,
      "is_finalized": true
    }
  },
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0x9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
      "block_number": 28451023,
      "from_address": "0x1234567890abcdef1234567890abcdef12345678",
      "to_address": "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd",
      "amount": "5000000",
      "nonce": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "identity": {
      "address": "0x1234567890abcdef1234567890abcdef12345678",
      "agent_class": "payment-agent",
      "deployer": "0xdeadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
      "capabilities": ["x402-payment", "erc3009-transfer"],
      "reputation_score": 87.5,
      "registered_at": 1706572800,
      "metadata": {}
    }
  }
}
```

**Key fields:**

| Field             | Values                                                | Meaning                                     |
|-------------------|-------------------------------------------------------|----------------------------------------------|
| `execution.state` | `provisional`, `confirmed`, `finalized`, `reorged`    | How far the event has progressed             |
| `execution.safe_to_execute` | `true` / `false`                             | `true` only after onchain finality confirmed |
| `execution.trust_source`    | `facilitator` or `onchain`                   | Who vouches for this state                   |
| `idempotency_key` | Deterministic SHA-256                                 | Safe to deduplicate on your end              |

**Event types:** `payment.pre_confirmed`, `payment.confirmed`, `payment.finalized`, `payment.failed`, `payment.reorged`

---

## Execution Guarantees

- **Nonce deduplication** -- PostgreSQL `UNIQUE` constraint on `(chain_id, nonce, authorizer)` with atomic upsert. The same transfer cannot be processed twice.
- **Idempotency keys** -- Deterministic SHA-256 derived from chain ID, tx hash, and log index. Safe to retry on your end.
- **At-least-once delivery** -- Convoy retries up to 10 times with exponential backoff. Permanently failed deliveries route to a dead-letter queue with alerting.
- **Finality verified per-chain** -- Ethereum: 12 blocks, Base: 3 blocks, Arbitrum: 1 block. Configurable per-endpoint via `policies.finality_depth`.
- **Reorg detection** -- The finality poller detects block reorganizations and emits `payment.reorged` events so your application can roll back provisional actions.
- **Circuit breaker on Convoy** -- Webhook delivery fails fast on unresponsive endpoints and auto-recovers, preventing cascade failures.
- **Payment gating** -- Triggers can require minimum payment amounts and specific tokens before dispatch. Gating runs before deduplication so rejected events don't consume nonces.

---

## Documentation

| Document                                   | Description                                      |
|--------------------------------------------|--------------------------------------------------|
| [Architecture](docs/ARCHITECTURE.md)       | System design, layer breakdown, data flow        |
| [API Reference](docs/API-REFERENCE.md)     | REST endpoint specifications                     |
| [Webhooks](docs/WEBHOOKS.md)               | Payload formats and delivery semantics           |
| [MCP Server](docs/MCP-SERVER.md)           | MCP tools, authentication tiers, and pricing     |
| [Security](docs/SECURITY.md)               | Auth model, HMAC signing, threat model           |
| [Operations](docs/OPERATIONS.md)           | Deployment, monitoring, DLQ handling             |
| [Development](docs/DEVELOPMENT.md)         | Local setup, testing, contribution guidelines    |
| [Skill Spec (TWSS-1)](docs/SKILL-SPEC.md)  | Execution-aware skill standard                   |
| [Team Update](docs/TEAM-UPDATE.md)         | Latest sprint summary and known issues           |

---

## Decoder Architecture (Phases C1-C3)

TripWire uses a unified decoder abstraction to process all event types through a single pipeline:

| Phase | Status | Description |
|-------|--------|-------------|
| C1 | Done | `Decoder` protocol + `DecodedEvent` envelope + `ERC3009Decoder` + `AbiGenericDecoder` |
| C2 | Done | Unified processing loop -- single code path for ERC-3009 and dynamic triggers (feature-flagged via `UNIFIED_PROCESSOR`) |
| C3 | Done | Per-trigger payment gating via decoder metadata (`require_payment`, `payment_token`, `min_payment_amount`) |

When `UNIFIED_PROCESSOR=true`, dynamic triggers gain finality checking, full policy evaluation, execution state metadata, notify mode, tracing, and metrics -- all features previously exclusive to the ERC-3009 path.

---

## Tech Stack

| Component            | Technology                                              |
|----------------------|---------------------------------------------------------|
| Runtime              | Python 3.11+                                            |
| API Framework        | FastAPI + Uvicorn                                       |
| Database             | Supabase (managed PostgreSQL)                           |
| Webhook Delivery     | Convoy (self-hosted) + direct httpx fast path           |
| Blockchain Indexing  | Goldsky Turbo                                           |
| Event Bus            | Redis Streams (optional, for horizontal scaling)        |
| RPC                  | httpx (raw JSON-RPC calls, no web3.py)                  |
| ABI Decoding         | eth-abi                                                 |
| Validation           | Pydantic v2                                             |
| Logging              | structlog (structured JSON)                             |
| Metrics              | Prometheus                                              |
| Tracing              | OpenTelemetry (optional)                                |
| Error Tracking       | Sentry (optional)                                       |
| Agent Identity       | ERC-8004 onchain registry                               |
| Payments             | x402 protocol (ERC-3009 transferWithAuthorization)      |

---

## License

Proprietary. All rights reserved. See [LICENSE](LICENSE) for details.
