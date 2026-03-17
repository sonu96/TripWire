# TripWire Architecture

> "Stripe Webhooks for x402" -- the infrastructure layer between onchain micropayments and application execution.

TripWire is a programmable onchain event trigger platform that watches ERC-3009 `transferWithAuthorization` payments on EVM chains, verifies finality, resolves onchain AI agent identities via ERC-8004, evaluates developer-defined policies, and delivers structured webhook payloads through Convoy self-hosted. x402 payments are the first use case, with the architecture designed to support arbitrary onchain event types.

---

> **See also:** [ARCHITECTURE_DETAILED.md](./ARCHITECTURE_DETAILED.md) for the full hybrid architecture with latency maps, real-world examples (x402 payments, Aerodrome APR alerts, whale alerts), and the complete event trigger platform vision.

## Table of Contents

1. [System Overview](#system-overview)
2. [Architecture Layers](#architecture-layers)
3. [Event Lifecycle](#event-lifecycle)
4. [Goldsky Pipeline](#goldsky-pipeline)
5. [ERC-3009 Event Processing](#erc-3009-event-processing)
6. [Finality Tracking](#finality-tracking)
7. [ERC-8004 Identity Resolution](#erc-8004-identity-resolution)
8. [Webhook Delivery](#webhook-delivery)
9. [Policy Engine](#policy-engine)
10. [Database Schema](#database-schema)
11. [Security Model](#security-model)
12. [Event Bus (Redis Streams)](#event-bus-redis-streams)
13. [Observability](#observability)
14. [Known Limitations](#known-limitations)

---

## System Overview

TripWire sits between onchain payment settlement and developer applications. It transforms raw blockchain events into actionable, verified webhook payloads that applications can trust and act on without running their own blockchain infrastructure.

```mermaid
graph TB
    subgraph L0["L0 -- Chain Layer"]
        ETH["Ethereum Mainnet"]
        BASE["Base (L2)"]
        ARB["Arbitrum One (L2)"]
    end

    subgraph L1["L1 -- Indexing Layer"]
        GS["Goldsky Turbo<br/>(webhook sink)"]
    end

    subgraph L2["L2 -- TripWire Middleware"]
        API["FastAPI (port 3402)"]
        DECODE["ERC-3009 Decoder<br/>(eth-abi)"]
        FINALITY["Finality Tracker<br/>(httpx JSON-RPC)"]
        IDENTITY["ERC-8004 Resolver<br/>(IdentityRegistry + ReputationRegistry)"]
        POLICY["Policy Engine"]
        DEDUP["Nonce Deduplication"]
        DISPATCH["Webhook Dispatcher"]
    end

    subgraph L3["L3 -- Delivery Layer"]
        CONVOY["Convoy self-hosted"]
        RETRY["Exponential Backoff Retries"]
        HMAC["HMAC-SHA256 Signing"]
        DLQ["Dead Letter Queue"]
    end

    subgraph L4["L4 -- Application Layer"]
        DEV_EXEC["Developer App<br/>(Execute Mode)"]
        DEV_NOTIFY["Developer App<br/>(Notify Mode via<br/>Supabase Realtime)"]
    end

    ETH --> GS
    BASE --> GS
    ARB --> GS
    GS -->|"SQL transform + _gs_log_decode + webhook POST"| API
    API --> DECODE
    DECODE --> DEDUP
    DEDUP --> FINALITY
    FINALITY --> IDENTITY
    IDENTITY --> POLICY
    POLICY --> DISPATCH
    DISPATCH --> CONVOY
    CONVOY --> RETRY
    CONVOY --> HMAC
    CONVOY --> DLQ
    CONVOY --> DEV_EXEC
    DISPATCH -.->|"Supabase Realtime"| DEV_NOTIFY
```

### Technology Stack

| Component | Technology | Purpose |
|-----------|-----------|---------|
| Runtime | Python 3.11+ | Async-first application runtime |
| API Framework | FastAPI + Uvicorn | HTTP API server |
| Database | Supabase (managed PostgreSQL) | Event storage, endpoint registry, nonce dedup |
| Blockchain Indexing | Goldsky Turbo (webhook sink) | Stream raw logs from chains to TripWire via webhooks |
| Webhook Delivery | Convoy self-hosted | Retries, HMAC signing, DLQ |
| Blockchain RPC | httpx (raw JSON-RPC) | Finality checks, identity resolution |
| ABI Decoding | eth-abi | Decode ERC-3009 event data |
| Validation | Pydantic v2 | Input/output schema validation |
| Logging | structlog | Structured JSON logging |
| HTTP Client | httpx (async) | All outbound HTTP calls |
| Event Bus | Redis Streams (optional) | Async event processing, horizontal worker scaling |
| Metrics | Prometheus (prometheus_client) | Pipeline, request, and delivery latency metrics |
| Tracing | OpenTelemetry (optional) | Distributed tracing with OTLP export |
| Error Tracking | Sentry (optional) | Exception capture with SecretStr scrubbing |
| MCP Auth | SIWE (eth_account) + x402 | 3-tier wallet auth for AI agent tools |

---

## Architecture Layers

### L0 -- Chain Layer

The onchain source of truth. TripWire monitors ERC-3009 `transferWithAuthorization` payments on three EVM chains:

| Chain | Chain ID | USDC Contract | Finality Depth |
|-------|----------|---------------|----------------|
| Ethereum Mainnet | `1` | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` | 12 blocks |
| Base (L2) | `8453` | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` | 3 blocks |
| Arbitrum One (L2) | `42161` | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` | 1 block |

When `transferWithAuthorization` is called on a USDC contract, two events are emitted in the same transaction:

1. `Transfer(address indexed from, address indexed to, uint256 value)` -- the ERC-20 transfer
2. `AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce)` -- the ERC-3009 authorization proof

### L1 -- Goldsky Indexing Layer

Goldsky Turbo reads raw logs from each chain, applies a SQL transform that filters for `AuthorizationUsed` events on USDC contracts, decodes them using `_gs_log_decode()`, and delivers the decoded events via webhook POST to TripWire's `/api/v1/ingest` endpoint. This is a fully managed pipeline -- Goldsky handles all chain indexing and delivers events directly to TripWire. When the optional event bus is enabled, events are published to Redis Streams for async processing by a pool of trigger workers.

### L2 -- TripWire Middleware Layer

The core application layer, implemented as a FastAPI service. It performs:

- **Decoding**: Combines `AuthorizationUsed` and `Transfer` event data into a unified `ERC3009Transfer` model using `eth-abi`.
- **Deduplication**: Inserts the `(chain_id, nonce, authorizer)` tuple into the `nonces` table; the unique constraint rejects replays.
- **Finality tracking**: Queries the chain's latest block via `eth_blockNumber` JSON-RPC and computes confirmation depth.
- **Identity resolution**: Resolves the sender's ERC-8004 onchain agent identity (agent class, capabilities, reputation score) from the IdentityRegistry and ReputationRegistry contracts.
- **Policy evaluation**: Checks the transfer against the endpoint's configured policies (amount bounds, sender lists, agent class, reputation threshold).
- **Dispatch**: Builds a `WebhookPayload` and sends it to Convoy for delivery.

### L3 -- Convoy Delivery Layer

Convoy self-hosted is a webhook delivery service. TripWire delegates all delivery concerns to Convoy with a single API call per message:

- **Retries**: Exponential backoff (5s, 5m, 30m, 2h, 5h, 10h) -- up to 6 attempts over ~17 hours.
- **HMAC-SHA256 signing**: Every payload is signed. Developers verify signatures using the Convoy REST API via httpx or manual HMAC verification.
- **Dead Letter Queue (DLQ)**: Failed messages after all retry attempts are preserved for manual inspection and replay.
- **Delivery logs**: Full audit trail of every attempt, response code, and timing.

### L4 -- Application Layer

Developers receive verified payment events in two modes:

- **Execute mode**: Convoy delivers a webhook to the developer's registered URL. The developer's server executes business logic upon receipt.
- **Notify mode**: The developer subscribes to Supabase Realtime and receives events as database row changes. No webhook server required.

---

## Event Lifecycle

The complete journey of an x402 payment from chain settlement to developer delivery:

```mermaid
sequenceDiagram
    participant Chain as L0: EVM Chain
    participant GS as L1: Goldsky Turbo
    participant TW as L2: TripWire
    participant Convoy as L3: Convoy
    participant Dev as L4: Developer App

    Chain->>Chain: transferWithAuthorization() called
    Chain->>Chain: Emits Transfer + AuthorizationUsed events

    GS->>Chain: Streams raw_logs dataset
    GS->>GS: SQL transform filters USDC + AuthorizationUsed topic
    GS->>GS: _gs_log_decode() extracts authorizer + nonce
    GS->>TW: Webhook POST decoded events to /api/v1/ingest
    TW->>TW: Decode ERC-3009 (combine Transfer + AuthorizationUsed)
    TW->>TW: Nonce deduplication (INSERT into nonces table)

    TW->>Chain: eth_blockNumber JSON-RPC
    Chain-->>TW: Latest block number
    TW->>TW: Compute confirmations = latest - event block

    alt Confirmations < required depth
        TW->>TW: Emit payment.pending
    else Confirmations >= required depth
        TW->>TW: Emit payment.confirmed
    end

    TW->>Chain: ERC-8004 eth_call (balanceOf, tokenOfOwnerByIndex, getMetadata)
    Chain-->>TW: Agent identity + reputation data

    TW->>TW: Policy engine evaluates transfer against endpoint policies
    alt Policy rejects
        TW->>TW: Log rejection reason, skip delivery
    else Policy allows
        TW->>TW: Build WebhookPayload (transfer + finality + identity)
        TW->>Convoy: MessageIn(event_type, payload)
        Convoy->>Dev: POST webhook with HMAC signature
        Dev-->>Convoy: 2xx OK
        Convoy-->>TW: Message ID
    end
```

### Step-by-Step

1. **Payment on chain**: A payer calls `transferWithAuthorization()` on a USDC contract. The transaction emits both a `Transfer` event and an `AuthorizationUsed` event.
2. **Goldsky detects**: Goldsky Turbo streams `raw_logs` from the chain. A SQL transform filters for logs where `address = USDC_CONTRACT` and `topic0 = keccak256("AuthorizationUsed(address,bytes32)")`, then decodes the log using `_gs_log_decode()`.
3. **Goldsky delivers**: Decoded events are delivered via webhook POST to TripWire's `/api/v1/ingest` endpoint.
4. **TripWire processes**: The middleware receives the events, decodes the full ERC-3009 transfer (correlating `Transfer` and `AuthorizationUsed` logs), and attempts nonce deduplication.
5. **Finality check**: TripWire queries `eth_blockNumber` via JSON-RPC and computes confirmations. If below the required depth for the chain, the event is marked `payment.pending`; otherwise `payment.confirmed`.
6. **Identity enrichment**: The sender's address is resolved against the ERC-8004 IdentityRegistry contract to retrieve agent class, capabilities, deployer, and reputation score.
7. **Policy evaluation**: The transfer is checked against the endpoint's policies (amount bounds, sender allowlist/blocklist, required agent class, minimum reputation score).
8. **Convoy delivers**: If the policy passes, TripWire builds a `WebhookPayload` and sends it to Convoy via `message.create()`. Convoy handles HMAC signing, delivery, retries, and DLQ.
9. **Developer receives**: The developer's registered endpoint receives a signed HTTP POST with the full payment payload including transfer data, finality status, and agent identity.

---

## Goldsky Pipeline

Goldsky Turbo is a managed blockchain indexing service that streams onchain data to external sinks including webhooks. TripWire uses it to receive ERC-3009 events from multiple chains via webhook without running any indexing infrastructure.

### Pipeline Configuration

Each chain gets its own pipeline, generated programmatically by `tripwire/ingestion/pipeline.py`. The configuration follows the Goldsky Turbo YAML specification:

```mermaid
graph LR
    subgraph Source
        DS["Goldsky Dataset<br/>(e.g., base.raw_logs v1.0.0)"]
    end
    subgraph Transform
        SQL["SQL Transform<br/>Filter: USDC address + AuthorizationUsed topic<br/>Decode: _gs_log_decode()"]
    end
    subgraph Sink
        WH["Webhook POST<br/>TripWire /api/v1/ingest"]
    end
    DS --> SQL --> WH
```

#### Generated YAML Structure

```yaml
version: "1"
name: tripwire-base-erc3009
sources:
  base_logs:
    type: dataset
    dataset_name: base.raw_logs
    version: 1.0.0
transforms:
  erc3009_decoded:
    primary_key: id
    sql: >
      SELECT id, block_number, block_hash, transaction_hash, log_index,
             block_timestamp,
             _gs_log_decode('event AuthorizationUsed(address indexed authorizer,
                             bytes32 indexed nonce)', topics, data) AS decoded
      FROM base_logs
      WHERE address = '0x833589fcd6edb6e08f4c7c32d4f71b54bda02913'
        AND topic0 = '0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5'
sinks:
  tripwire_webhook:
    type: webhook
    url: https://your-tripwire-host/api/v1/ingest
    secret_name: TRIPWIRE_WEBHOOK_SECRET
    from: erc3009_decoded
```

### Key Design Decisions

- **Filter at the source**: The SQL transform filters by USDC contract address and `AuthorizationUsed` topic0 hash *before* delivering via webhook, so only ERC-3009 events reach TripWire. This avoids processing millions of irrelevant Transfer logs.
- **`_gs_log_decode()`**: Goldsky's built-in ABI decoder extracts `authorizer` and `nonce` from the raw `topics` and `data` fields inline in the SQL transform.
- **One pipeline per chain**: Each supported chain (Ethereum, Base, Arbitrum) gets a separate pipeline (`tripwire-ethereum-erc3009`, `tripwire-base-erc3009`, `tripwire-arbitrum-erc3009`) with its own dataset source.
- **Reorg handling**: Goldsky Turbo has built-in reorg detection. When a chain reorganization occurs, Goldsky replays the affected blocks and delivers corrected events via webhook. The `block_hash` field in the events table allows TripWire to detect and handle reorged events (emitting `payment.reorged` events).

### Pipeline Lifecycle

The `pipeline.py` module provides CLI wrappers for the `goldsky` CLI:

| Function | Command | Purpose |
|----------|---------|---------|
| `deploy_pipeline(chain_id)` | `goldsky turbo apply <config.yaml>` | Deploy a new pipeline |
| `get_pipeline_status(chain_id)` | `goldsky pipeline status <name>` | Check pipeline health |
| `stop_pipeline(chain_id)` | `goldsky pipeline stop <name>` | Pause indexing |
| `start_pipeline(chain_id)` | `goldsky pipeline start <name>` | Resume indexing |

---

## ERC-3009 Event Processing

ERC-3009 defines `transferWithAuthorization`, a function (not an event) that allows gasless USDC transfers using a signed authorization. When called, two events are emitted in the same transaction:

```mermaid
graph TD
    TX["transferWithAuthorization(from, to, value, validAfter, validBefore, nonce, v, r, s)"]
    TX --> E1["Transfer(from, to, value)<br/>Standard ERC-20 event"]
    TX --> E2["AuthorizationUsed(authorizer, nonce)<br/>ERC-3009 specific event"]
    E1 --> DECODE["decoder.py correlates both events"]
    E2 --> DECODE
    DECODE --> MODEL["ERC3009Transfer model<br/>(unified view)"]
```

### Event Topics

| Event | Topic0 (keccak256) |
|-------|-------------------|
| `AuthorizationUsed(address,bytes32)` | `0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5` |
| `Transfer(address,address,uint256)` | `0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef` |

### Decoding with eth-abi

The decoder module (`tripwire/ingestion/decoder.py`) provides two decoding paths:

**1. Raw log decoding** (`decode_erc3009_from_logs`): For processing raw transaction logs that contain both events. It:
- Iterates all logs in a transaction
- Filters for logs from known USDC contract addresses
- Matches `Transfer` and `AuthorizationUsed` by `topic0`
- Decodes `Transfer`: `from` and `to` from indexed topics (address from last 40 hex chars), `value` from `data` via `eth_abi.decode(["uint256"], data)`
- Decodes `AuthorizationUsed`: `authorizer` from `topic[1]`, `nonce` from `topic[2]` (bytes32 hex)
- Combines both into a single `ERC3009Transfer` model

**2. Goldsky-decoded rows** (`decode_transfer_event`): For rows already decoded by Goldsky's `_gs_log_decode()` SQL transform. The `decoded` column contains extracted `authorizer` and `nonce` fields directly.

### Contract Validation

The decoder validates that the emitting contract address matches the expected USDC contract for the resolved chain:

```python
_CONTRACT_TO_CHAIN: dict[str, ChainId] = {
    addr.lower(): chain_id for chain_id, addr in USDC_CONTRACTS.items()
}
```

If the contract address does not match, a `ValueError` is raised, preventing spoofed events from non-USDC contracts.

### Nonce Deduplication

Each ERC-3009 authorization has a unique `bytes32` nonce scoped to the `authorizer` address and `chain_id`. TripWire enforces deduplication via a database unique constraint:

```sql
CREATE TABLE nonces (
    chain_id    INTEGER NOT NULL,
    nonce       TEXT NOT NULL,
    authorizer  TEXT NOT NULL,
    UNIQUE (chain_id, nonce, authorizer)
);
```

When processing a new event, TripWire attempts to `INSERT` into the `nonces` table. If the `(chain_id, nonce, authorizer)` tuple already exists, the insert fails and the event is skipped as a duplicate. This guarantees at-most-once processing per authorization nonce.

---

## Finality Tracking

Block finality determines when a transaction can be considered irreversible. TripWire tracks finality by comparing confirmation depth (current block minus event block) against chain-specific thresholds.

### Confirmation Depths

```mermaid
graph LR
    subgraph "Required Confirmations"
        ETH["Ethereum<br/>12 blocks<br/>(~2.4 min)"]
        BASE_C["Base<br/>3 blocks<br/>(~6 sec)"]
        ARB_C["Arbitrum<br/>1 block<br/>(~0.25 sec)"]
    end
```

| Chain | Confirmations Required | Approximate Time |
|-------|----------------------|------------------|
| Ethereum | 12 blocks | ~2.4 minutes |
| Base | 3 blocks | ~6 seconds |
| Arbitrum | 1 block | ~0.25 seconds |

These depths are defined in `tripwire/types/models.py`:

```python
FINALITY_DEPTHS: dict[ChainId, int] = {
    ChainId.ETHEREUM: 12,
    ChainId.BASE: 3,
    ChainId.ARBITRUM: 1,
}
```

### JSON-RPC Approach

TripWire uses raw JSON-RPC calls via `httpx` (no `web3.py` dependency) to query the latest block number:

```python
payload = {
    "jsonrpc": "2.0",
    "method": "eth_blockNumber",
    "params": [],
    "id": 1,
}
```

The `check_finality` function computes:

```
confirmations = max(0, current_block - transfer.block_number)
is_finalized  = confirmations >= FINALITY_DEPTHS[chain_id]
```

### FinalityStatus Model

The result is a `FinalityStatus` model containing:

| Field | Type | Description |
|-------|------|-------------|
| `tx_hash` | `str` | Transaction hash |
| `chain_id` | `ChainId` | Chain identifier |
| `block_number` | `int` | Block the event was included in |
| `confirmations` | `int` | Current confirmation count |
| `required_confirmations` | `int` | Chain-specific threshold |
| `is_finalized` | `bool` | Whether the threshold is met |
| `finalized_at` | `int \| None` | Block number at which finality was reached |

### Event Types by Finality

| Finality State | WebhookEventType | Meaning |
|----------------|------------------|---------|
| Confirmations < required | `payment.pending` | Transaction seen but not yet final |
| Confirmations >= required | `payment.confirmed` | Transaction is considered irreversible |
| Block reorged | `payment.reorged` | Transaction was removed by a chain reorganization |
| Validation failure | `payment.failed` | Decoding or contract validation failed |

---

## ERC-8004 Identity Resolution

ERC-8004 is an onchain AI agent identity registry (went mainnet January 29, 2026). TripWire uses it to enrich payment events with sender identity data, enabling developers to make policy decisions based on who (or what) is paying.

### Registry Contracts

Both contracts are deployed via CREATE2 at the same address on all supported chains:

| Contract | Address |
|----------|---------|
| IdentityRegistry | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` |
| ReputationRegistry | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` |

### Resolution Flow

The ERC-8004 IdentityRegistry is an ERC-721 contract. Each registered agent owns an NFT (token) representing its onchain identity. Resolution proceeds through a series of `eth_call` requests:

```mermaid
sequenceDiagram
    participant TW as TripWire
    participant Cache as In-Memory Cache
    participant IR as IdentityRegistry<br/>(0x8004...a432)
    participant RR as ReputationRegistry<br/>(0x8004...9b63)

    TW->>Cache: Lookup (chain_id, address)
    alt Cache hit (TTL < 5 min)
        Cache-->>TW: Return cached AgentIdentity
    else Cache miss
        TW->>IR: balanceOf(address)
        IR-->>TW: balance (uint256)
        alt balance == 0
            TW->>TW: Return None (not an ERC-8004 agent)
        else balance > 0
            TW->>IR: tokenOfOwnerByIndex(address, 0)
            IR-->>TW: agentId (uint256)
            TW->>IR: tokenURI(agentId)
            IR-->>TW: agent URI (string)
            TW->>IR: getMetadata(agentId, "agentClass")
            IR-->>TW: agent class (bytes -> utf-8)
            TW->>IR: getMetadata(agentId, "capabilities")
            IR-->>TW: capabilities (bytes -> comma-separated utf-8)
            TW->>IR: ownerOf(agentId)
            IR-->>TW: deployer address
            TW->>RR: getSummary(agentId, [])
            RR-->>TW: reputation score (uint256, basis points)
            TW->>TW: Build AgentIdentity model
            TW->>Cache: Store with 5-min TTL
        end
    end
```

### Step-by-Step Resolution

1. **`balanceOf(address)`** -- Check if the sender address owns an ERC-8004 identity NFT. If balance is 0, the sender is not a registered agent; return `None`.
2. **`tokenOfOwnerByIndex(address, 0)`** -- Get the first (index 0) token ID owned by the address. This is the `agentId`.
3. **`tokenURI(agentId)`** -- Retrieve the agent's metadata URI (e.g., an HTTPS or IPFS URL pointing to off-chain metadata).
4. **`getMetadata(agentId, "agentClass")`** -- Read the `agentClass` key from the token's onchain metadata. Returns raw bytes decoded as UTF-8 (e.g., `"trading-bot"`, `"data-oracle"`, `"payment-agent"`).
5. **`getMetadata(agentId, "capabilities")`** -- Read the `capabilities` key. Returns a comma-separated UTF-8 string (e.g., `"swap,limit-order,portfolio-rebalance"`).
6. **`ownerOf(agentId)`** -- Get the deployer (token owner/minter) address.
7. **`getSummary(agentId, [])` on ReputationRegistry** -- Fetch the aggregate reputation score. The registry returns a `uint256` in basis points (0--10000), which TripWire converts to a 0--100 float.

### Caching Strategy

Identity resolution requires 6--7 `eth_call` RPC requests per address. To avoid excessive RPC load:

- **In-memory cache** with a 5-minute TTL (`_CACHE_TTL = 300` seconds)
- Cache key: `"{chain_id}:{address_lowercase}"`
- Both positive results (`AgentIdentity`) and negative results (`None` for non-agents) are cached
- Cache entries use `time.monotonic()` for expiry to avoid clock drift issues

### Development Mode

In development (`APP_ENV=development`), a `MockResolver` is used instead of the real ERC-8004 resolver. It provides three pre-configured agent identities for testing without requiring RPC access.

### AgentIdentity Model

| Field | Type | Description |
|-------|------|-------------|
| `address` | `str` | Agent's Ethereum address (lowercased) |
| `agent_class` | `str` | Classification (e.g., `"trading-bot"`, `"data-oracle"`) |
| `deployer` | `str` | Address that deployed/minted the agent NFT |
| `capabilities` | `list[str]` | List of declared capabilities |
| `reputation_score` | `float` (0--100) | Aggregate reputation from the ReputationRegistry |
| `registered_at` | `int` | Registration timestamp (or token ID as proxy) |
| `metadata` | `dict` | Additional data (`agent_id`, `agent_uri`) |

---

## Webhook Delivery

TripWire uses Convoy self-hosted as a webhook delivery service. The integration is implemented in `tripwire/webhook/convoy_client.py` (Convoy REST API via httpx wrapper) and `tripwire/webhook/dispatcher.py` (orchestration layer).

### Convoy Model

```mermaid
graph TD
    subgraph TripWire
        DISPATCH["Dispatcher<br/>Build payload + match endpoints"]
    end
    subgraph Convoy["Convoy self-hosted"]
        APP["Project<br/>(one per developer)"]
        EP["Endpoint<br/>(developer's webhook URL)"]
        MSG["Message<br/>(payment event payload)"]
        SIGN["HMAC-SHA256 Signing"]
        RETRY_S["Retry Engine<br/>5s, 5m, 30m, 2h, 5h, 10h"]
        DLQ_S["Dead Letter Queue"]
        LOG["Delivery Logs"]
    end
    subgraph Developer
        URL["Developer's URL"]
    end

    DISPATCH -->|"message.create()"| MSG
    MSG --> SIGN
    SIGN --> EP
    EP -->|"POST"| URL
    URL -->|"non-2xx"| RETRY_S
    RETRY_S -->|"all retries exhausted"| DLQ_S
    EP --> LOG
```

### Convoy Resource Mapping

| Convoy Concept | TripWire Mapping |
|---------------|-----------------|
| Project | One per registered developer/endpoint |
| Endpoint | Developer's webhook URL |
| Message | A single `WebhookPayload` (payment event) |
| Event Type | `payment.confirmed`, `payment.pending`, `payment.failed`, `payment.reorged` |

### Retry Schedule

Convoy handles retries for failed deliveries (non-2xx responses) on an exponential backoff schedule:

| Attempt | Delay After Previous |
|---------|---------------------|
| 1 | Immediate |
| 2 | 5 seconds |
| 3 | 5 minutes |
| 4 | 30 minutes |
| 5 | 2 hours |
| 6 | 5 hours |

If all 6 attempts fail, the message is moved to the Dead Letter Queue (DLQ) for manual inspection and replay via `retry_message()`.

### Endpoint Matching

When a new ERC-3009 transfer is processed, TripWire matches it against registered endpoints:

```python
def match_endpoints(transfer, endpoints) -> list[Endpoint]:
    # An endpoint matches if:
    # 1. endpoint.recipient == transfer.to_address (case-insensitive)
    # 2. transfer.chain_id is in endpoint.chains
    # 3. endpoint.active == True
```

For Notify mode, subscriptions are matched against transfer data using filters:

| Filter | Match Condition |
|--------|----------------|
| `chains` | `transfer.chain_id` is in the list |
| `senders` | `transfer.from_address` is in the list (case-insensitive) |
| `recipients` | `transfer.to_address` is in the list (case-insensitive) |
| `min_amount` | `transfer.value >= min_amount` |
| `agent_class` | Sender's ERC-8004 `agent_class` matches |

### Execute vs Notify Mode

| Aspect | Execute Mode | Notify Mode |
|--------|-------------|-------------|
| Delivery | Convoy webhook POST | Supabase Realtime push |
| Server required | Yes (developer hosts a URL) | No (client-side subscription) |
| Retries | Convoy handles retries (6 attempts) | Supabase Realtime reconnection |
| Signing | HMAC-SHA256 via Convoy | N/A (Supabase auth) |
| Use case | Server-to-server automation | Client-side dashboards, notifications |

### Webhook Payload Structure

```json
{
  "id": "550e8400-e29b-41d4-a716-446655440000",
  "type": "payment.confirmed",
  "mode": "execute",
  "timestamp": 1710000000,
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0xabc...",
      "block_number": 12345678,
      "from_address": "0x...",
      "to_address": "0x...",
      "amount": "1000000",
      "nonce": "0x...",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": {
      "confirmations": 3,
      "required_confirmations": 3,
      "is_finalized": true
    },
    "identity": {
      "address": "0x...",
      "agent_class": "trading-bot",
      "deployer": "0x...",
      "capabilities": ["swap", "limit-order"],
      "reputation_score": 85.0,
      "registered_at": 1738108800,
      "metadata": {"agent_id": 1, "agent_uri": "..."}
    }
  }
}
```

---

## Policy Engine

The policy engine (`tripwire/api/policies/engine.py`) evaluates whether a transfer should be delivered to an endpoint based on developer-configured rules. Policies are attached to endpoints at registration time.

### Evaluation Flow

```mermaid
graph TD
    START["Incoming Transfer"] --> AMT_MIN{"min_amount set?"}
    AMT_MIN -->|Yes| CHK_MIN["amount >= min_amount?"]
    AMT_MIN -->|No| AMT_MAX
    CHK_MIN -->|No| REJECT["REJECT<br/>Amount below minimum"]
    CHK_MIN -->|Yes| AMT_MAX{"max_amount set?"}

    AMT_MAX -->|Yes| CHK_MAX["amount <= max_amount?"]
    AMT_MAX -->|No| BLOCKED
    CHK_MAX -->|No| REJECT2["REJECT<br/>Amount above maximum"]
    CHK_MAX -->|Yes| BLOCKED{"blocked_senders set?"}

    BLOCKED -->|Yes| CHK_BLK["sender in blocklist?"]
    BLOCKED -->|No| ALLOWED
    CHK_BLK -->|Yes| REJECT3["REJECT<br/>Sender is blocked"]
    CHK_BLK -->|No| ALLOWED{"allowed_senders set?"}

    ALLOWED -->|Yes| CHK_ALW["sender in allowlist?"]
    ALLOWED -->|No| AGENT
    CHK_ALW -->|No| REJECT4["REJECT<br/>Sender not in allowlist"]
    CHK_ALW -->|Yes| AGENT{"required_agent_class set?"}

    AGENT -->|Yes| CHK_AGT["identity available<br/>AND class matches?"]
    AGENT -->|No| REPUTATION
    CHK_AGT -->|No| REJECT5["REJECT<br/>Agent class mismatch<br/>or no identity"]
    CHK_AGT -->|Yes| REPUTATION{"min_reputation_score set?"}

    REPUTATION -->|Yes| CHK_REP["identity available<br/>AND score >= min?"]
    REPUTATION -->|No| ACCEPT["ACCEPT"]
    CHK_REP -->|No| REJECT6["REJECT<br/>Reputation too low<br/>or no identity"]
    CHK_REP -->|Yes| ACCEPT
```

### Policy Fields

All fields are optional. If a field is not set (`None`), that check is skipped.

| Field | Type | Description |
|-------|------|-------------|
| `min_amount` | `str \| None` | Minimum transfer amount (in smallest unit, USDC 6 decimals). Reject if `amount < min_amount`. |
| `max_amount` | `str \| None` | Maximum transfer amount. Reject if `amount > max_amount`. |
| `allowed_senders` | `list[str] \| None` | Allowlist of sender addresses. If set, only these senders can trigger webhooks. |
| `blocked_senders` | `list[str] \| None` | Blocklist of sender addresses. Checked before allowlist. |
| `required_agent_class` | `str \| None` | Required ERC-8004 agent class (e.g., `"trading-bot"`). Requires identity resolution. |
| `min_reputation_score` | `float \| None` | Minimum ERC-8004 reputation score (0--100). Requires identity resolution. |
| `finality_depth` | `int` | Custom finality depth override (default 3, range 1--64). |

### Evaluation Order

1. **Amount range** -- `min_amount`, then `max_amount`
2. **Sender blocklist** -- `blocked_senders`
3. **Sender allowlist** -- `allowed_senders`
4. **Agent class** -- `required_agent_class` (requires ERC-8004 identity)
5. **Reputation** -- `min_reputation_score` (requires ERC-8004 identity)

The function returns `(allowed: bool, reason: str | None)`. If `allowed` is `False`, `reason` contains a human-readable explanation.

---

## Database Schema

TripWire uses Supabase (managed PostgreSQL). The schema is built up through incremental migrations in `tripwire/db/migrations/` (001 through 019 as of this writing).

### ER Diagram

```mermaid
erDiagram
    endpoints ||--o{ subscriptions : "has"
    endpoints ||--o{ webhook_deliveries : "receives"
    events ||--o{ webhook_deliveries : "triggers"
    events ||--o{ event_endpoints : "matched by"
    endpoints ||--o{ event_endpoints : "matched to"

    endpoints {
        text id PK
        text url
        text mode "notify | execute"
        jsonb chains
        text recipient
        jsonb policies
        boolean active
        timestamptz created_at
        timestamptz updated_at
    }

    subscriptions {
        text id PK
        text endpoint_id FK
        jsonb filters
        boolean active
        timestamptz created_at
    }

    events {
        text id PK
        integer chain_id
        text tx_hash
        bigint block_number
        text block_hash
        integer log_index
        text from_address
        text to_address
        text amount
        text authorizer
        text nonce
        text token
        text status "pending | confirmed | failed | reorged"
        integer finality_depth
        jsonb identity_data
        timestamptz created_at
        timestamptz confirmed_at
    }

    event_endpoints {
        text event_id FK
        text endpoint_id FK
        timestamptz matched_at
    }

    nonces {
        integer chain_id
        text nonce
        text authorizer
        timestamptz created_at
        timestamptz reorged_at "NULL unless reorged"
    }

    nonces_archive {
        integer chain_id
        text nonce
        text authorizer
        timestamptz created_at
        timestamptz archived_at
    }

    webhook_deliveries {
        text id PK
        text endpoint_id FK
        text event_id FK
        text convoy_message_id
        text status "pending | delivered | failed"
        timestamptz created_at
    }

    audit_log {
        text id PK
        text action
        text entity_type
        text entity_id
        jsonb metadata
        timestamptz created_at
    }
```

### Table Descriptions

#### `endpoints`
Developer-registered webhook endpoints. Each endpoint specifies a URL, delivery mode (execute or notify), the chains to monitor, the USDC recipient address, and optional policies. Management requests are authenticated via SIWE wallet signature (api_key columns were removed in migration 010).

**Key indexes**: `recipient` (for matching transfers), `active` (partial index on `TRUE`), `mode`.

#### `subscriptions`
Notify-mode subscription filters attached to endpoints. Each subscription defines filter criteria (chains, senders, recipients, min_amount, agent_class) that determine which events trigger Supabase Realtime notifications. Cascades on endpoint deletion.

#### `events`
Combined ERC-3009 event data: Transfer fields (`from_address`, `to_address`, `amount`) plus AuthorizationUsed fields (`authorizer`, `nonce` as bytes32 hex). Tracks lifecycle via `status` (pending/confirmed/failed/reorged) and stores resolved identity data as JSONB.

**Key indexes**: `(chain_id, tx_hash)` for transaction lookup, `to_address` and `from_address` for address-based queries, `(chain_id, nonce, authorizer)` for deduplication cross-reference, `(chain_id, block_number)` for finality range scans, `created_at` for cursor pagination.

#### `event_endpoints`
M2M join table (added migration 014) linking a processed event to every endpoint it matched. Replaces the old single `endpoint_id` foreign key on `events`, allowing one event to fan out to multiple endpoints. `webhook_deliveries` is the authoritative delivery record; `event_endpoints` provides the match index.

#### `nonces`
Deduplication table. The `UNIQUE (chain_id, nonce, authorizer)` constraint enforces that each ERC-3009 authorization nonce can only be processed once per chain and authorizer. INSERT failures on this constraint indicate duplicate events. The `reorged_at` column (migration 018) records when a nonce was invalidated by a chain reorg, enabling re-processing of re-broadcast transactions via `record_nonce_with_reorg()`.

#### `nonces_archive`
Background archival table (migration 019). Old nonce rows are periodically moved here by the archival job (`tripwire/db/archival.py`) to keep the hot `nonces` table small while preserving the deduplication history.

#### `webhook_deliveries`
Tracks the relationship between events and endpoint deliveries. Stores the Convoy message ID for cross-referencing delivery status with Convoy's delivery logs. Cascades on both endpoint and event deletion.

#### `audit_log`
Immutable append-only log of system actions. Records the action type, affected entity, and metadata JSONB for forensic analysis. Primary key is auto-generated via `gen_random_uuid()`.

### Key Design Decisions

1. **Nonce deduplication via unique constraint**: Rather than application-level deduplication (which is race-prone), TripWire uses a PostgreSQL unique constraint on `(chain_id, nonce, authorizer)`. This is atomic, idempotent, and works correctly under concurrent writes.

2. **Cursor pagination via `created_at`**: The `idx_events_created_at` index supports efficient cursor-based pagination for the events API. Clients pass the last seen `created_at` timestamp to fetch the next page.

3. **Amounts as text**: All USDC amounts are stored as `TEXT` (not numeric) to preserve exact 6-decimal precision without floating-point rounding errors. Application code handles conversion to integers for comparison.

4. **JSONB for flexible fields**: `chains` (array), `policies` (object), `filters` (object), and `identity_data` (object) use JSONB for schema flexibility while keeping the core relational structure strict.

5. **Cascade deletes**: `subscriptions` and `webhook_deliveries` cascade on endpoint deletion, ensuring referential integrity without orphaned records.

6. **Partial indexes**: The `active = TRUE` partial indexes on `endpoints` and `subscriptions` optimize the hot path (querying only active records) without indexing deactivated rows.

7. **Precomputed `topic0` on triggers**: Migration 017 added a `topic0` column to `triggers` and `trigger_templates`, storing the keccak256 event-signature hash inline. This enables O(1) index lookups during event routing without recomputing the hash at query time.

8. **Nonce archival**: Migration 019 introduced `nonces_archive` and a background archival job. Active nonces older than a configurable threshold are moved to the archive table, keeping the hot-path `nonces` table lean for the unique-constraint INSERT that performs deduplication.

---

## Security Model

### HMAC-SHA256 Webhook Signing

Every webhook payload delivered by Convoy is signed with HMAC-SHA256. The signing process is handled entirely by Convoy:

```mermaid
sequenceDiagram
    participant TW as TripWire
    participant Convoy
    participant Dev as Developer App

    TW->>Convoy: message.create(payload)
    Convoy->>Convoy: Sign payload with endpoint secret<br/>(HMAC-SHA256)
    Convoy->>Dev: POST /webhook<br/>Headers: X-TripWire-ID, X-TripWire-Timestamp, X-TripWire-Signature
    Dev->>Dev: Verify signature using Convoy REST API via httpx<br/>or manual HMAC-SHA256 check
    Dev-->>Convoy: 200 OK
```

Each Convoy endpoint gets a unique signing secret. Developers verify incoming webhooks by:
1. Extracting `X-TripWire-ID`, `X-TripWire-Timestamp`, and `X-TripWire-Signature` headers
2. Computing `HMAC-SHA256(secret, "{X-TripWire-ID}.{X-TripWire-Timestamp}.{body}")` and comparing against the signature
3. Rejecting requests where the timestamp is older than a tolerance window (replay protection)

### Nonce Replay Protection

ERC-3009 authorizations include a `bytes32` nonce that is unique per `(chain_id, authorizer)`. TripWire enforces at-most-once processing:

```
INSERT INTO nonces (chain_id, nonce, authorizer) VALUES ($1, $2, $3)
-- Fails with unique constraint violation if already processed
```

This prevents:
- **Double-spending**: The same authorization nonce cannot trigger multiple webhook deliveries
- **Replay attacks**: Re-submitting an already-processed event is rejected at the database level
- **Race conditions**: The PostgreSQL unique constraint is atomic, handling concurrent inserts correctly

### SIWE Wallet Authentication

Management requests (creating/updating endpoints, listing deliveries, etc.) are authenticated via Sign-In With Ethereum (SIWE). The caller signs a structured message with their wallet; TripWire verifies the signature via `eth_account`. API key columns were removed in migration 010 — no API keys are stored in the database.

### Contract Address Validation

The decoder validates that events originate from the expected USDC contract address for each chain:

```python
expected = USDC_CONTRACTS[chain_id].lower()
if contract != expected:
    raise ValueError(...)
```

This prevents processing events from rogue contracts that emit `AuthorizationUsed` or `Transfer` events with the same topic signatures but from different (potentially malicious) contract addresses.

### Supabase Service Role Key

TripWire uses the `supabase_service_role_key` (not the anon key) for database operations. The service role key bypasses Row Level Security (RLS), which is appropriate for a backend service but must be kept secret. The anon key is stored separately for potential client-facing features.

### Summary of Security Layers

| Layer | Mechanism | Protects Against |
|-------|-----------|-----------------|
| Webhook signing | HMAC-SHA256 via Convoy | Payload tampering, webhook spoofing |
| Timestamp validation | Convoy signature includes timestamp | Replay attacks on webhook delivery |
| Nonce deduplication | PostgreSQL unique constraint | Double-processing, replay of onchain events |
| SIWE wallet auth | EIP-4361 signature verification | Unauthorized management API access |
| Contract validation | Address check against known USDC contracts | Spoofed events from malicious contracts |
| Service role isolation | Supabase service_role key (backend only) | Unauthorized database access |
| MCP 3-tier auth | SIWE wallet signature + x402 payment | Unauthorized tool access, impersonation |
| Body-hash binding | SHA256(body) in SIWE statement | Request tampering after signature |
| Redis nonce | Atomic delete on use | SIWE replay attacks |

---

## Event Bus (Redis Streams)

An optional async processing layer between Goldsky ingestion and trigger evaluation, enabled via `EVENT_BUS_ENABLED=true`.

### Architecture

When enabled, ingest routes publish events to Redis Streams partitioned by `topic0` (event signature keccak256 hash). A pool of `TriggerWorker` tasks consume events via `XREADGROUP` consumer groups for at-least-once delivery.

```mermaid
graph LR
    subgraph Ingest
        GS["Goldsky POST"]
        API["/api/v1/ingest"]
    end
    subgraph EventBus["Redis Streams"]
        S1["tripwire:events:0xddf2..."]
        S2["tripwire:events:0x98de..."]
        SN["tripwire:events:..."]
        DLQ["tripwire:dlq"]
    end
    subgraph Workers["Worker Pool"]
        W1["Worker-0"]
        W2["Worker-1"]
        W3["Worker-2"]
    end
    subgraph Processing
        EP["EventProcessor"]
    end

    GS --> API
    API -->|"XADD"| S1
    API -->|"XADD"| S2
    API -->|"XADD"| SN
    S1 -->|"XREADGROUP"| W1
    S2 -->|"XREADGROUP"| W2
    SN -->|"XREADGROUP"| W3
    W1 --> EP
    W2 --> EP
    W3 --> EP
    W1 -.->|"after 5 failures"| DLQ
```

### Key Properties

| Property | Detail |
|----------|--------|
| Delivery semantics | At-least-once via XREADGROUP consumer groups |
| Partitioning | By topic0 (event signature hash), round-robin across workers |
| Stale message recovery | XAUTOCLAIM after 30s idle |
| Dead letter queue | `tripwire:dlq` after 5 processing failures |
| Stream cap | 500 max streams, 100 max per worker |
| Graceful degradation | Falls back to sync processing when Redis publish fails |
| Feature flag | `EVENT_BUS_ENABLED=false` (default) — zero impact |

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `EVENT_BUS_ENABLED` | `false` | Enable Redis Streams async processing |
| `EVENT_BUS_WORKERS` | `3` | Number of consumer tasks |

---

## Observability

TripWire includes a full observability stack. All components are optional and degrade gracefully when not configured.

### Prometheus Metrics (`/metrics`)

| Type | Metric | Labels |
|------|--------|--------|
| Counter | `events_processed_total` | chain, event_type, status |
| Counter | `webhooks_sent_total` | endpoint, event_type |
| Counter | `errors_total` | component, error_type |
| Counter | `auth_requests_total` | method, status |
| Counter | `nonce_dedup_total` | chain, result |
| Histogram | `pipeline_duration_seconds` | — |
| Histogram | `request_duration_seconds` | — |
| Histogram | `webhook_delivery_duration_seconds` | — |
| Gauge | `dlq_backlog` | — |
| Info | `tripwire_build_info` | version, env |

Optional auth: set `METRICS_BEARER_TOKEN` to protect `/metrics` in production.

### OpenTelemetry Tracing

Set `OTEL_ENABLED=true` and `OTEL_ENDPOINT=<collector>` to enable distributed tracing. Uses batch span processor with OTLP exporter. Falls back to a no-op tracer when the OTel package is not installed.

### Sentry Error Tracking

Set `SENTRY_DSN=<dsn>` to enable error capture. A `before_send` hook strips `SecretStr` values from Sentry events. Configurable `SENTRY_TRACES_SAMPLE_RATE` (default 0.1).

### Health Endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /health` | Basic liveness check |
| `GET /health/detailed` | Deep probe: Supabase, Redis, webhook provider, identity resolver, background tasks, worker pool |
| `GET /ready` | Readiness probe (200 only after lifespan startup completes) |

---

## Known Limitations

Documented architectural limitations. Acceptable for a single-instance beta launch but must be addressed before horizontal scaling.

### Pre-Scale Blockers

1. **Finality poller has no distributed coordination.** In multi-instance deployments, every instance runs its own poller, causing duplicate `payment.confirmed` webhooks. *Fix:* Add Redis-based distributed lock or leader election before scaling horizontally.

2. **Reorged nonce recovery is implemented but requires finality poller integration.** Migration 018 added `reorged_at` column and `record_nonce_with_reorg()` to mark nonces as reorged rather than permanently consumed. The finality poller must call `record_nonce_with_reorg()` on detected reorgs; until that integration is wired end-to-end, re-broadcast transactions with the same ERC-3009 nonce may still be rejected as duplicates.

3. **Pre-confirmed events can get stranded.** The x402 facilitator fast path creates `payment.pre_confirmed` events with synthetic pseudo-tx-hashes. If the real tx never lands, these sit in `pending` forever. *Fix:* Add a TTL-based cleanup job that fires `payment.failed` after N minutes.

### Horizontal Scaling

4. **In-process caches are not shared across instances.** Trigger cache (30s TTL), identity cache (300s TTL), and trigger repository cache are per-process. Cache invalidation only flushes the local instance. *Fix:* Redis pub/sub for invalidation signals; move identity cache to Redis.

5. **`event_endpoints` join table added in migration 014.** Events can now match multiple endpoints (M2M). The `search_events` MCP tool should query through `event_endpoints` rather than a single `endpoint_id` foreign key; older query paths that assumed one endpoint per event may need updating.

6. **Webhook signing secrets are held exclusively by Convoy** (migration 016 dropped `webhook_secret` from the `endpoints` table). A Convoy compromise exposes signing secrets; TripWire's database alone does not. Envelope encryption with KMS remains a hardening option for the Convoy secret store.

7. **Nonce archival is background-only.** Migration 019 added `nonces_archive` and the archival job (`tripwire/db/archival.py`), but the job must be kept running continuously to prevent the hot `nonces` table from accumulating unbounded rows. If the archival job crashes silently in production, the table will grow until manually remediated.
