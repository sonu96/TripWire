# TripWire Architecture

Programmable onchain event triggers for AI agents.

Built on Goldsky. Verified by TripWire. Delivered via Convoy.

TripWire is the infrastructure layer between onchain events and application
execution. It watches EVM chains for specific log events, decodes them,
evaluates policies, resolves agent identity, and delivers structured webhooks
or real-time notifications. x402 payment webhooks are the primary use case,
but the trigger registry supports arbitrary EVM events.

The [TWSS-1 Skill Spec](SKILL-SPEC.md) defines the execution-aware output
contract: lifecycle states (provisional/confirmed/finalized/reorged),
three-layer gating (can_pay/can_trust/is_safe), and the two-phase execution
model (prepare/commit). Machine-readable at `/.well-known/tripwire-skill-spec.json`.

Last updated: 2026-03-18

---

## 1. System Overview

TripWire is organized into six architecture layers spanning two planes:

```
─── CONTROL PLANE (MCP + API) ────────────────────────────────────────────
L5  MCP            AI agent interface (8 tools, 4-tier auth, x402 Bazaar)
L4  Application    Developer's API (receives structured webhook, executes logic)
L3  Delivery       Convoy (execute mode) + Supabase Realtime (notify mode)
L2  Middleware      TripWire FastAPI (decode, dedup, finality, identity, policy)

─── DATA PLANE (event ingestion) ─────────────────────────────────────────
L1  Indexing       Goldsky Turbo (SQL transform + webhook sink -> /ingest)
L0  Chain          Base / Ethereum / Arbitrum (ERC-3009 + arbitrary EVM events)
```

**Goldsky serves two distinct architectural roles:**

| Role            | Product        | Function                                    |
|-----------------|----------------|---------------------------------------------|
| Event streaming | Goldsky Turbo  | Indexes raw EVM logs, runs SQL transforms, delivers decoded events via webhook to `/ingest/goldsky` |
| RPC queries     | Goldsky Edge   | Managed JSON-RPC endpoints for finality polling (`eth_blockNumber`, `eth_getBlockByNumber`) and identity resolution (`eth_call` to ERC-8004) |

Turbo is the data plane -- it pushes events to TripWire. Edge is the query
plane -- TripWire pulls block data from it. Both are Goldsky products but
serve different architectural roles and use different authentication.

**Runtime stack:**

| Component            | Technology                                         |
|----------------------|----------------------------------------------------|
| Language             | Python 3.11+                                       |
| API framework        | FastAPI + Uvicorn                                  |
| Database             | Supabase (managed PostgreSQL)                      |
| Webhook delivery     | Convoy self-hosted (retries, HMAC, DLQ)            |
| Notify delivery      | Supabase Realtime (WebSocket push on DB insert)    |
| Event streaming (data plane) | Goldsky Turbo (SQL transform + webhook sink) |
| RPC queries (query plane)   | Goldsky Edge (managed JSON-RPC endpoints)    |
| ABI decoding         | eth-abi                                             |
| Event bus (optional) | Redis Streams                                      |
| Validation           | Pydantic v2                                         |
| Logging              | structlog (structured JSON)                        |
| Metrics              | Prometheus (prometheus_client)                     |
| Tracing              | OpenTelemetry (optional)                           |
| Error tracking       | Sentry (optional)                                  |
| Auth                 | SIWE wallet signatures + session tokens (no API keys) |
| Payment gating       | x402 protocol (ERC-3009 micropayments)             |

### 1.1 Dual-Product Architecture: Pulse and Keeper

TripWire operates as a dual-product platform controlled by a single
`PRODUCT_MODE` setting (`tripwire/config/settings.py`). The two products
share the ingestion pipeline, delivery layer, and identity system, but
differ in what events they care about and how they process them.

| Product | Purpose | Event Types | Processing |
|---------|---------|-------------|------------|
| **Pulse** | Generic onchain event triggers | Any EVM log (Transfer, Swap, Liquidation, etc.) | Dynamic triggers with ABI decode + JMESPath filters |
| **Keeper** | Managed x402 payment infrastructure | ERC-3009 TransferWithAuthorization | Payment-specific decode, nonce dedup, facilitator correlation |

**ProductMode enum** (`tripwire/types/models.py`):

```
ProductMode.PULSE   — only Pulse handlers active
ProductMode.KEEPER  — only Keeper handlers active
ProductMode.BOTH    — both active (default)
```

The `settings.is_pulse` and `settings.is_keeper` convenience properties
determine which handlers are registered with the `EventProcessor` at
startup. MCP tools carry a `product` tag (`"pulse"`, `"keeper"`, or
`"both"`) that controls visibility based on the active product mode.

**Event-neutral model** (`tripwire/types/models.py`):

All events flow through a shared `OnchainEvent` base model that is
product-neutral. Product-specific subclasses extend it:

```
OnchainEvent (base)
  ├── event_id, event_type, chain_id, tx_hash, block_number, block_hash,
  │   log_index, contract_address, topic0, decoded_fields, timestamp,
  │   execution (ExecutionBlock), identity, source
  │
  ├── PaymentEvent (Keeper)
  │     └── payment: PaymentData (amount, token, from/to, nonce, authorizer)
  │
  └── TriggerEvent (Pulse)
        └── trigger_id, trigger_name, filter_matched
```

The database schema (migration 026) adds event-neutral columns
(`event_type`, `decoded_fields`, `source`, `trigger_id`, `product_source`)
to the `events` table with safe defaults so existing Keeper payment rows
are unaffected. The `product_source` column (`"keeper"` or `"pulse"`)
enables operational routing queries.

**Processor handler pattern**:

The `EventProcessor` is event-type agnostic. It detects the event type
from the raw log's topic0 signature and delegates to the first registered
handler whose `can_handle()` returns True. Product-specific logic lives
in `tripwire/ingestion/handlers/`:

```
EventProcessor.process_event(raw_log)
  │
  ├── _detect_event_type(raw_log)
  │     Returns "erc3009_transfer" or ("dynamic", [triggers])
  │
  └── for handler in self._handlers:
        if await handler.can_handle(detection, raw_log):
            return await handler.handle(raw_log, processor, detection)
```

Two concrete handlers implement the `EventHandler` protocol
(`tripwire/ingestion/handlers/base.py`):

| Handler | Product | `can_handle()` | Processing |
|---------|---------|-----------------|------------|
| `PaymentHandler` | Keeper | `event_type == "erc3009_transfer"` | ERC3009Decoder → nonce dedup (correlation-aware) → finality + identity (parallel) → `_dispatch_for_transfer` |
| `TriggerHandler` | Pulse | `isinstance(event_type, tuple)` | AbiGenericDecoder per trigger → filter → payment gate (C3) → dedup → identity → reputation gate → endpoint fetch → dispatch |

The `EventHandler` protocol defines two methods:

```python
class EventHandler(Protocol):
    async def can_handle(self, event_type, raw_log) -> bool: ...
    async def handle(self, raw_log, processor, event_type) -> dict | None: ...
```

Handlers receive a `processor` reference to access shared utilities
(endpoint cache, nonce repo, webhook provider, identity resolver, event
recording, delivery recording).

---

## 2. Master Architecture Diagram

```
═══ DATA PLANE (Goldsky Turbo → TripWire) ════════════════════════════════

  L0: Blockchain (Base / Ethereum / Arbitrum)
           |  raw EVM logs
           v
  L1: Goldsky Turbo (per-chain pipelines: tripwire-{chain}-erc3009)
      |
      +-- Source: {chain}.raw_logs dataset
      +-- SQL Transform: _gs_log_decode()
      |     INNER JOIN AuthorizationUsed + Transfer on same tx + contract
      |     Filter: USDC contract address + topic0
      |     Output: pre-decoded, pre-joined rows (authorizer, nonce,
      |             from, to, value, block metadata)
      +-- Sink: webhook POST to /api/v1/ingest/goldsky
            Auth: Bearer GOLDSKY_WEBHOOK_SECRET
            Mode: one_row_per_request
           |
           |  decoded events (~1-4s from block)
           v
═══ CONTROL PLANE (TripWire Engine) ══════════════════════════════════════

                              AI Agents
                                 |
                        +--------+--------+
                        |   MCP Server    |  POST /mcp (JSON-RPC 2.0)
                        |  8 tools        |  4-tier auth: PUBLIC / SIWX / SESSION / X402
                        +--------+--------+
                                 |
   +-----------------------------+-----------------------------+
   |                     FastAPI Application                   |
   |                                                           |
   |  +----------+  +----------+  +----------+  +-----------+ |
   |  | /ingest  |  | /api/v1  |  | /health  |  | /metrics  | |
   |  | goldsky  |  | endpoints|  | /ready   |  | prometheus| |
   |  | event    |  | triggers |  | detailed |  |           | |
   |  +----+-----+  | events   |  +----------+  +-----------+ |
   |       |        | subs     |                               |
   |       |        +----------+                               |
   +-------+-----------------------------------------------+--+
           |                                                |
           v                                                |
   +-------+--------+                                      |
   | EVENT_BUS_     |  (feature-flagged, default OFF)      |
   | ENABLED?       |                                      |
   +---+--------+---+                                      |
       |        |                                          |
      YES       NO                                         |
       |        |                                          |
       v        |                                          |
   +---+----+   |                                          |
   | Redis  |   |                                          |
   | Streams|   |    +-----------------------------------+ |
   | topic0 |   |    |       EventProcessor              | |
   | keyed  |   +--->|                                   | |
   +---+----+        |  detect -> decode -> dedup        | |
       |             |  -> finality + identity (parallel) | |
       v             |  -> endpoint match -> policy      | |
   +---+--------+    |  -> dispatch                      | |
   | WorkerPool |    +-------+--------+---------+--------+ |
   | N workers  |--->|       |        |         |          |
   +------------+    |       v        v         v          |
                     | +-----+--+ +---+----+ +--+-------+  |
                     | | Convoy | | Realtime| | Events  |  |
                     | | Provider| | Notifier| | Table   |  |
                     | +-----+--+ +---+----+ +--+-------+  |
                     +-------+--------+---------+--------+--+
                             |        |         |
                             v        v         v
                        +---------+ +--------+ +---------+
                        | Convoy  | |Supabase| |Supabase |
                        | Server  | |Realtime| |Postgres |
                        +---------+ +--------+ +---------+
                             |
                             v
                      Developer's API
                      (L4 Application)

═══ QUERY PLANE (Goldsky Edge RPC) ═══════════════════════════════════════

  Goldsky Edge (managed JSON-RPC, authenticated via GOLDSKY_EDGE_API_KEY)
      |
      +-- Finality Poller: eth_blockNumber + eth_getBlockByNumber
      |     Per-chain poll loops for confirmation depth + reorg detection
      +-- Identity Resolver: eth_call to ERC-8004 registry contracts
      |     Agent identity + reputation score lookups
      +-- Client: tripwire/rpc.py (shared httpx.AsyncClient singleton)
```

---

## 3. Input Sources

TripWire accepts events through three input paths, each with different
latency and trust characteristics.

### 3.1 Goldsky Turbo (reliable path)

```
  EVM Chain (log emitted)
       |  raw blocks with EVM logs
       v
  Goldsky Turbo Pipeline: tripwire-{chain}-erc3009
       |
       |  1. Source: {chain}.raw_logs dataset (raw_logs table)
       |  2. SQL Transform (_gs_log_decode):
       |     - Self-JOIN {chain}_logs on transaction_hash + address
       |     - Left side: AuthorizationUsed events (topic0 filter)
       |     - Right side: Transfer events (topic0 filter)
       |     - WHERE: address = USDC contract for this chain
       |     - _gs_log_decode() decodes indexed + data params from ABI
       |     - Output columns: block_number, block_hash, tx_hash,
       |       log_index, block_timestamp, chain_id, authorizer,
       |       nonce, from_address, to_address, transfer value
       |  3. Sink: webhook to TripWire
       |
       |  ~1-4s from block to delivery
       v
  POST /api/v1/ingest/goldsky
       |  Verified via Bearer token (GOLDSKY_WEBHOOK_SECRET)
       |  Batch max: 1000 logs per request
       |  Mode: one_row_per_request (each decoded row = one HTTP POST)
       v
  EventProcessor.process_event()
       |
       v
  detect -> decode -> dedup -> finality -> identity -> policy -> dispatch
```

**Key insight**: TripWire receives pre-decoded, pre-joined events from
Goldsky, not raw logs. The SQL transform (step 2 above) runs inside
Goldsky's infrastructure before delivery. This means the
`AuthorizationUsed + Transfer` JOIN happens at the indexing layer, and
TripWire only needs to extract fields from the already-decoded payload.

**Pipeline configuration** (`tripwire/ingestion/pipeline.py`):

- Builds per-chain YAML configs programmatically
- Pipeline naming convention: `tripwire-{chain}-erc3009`
- Deployed via `goldsky turbo apply {config.yaml}`
- Lifecycle helpers: `deploy_pipeline()`, `stop_pipeline()`,
  `start_pipeline()`, `get_pipeline_status()`

The Goldsky path supports both hardcoded ERC-3009 events and dynamic
triggers registered through the trigger registry.

### 3.2 x402 Facilitator (fast path)

```
  x402 Facilitator (verifies ERC-3009 signature off-chain)
       |
       v
  POST /api/v1/ingest/facilitator
       |  Verified via HMAC (FACILITATOR_WEBHOOK_SECRET)
       |  ~100ms end-to-end target
       v
  EventProcessor.process_pre_confirmed_event()
       |
       v
  dedup -> identity -> endpoint match -> policy -> dispatch
  (SKIPS decode + finality -- tx not yet onchain)
```

The fast path achieves ~100ms by skipping decode (data is already structured
from the facilitator) and finality (the transaction has not yet been mined).
The event is recorded as `payment.pre_confirmed` with `block_number=0`.

**Unified event lifecycle (migration 020):** The facilitator and Goldsky paths
now produce a SINGLE event that transitions states:

```
pre_confirmed → confirmed → finalized  (happy path)
pre_confirmed → reorged                (reorg detected)
```

When the facilitator claims a nonce, it records `source="facilitator"` and
the pre-generated `event_id` in the nonces table. When Goldsky later delivers
the real onchain event for the same nonce, `record_nonce_or_correlate()`
returns the existing event_id and source. The processor calls
`_promote_pre_confirmed_event()` which updates the event row with real
onchain data (tx_hash, block_number, block_hash, log_index) and dispatches
`payment.confirmed` or `payment.finalized` depending on current finality.
The finality poller then handles the `confirmed → finalized` transition.

### 3.3 Dynamic Triggers (same Goldsky path)

Dynamic triggers use the same `/ingest/goldsky` endpoint. The difference is
in event type detection: when a log's topic0 does not match any hardcoded
signature in `_EVENT_SIGNATURES`, the processor falls back to the trigger
registry and queries for matching triggers by topic0, chain_id, and
contract_address.

### 3.4 Goldsky Edge RPC (query plane)

While Goldsky Turbo pushes events to TripWire (data plane), Goldsky Edge
provides the RPC endpoints that TripWire pulls data from (query plane).

```
  tripwire/rpc.py (shared httpx.AsyncClient singleton)
       |  Authenticated via Bearer GOLDSKY_EDGE_API_KEY
       |  Timeout: 10s per request
       |
       +-- eth_blockNumber(chain_id)
       |     Used by: FinalityPoller (per-chain poll loops)
       |     Returns: latest block height
       |
       +-- get_block_hash(chain_id, block_num)  [in finality.py, calls eth_getBlockByNumber]
       |     Used by: FinalityPoller (reorg detection)
       |     Returns: block hash for canonical chain comparison
       |
       +-- eth_call(chain_id, to, data)
             Used by: ERC-8004 identity resolver, reputation service
             Returns: contract read results (agent identity, scores)
```

**RPC URL configuration**: Per-chain URLs are set via `BASE_RPC_URL`,
`ETHEREUM_RPC_URL`, and `ARBITRUM_RPC_URL` environment variables. These
point to Goldsky Edge managed endpoints. The `GOLDSKY_EDGE_API_KEY` is
attached as a Bearer token to all outbound RPC requests.

**Client lifecycle**: The async HTTP client is created lazily on first use
and closed during application shutdown (`close_rpc_client()`).

---

## 4. Processing Pipeline

### 4.1 Event Type Detection and Handler Routing

Event processing follows a two-step dispatch: detection then handler
delegation. The `EventProcessor` is a thin orchestrator -- all
product-specific logic lives in handlers.

**Step 1: Detection** (`_detect_event_type()`):

```python
# tripwire/ingestion/processor.py :: _detect_event_type()

topic0 = topics[0].lower()

1. Check _EVENT_SIGNATURES dict (hardcoded):
   - AuthorizationUsed topic -> "erc3009_transfer"
   - Transfer topic          -> "erc3009_transfer"

2. If no hardcoded match, check trigger registry:
   - TriggerRepository.find_by_topic(topic0)
   - Filter by chain_id and contract_address
   - If matched: return ("dynamic", [triggers])

3. Otherwise: return "unknown" (event is skipped)
```

**Step 2: Handler delegation**:

The processor iterates `self._handlers` (a list of `EventHandler`
protocol implementors) and delegates to the first handler whose
`can_handle()` returns True:

```python
for handler in self._handlers:
    if await handler.can_handle(detection, raw_log):
        return await handler.handle(raw_log, processor, detection)
```

Default handler chain: `[PaymentHandler(), TriggerHandler()]`.
`PaymentHandler` matches `"erc3009_transfer"` detections (Keeper product).
`TriggerHandler` matches `("dynamic", triggers)` tuples (Pulse product).
Handlers receive the `processor` reference to access shared utilities
(repos, webhook provider, identity resolver, caches).

### 4.2 ERC-3009 Pipeline (PaymentHandler)

```
  Raw log
    |
    v
  1. DECODE              ERC3009Decoder().decode()
    |                    Returns DecodedEvent envelope with typed_model=ERC3009Transfer
    |                    (wraps decode_transfer_event() via decoder abstraction, see 9.3)
    |                    ~1ms
    v
  2. DEDUP               nonce_repo.record_nonce_or_correlate()
    |                    Unique constraint: (chain_id, nonce, authorizer)
    |                    Returns (is_new, existing_event_id, existing_source)
    |                    If existing_source="facilitator": promotes pre_confirmed
    |                    event instead of dropping as duplicate (see 4.5)
    |                    Reorg-aware: reorged nonces can be reused
    |                    Uses SELECT FOR UPDATE to prevent TOCTOU races
    |                    ~2-5ms
    v
  3. FINALITY  ----+     check_finality() via JSON-RPC
    |              |     eth_blockNumber -> compute confirmations
    |              |     Accepts optional required_depth override
    |              |     (from EndpointPolicies.finality_depth)
    |              |     ~10-50ms (RPC round-trip)
    |   [parallel] |
  4. IDENTITY  ----+     resolver.resolve() via ERC-8004 registry
    |                    Returns AgentIdentity or None
    |                    ~5-30ms (RPC or cache hit)
    v
  5. ENDPOINT MATCH      list_by_recipient() with 30s TTL cache
    |                    Filter: recipient + chain_id + active
    |                    ~0-2ms (cached) or ~5-10ms (DB)
    v
  6. POLICY EVAL         evaluate_policy() per endpoint
    |                    Checks: min/max amount, sender allowlists,
    |                    agent class, reputation score
    |                    ~<1ms
    v
  6b. FINALITY GATE      Per-endpoint finality_depth check
    |                    EndpointPolicies.finality_depth (int | None)
    |                    None = chain default from FINALITY_DEPTHS
    |                    Defers endpoints whose required depth exceeds
    |                    current confirmations (they wait for poller)
    v
  7. RECORD EVENT        Insert into events table + event_endpoints join
    |                    ~3-8ms
    v
  8a. EXECUTE MODE       dispatch_event() via Convoy
    |                    One message per matched endpoint
    |                    ~20-80ms (Convoy API call)
    |
  8b. NOTIFY MODE        RealtimeNotifier.notify_batch()
                         Insert into realtime_events table
                         Supabase Realtime pushes via WebSocket
                         ~sub-1ms (local DB insert)
```

Steps 3 and 4 (finality and identity) run in parallel via `asyncio.gather`
since they have zero data dependencies on each other.

### 4.3 Dynamic Trigger Pipeline (TriggerHandler)

For each matched trigger:

```
  1. DECODE              AbiGenericDecoder(trigger.abi).decode()
                         Returns DecodedEvent envelope with decoded fields dict
                         (wraps decode_event_with_abi() via decoder abstraction, see 9.3)
  2. FILTER              evaluate_filters(decoded, trigger.filter_rules)
                         JMESPath-based filter engine (AND logic)
  3. DEDUP               tx_hash:log_index:trigger_id as nonce key
  4. IDENTITY            First address field in decoded data
  4b. REPUTATION GATE    If trigger.reputation_threshold > 0:
                         Compare resolved identity's reputation_score against threshold
                         Events from agents below threshold are rejected with
                         status="filtered", reason="reputation_below_threshold"
  5. ENDPOINT FETCH      endpoint_repo.get_by_id(trigger.endpoint_id)
  6. DISPATCH            webhook_provider.send() via Convoy
  7. RECORD              events table + event_endpoints join
```

### 4.4 Batch Processing

The `/ingest/goldsky` endpoint accepts arrays of up to 1000 logs.
`EventProcessor.process_batch()` processes them concurrently with a
semaphore of 10 to bound downstream pressure.

### 4.5 Pre-Confirmed Event Promotion

When Goldsky delivers an onchain event whose nonce was already claimed by
the facilitator fast path, the processor promotes the existing event instead
of dropping it as a duplicate:

```
  Goldsky delivers real onchain event
    |
    v
  1. DECODE              decode_transfer_event()
    |
    v
  2. DEDUP               record_nonce_or_correlate(source="goldsky")
    |                    Returns (False, existing_event_id, "facilitator")
    v
  3. PROMOTE             _promote_pre_confirmed_event()
    |                    Updates event row with real tx_hash, block_number,
    |                    block_hash, log_index via promote_to_confirmed()
    v
  4. FINALITY CHECK      check_finality() on the real block
    |                    Determines: confirmed or already finalized
    v
  5. IDENTITY            resolver.resolve() for the authorizer
    v
  6. ENDPOINT FETCH      Fetches linked endpoints from event_endpoints join table
    v
  7. POLICY EVAL         evaluate_policy() per endpoint
    v
  8. DISPATCH            payment.confirmed or payment.finalized webhook
```

The nonce table's `record_nonce_or_correlate()` function (migration 020)
uses `SELECT FOR UPDATE` to prevent TOCTOU races when two sources claim the
same nonce concurrently.

---

## 5. Execution Guarantees

This section defines TripWire's formal trust model, deduplication semantics,
and delivery guarantees. These properties are what make TripWire suitable for
production financial infrastructure.

### 5.1 Trust Levels

Every event that flows through TripWire carries an implicit trust level.
The trust level determines what actions are safe to take in response.

```
PROVISIONAL (confidence ~0.99)
  → x402 facilitator fast path
  → Signature verified, tx NOT yet onchain
  → Trust boundary: facilitator's signature verification
  → Event type: payment.pre_confirmed

CONFIRMED (confidence 1.0, single block)
  → Goldsky reliable path, 1+ block confirmation
  → Event type: payment.confirmed
  → Safe for: low-value actions, dashboard updates

FINALIZED (confidence 1.0, chain-specific depth)
  → Meets full finality threshold per chain
  → Arbitrum: 1 block (~250ms)
  → Base: 3 blocks (~6s)
  → Ethereum: 12 blocks (~2.5 min)
  → Event type: payment.finalized (dedicated event type)
  → Safe for: irreversible actions, fund transfers

REORGED (confidence 0.0)
  → Block hash mismatch detected by finality poller
  → Nonce invalidated (reorged_at set, reusable)
  → Event type: payment.reorged
  → Action: roll back any provisional actions
```

These trust levels are encoded in the `WebhookEventType` enum
(`tripwire/types/models.py`) and surfaced explicitly in every webhook
payload via a nested `ExecutionBlock` (the `execution` field):

- **`execution.state`**: `ExecutionState` enum -- `provisional`, `confirmed`,
  `finalized`, or `reorged`. Derived from event type and finality data by
  `derive_execution_metadata()`, which returns an `ExecutionBlock`.
- **`execution.safe_to_execute`**: `bool` -- `true` only when `state` is
  `finalized`. Tells the consumer whether irreversible actions are safe.
- **`execution.trust_source`**: `TrustSource` enum -- `facilitator` (pre_confirmed
  events) or `onchain` (all others).
- **`execution.finality`**: `FinalityData | null` -- confirmation count,
  required confirmations, and finalization flag. `null` for pre_confirmed events.

The `WebhookEventType` enum now includes six event types:
`payment.pre_confirmed`, `payment.pending`, `payment.confirmed`,
`payment.finalized`, `payment.failed`, `payment.reorged`.

All payloads also carry `version: "v1"` for forward compatibility.

### 5.1a Execution State Derivation

The function `execution_state_from_status()` in `tripwire/types/models.py`
maps the database `events.status` column to the execution metadata triple
`(ExecutionState, safe_to_execute, TrustSource)` at query time. The related
`derive_execution_metadata()` function returns an `ExecutionBlock` model
(containing `state`, `safe_to_execute`, `trust_source`, and `finality`)
used to populate the nested `execution` field on `WebhookPayload`. Both are
pure derivations -- no schema migration is needed.

```
  events.status         -> ExecutionState    safe_to_execute  TrustSource
  ─────────────────────────────────────────────────────────────────────────
  pre_confirmed         -> provisional       false            facilitator
  pending               -> confirmed         false            onchain
  confirmed             -> confirmed         false            onchain
  finalized             -> finalized         true             onchain
  reorged               -> reorged           false            onchain
```

Applied at API and MCP response boundaries: whenever event data is returned
to callers (REST API responses, MCP tool results), the derived fields are
injected into the payload. This ensures consumers always receive the
execution state without relying on them to interpret raw status strings.

### 5.2 The Two Trust Models

TripWire operates two fundamentally different trust models. The developer
chooses which to act on based on their risk tolerance.

**Goldsky path (chain-derived trust):**

```
event → decode → dedup → finality check → identity → policy → dispatch
```

Trust is derived from the chain itself: block confirmations measured against
`FINALITY_DEPTHS` (Arbitrum: 1, Base: 3, Ethereum: 12). The finality check
calls `eth_blockNumber` via JSON-RPC to compute `confirmations =
current_block - event.block_number`. The guarantee: a `payment.confirmed`
webhook only fires after N confirmations have been observed.

**Facilitator fast path (facilitator-derived trust):**

```
facilitator verifies ERC-3009 signature → TripWire skips decode + finality
  → dedup → identity → policy → dispatch
```

Trust is derived from the facilitator's signature verification, not from
the chain. The transaction has not been mined yet. The guarantee: the
webhook fires in ~100ms, but the event is PROVISIONAL. The real
confirmation comes later when Goldsky delivers the onchain event.

**The fast path trades finality for speed.** The developer chooses which
webhooks to act on:

- Act on `payment.pre_confirmed` (~100ms) for low-risk actions like
  showing a success screen, unlocking a download, or updating a dashboard.
- Wait for `payment.confirmed` (~500ms-13s depending on chain) for
  medium-risk actions like unlocking content or updating balances.
- Wait for `payment.finalized` (chain-specific depth) for irreversible
  actions like transferring funds, minting tokens, or granting permanent
  access. The `safe_to_execute` flag is `true` only on finalized events.
- The developer's endpoint receives up to THREE events for the same payment
  (`pre_confirmed` → `confirmed` → `finalized`). Deduplication across
  the paths is handled via the `idempotency_key` (see below). Each event
  type gets its own key, so all arrive, but replays of the same event
  type are suppressed.

### 5.3 Deduplication Guarantees

TripWire enforces deduplication at two levels:

**Nonce dedup (prevents duplicate processing):**

PostgreSQL UNIQUE constraint on `(chain_id, nonce, authorizer)` in the
`nonces` table. Two PL/pgSQL functions handle deduplication:

- `record_nonce_with_reorg()`: Simple insert-or-reject with reorg reclaim.
- `record_nonce_or_correlate()` (migration 020): Correlation-aware variant
  that returns `(is_new, existing_event_id, existing_source)`. On conflict,
  it uses `SELECT FOR UPDATE` to lock the row before reading, preventing
  TOCTOU races when the facilitator and Goldsky paths claim the same nonce
  concurrently. If the existing nonce was created by the facilitator, the
  Goldsky path promotes the pre_confirmed event instead of dropping it.

**Event dedup (prevents duplicate delivery):**

The `idempotency_key` is a deterministic SHA-256 hash:

```
idempotency_key = SHA256(chain_id : tx_hash : log_index : endpoint_id : event_type)
```

The same event always produces the same key. Because `event_type` is part
of the hash, a `payment.pre_confirmed` and `payment.confirmed` for the
same transaction produce different keys -- both are delivered. But two
deliveries of `payment.confirmed` for the same tx+endpoint are identical
and can be deduplicated by the consumer.

**Reorg recovery:**

When the finality poller detects a reorg (block hash mismatch between the
stored event and the canonical chain), it sets `reorged_at` on the nonce
via `invalidate_by_event_id()`. This allows the re-broadcast transaction
to be processed as a new event when it reappears in a different block.

### 5.4 Delivery Guarantees

- **At-least-once delivery via Convoy**: Convoy retries failed webhook
  deliveries with exponential backoff (10 retries, base duration 10s).
  After exhaustion, the delivery enters Convoy's dead-letter queue.
  TripWire's DLQ handler polls for failed deliveries and retries up to
  3 additional times before marking them `dead_lettered` and firing an
  alert.
- **At-least-once processing via Redis Streams**: When the event bus is
  enabled, consumer groups ensure messages are ACKed only after successful
  processing. Unacked messages are reclaimed by other workers via
  XAUTOCLAIM after 30s idle. Messages that fail 5 times are written to
  the `tripwire:dlq` stream and ACKed from the source.
- **Idempotency keys enable exactly-once semantics at the consumer**:
  TripWire delivers at-least-once, but every webhook payload includes a
  deterministic `idempotency_key`. Consumers that store and check this
  key achieve exactly-once processing semantics.
- **DLQ for permanently failed deliveries**: Both Convoy (webhook-level)
  and Redis Streams (processing-level) have dead-letter mechanisms with
  alerting and manual replay capability.

### 5.5 What TripWire Does NOT Guarantee

- **No exactly-once delivery.** TripWire provides at-least-once delivery
  only. The consumer is responsible for deduplication via the
  `idempotency_key` included in every webhook payload.
- **No distributed finality poller coordination.** The finality poller is
  single-instance only. In a multi-instance deployment, each instance runs
  its own poller, which can produce duplicate confirmation webhooks for the
  same event. Mitigation: run a single instance, or add a Redis/Postgres
  advisory lock (see Known Limitations P1).
- **Pre-confirmed events can strand.** If the facilitator fast path creates
  a `payment.pre_confirmed` event but the transaction never lands onchain,
  the event remains in `pre_confirmed` status indefinitely. The unified
  lifecycle (migration 020) ensures promotion when Goldsky delivers the
  real tx, but if the authorization expires and the tx is never mined, the
  event has no cleanup mechanism yet (see Known Limitations P2).
- **30s cache TTL means new triggers are invisible for up to 30 seconds.**
  The endpoint cache (`processor.py`), trigger topic cache
  (`triggers.py`), and TriggerIndex (`trigger_worker.py`) all use
  in-process TTL caches. A newly created trigger will not be evaluated
  against incoming events until the cache refreshes.

**"Goldsky tells you what happened. TripWire decides if it's safe to act. Convoy makes sure you hear about it."**

---

## 6. Identity Resolution (ERC-8004)

TripWire enriches every event with onchain agent identity from the ERC-8004
registry. This enables reputation-gated webhook delivery and agent
classification.

### 6.1 Two Registry Contracts (CREATE2, same address all chains)

| Contract | Address | Purpose |
|----------|---------|---------|
| IdentityRegistry | `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432` | ERC-721 based. Stores agent_class, deployer, capabilities, tokenURI |
| ReputationRegistry | `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63` | Aggregated reputation scores (0-10000 basis points -> 0-100 float) |

### 6.2 Resolution Pipeline (7 RPC calls via Goldsky Edge)

```
address
  -> balanceOf(address)              [sequential - checks if registered]
  -> tokenOfOwnerByIndex(address, 0) [sequential - gets agent_id]
  -> asyncio.gather(                 [5 parallel calls]
      tokenURI(agent_id),
      getMetadata(agent_id, "agentClass"),
      getMetadata(agent_id, "capabilities"),
      ownerOf(agent_id),
      getSummary(agent_id, [])      [ReputationRegistry]
    )
  -> AgentIdentity {
      address, agent_class, deployer, capabilities,
      reputation_score, registered_at, metadata
    }
```

### 6.3 Caching

- Identity cache: in-process dict, 300s TTL for hits, 30s for misses
- Reputation cache: in-process dict, 300s TTL
- Cache key: `{chain_id}:{address}`

### 6.4 Where Identity Is Used

1. **Pipeline enrichment**: Every webhook payload includes `data.identity` (AgentIdentity or null)
2. **Endpoint policies**: `required_agent_class` and `min_reputation_score` filter webhooks
3. **Dynamic trigger reputation gating**: Triggers with `reputation_threshold > 0` evaluate the resolved agent identity's reputation score against the threshold. Events from agents scoring below the threshold are rejected with status `"filtered"`, reason `"reputation_below_threshold"`. Previously this field existed on the Trigger model but was never evaluated (dead code); it is now enforced at step 4b of the dynamic trigger pipeline (see Section 4.3).
4. **MCP reputation gating**: Per-tool `min_reputation` threshold checked before execution
5. **Audit context**: Agent identity logged with every event

### 6.5 Mock vs Production

- Production: `ERC8004Resolver` — real onchain RPC calls
- Development: `MockResolver` — 3 hardcoded agents (trading-bot, data-oracle, payment-agent)

---

## 7. Delivery Layer

### 7.1 Execute Mode: Convoy (webhook POST)

All execute-mode delivery is routed through Convoy. There is NO direct
httpx delivery path in the current codebase. Direct httpx fast-path
delivery is PLANNED but NOT implemented.

```
  EventProcessor
       |
       v
  dispatch_event()             Build WebhookPayload per endpoint
       |                       Deterministic idempotency key:
       |                       sha256(chain_id:tx_hash:log_index:endpoint_id:event_type)
       v
  WebhookProvider.send()       Protocol interface
       |
       v
  ConvoyProvider.send()        POST to Convoy API
       |                       convoy_project_id = endpoint.convoy_project_id
       |                       convoy_endpoint_id = endpoint.convoy_endpoint_id
       v
  Convoy Server                Handles:
       |                       - Exponential backoff retries
       |                       - HMAC signature signing (sole signer)
       |                       - Delivery logging
       |                       - Dead letter queue
       v
  Developer's API endpoint
```

Convoy is the sole HMAC signer. The `webhook_secret` column was dropped
from the endpoints table in migration 016. Secrets are managed entirely
within Convoy.

**Circuit breaker** (`tripwire/webhook/convoy_client.py`):

A module-level state machine protects against Convoy being down:

```
  closed (normal) ──[5 consecutive failures]──> open (reject fast)
                                                    |
                                              [30s recovery timeout]
                                                    |
  closed <──[probe succeeds]── half_open (allow one probe)
                                    |
                              [probe fails]──> open
```

| Parameter          | Value | Description                          |
|--------------------|-------|--------------------------------------|
| FAILURE_THRESHOLD  | 5     | Consecutive failures to trip circuit |
| RECOVERY_TIMEOUT   | 30s   | Wait before allowing probe request   |
| HALF_OPEN_TIMEOUT  | 10s   | Shorter timeout for probe requests   |

All Convoy API calls (`create_application`, `create_endpoint`, `send_webhook`)
check the circuit via `_guard_circuit()` and raise `ConvoyCircuitOpenError`
when the circuit is open. Successful calls reset the circuit to closed.
Circuit state is exposed via `get_circuit_state()` for diagnostics and
tracked as a Prometheus gauge (`tripwire_convoy_circuit_state`).

**Provider abstraction** (`tripwire/webhook/provider.py`):

- `WebhookProvider` -- Protocol interface with `send()`, `create_app()`,
  `create_endpoint()`
- `ConvoyProvider` -- Production implementation backed by Convoy API
- `LogOnlyProvider` -- Development fallback when `CONVOY_API_KEY` is not set

**DLQ Handler** (`tripwire/webhook/dlq_handler.py`):

Background poller that queries Convoy for failed deliveries per endpoint.
Retries up to `dlq_max_retries` (default 3) via `force_resend()`. After
exhaustion, marks the delivery as `dead_lettered` in the local
`webhook_deliveries` table and fires an alert to `dlq_alert_webhook_url`.

### 7.2 Notify Mode: Supabase Realtime

```
  EventProcessor
       |
       v
  RealtimeNotifier.notify_batch()
       |
       v
  INSERT into realtime_events table (bulk)
       |
       v
  Supabase Realtime detects INSERT
       |
       v
  WebSocket push to subscribed clients
```

Latency is sub-1ms from the application's perspective (local DB insert).
Supabase Realtime handles the WebSocket fan-out.

Subscription filtering: each notify-mode endpoint can have subscriptions
with filters (chains, senders, recipients, min_amount, agent_class). If
no subscriptions are defined, the endpoint receives all events
(backwards-compatible).

---

## 8. Event Bus (Redis Streams)

The event bus is an optional async processing layer that provides backpressure between Goldsky ingestion and trigger evaluation. When enabled, events are buffered in Redis Streams partitioned by topic0, allowing horizontal scaling of event processing without overwhelming the database or downstream services. When disabled (default), events flow synchronously through the processor.

Feature-flagged via `EVENT_BUS_ENABLED` (default: `false`). When enabled,
the `/ingest` endpoint publishes to Redis Streams instead of processing
synchronously. When disabled, events are processed inline through
`EventProcessor`.

### 8.1 Architecture

```
  /ingest/goldsky
       |
       v
  EVENT_BUS_ENABLED?
       |
      YES -----> publish_batch() -----> Redis Streams
       |                                     |
       |                          +----------+----------+
       |                          |          |          |
       |                       worker-0  worker-1  worker-2
       |                          |          |          |
       |                          +----------+----------+
       |                                     |
       |                          EventProcessor.process_event()
       |
      NO ------> EventProcessor.process_batch() (synchronous)
```

### 8.2 Stream Topology

Streams are keyed by topic0 (the keccak256 hash of the event signature):

```
  tripwire:events:0xabc123...   (one stream per unique event type)
  tripwire:events:0xdef456...
  tripwire:events:unknown        (fallback for invalid/capped topics)
  tripwire:dlq                  (dead-letter stream)
```

**Constants:**

| Parameter          | Value   | Description                                 |
|--------------------|---------|---------------------------------------------|
| STREAM_PREFIX      | `tripwire:events:` | Key prefix for event streams     |
| CONSUMER_GROUP     | `trigger-workers`  | Shared consumer group name       |
| MAX_STREAM_LEN     | 100,000 | Max entries per stream (approximate trim)   |
| MAX_STREAMS        | 500     | Global cap on distinct streams              |
| BLOCK_MS           | 2,000   | XREADGROUP block timeout                    |
| CLAIM_IDLE_MS      | 30,000  | XAUTOCLAIM idle threshold                   |

### 8.3 Topic0 Validation

Topics are validated with strict regex: `^0x[0-9a-f]{64}$` (lowercase hex
only). Invalid topics are routed to `tripwire:events:unknown`.

### 8.4 Worker Pool

```
  WorkerPool
    |
    +-- TriggerIndex (shared, refreshes every 10s with lock-guarded double-check)
    |
    +-- worker-0  (assigned streams via round-robin)
    +-- worker-1
    +-- worker-2  (default: EVENT_BUS_WORKERS=3)
    |
    +-- Stream discovery loop (every 30s, scans for new streams)
```

**TriggerIndex**: In-memory dict mapping `topic0 -> [Trigger]`. Rebuilt
from the database every 10 seconds. Uses an asyncio lock with double-check
to prevent concurrent refreshes.

**TriggerWorker**: Each worker runs an XREADGROUP consumer loop:

1. Consume batch from assigned streams
2. Every 10 iterations, claim stale messages via XAUTOCLAIM
3. Process each message through `EventProcessor.process_event()`
4. Batch ACK all successful messages via Redis pipeline
5. On failure: increment per-message failure counter
6. After 5 failures: write to DLQ stream and ACK the source message

**Worker restart**: Crashed workers are automatically restarted with
exponential backoff (base 2s, max 120s). Restart counter resets after 300s
of stability.

### 8.5 Safety Mechanisms

| Mechanism                  | Implementation                                     |
|----------------------------|----------------------------------------------------|
| DLQ stream + consumer      | `tripwire:dlq` -- permanently failed events after 5 retries; consumed by `RedisDLQConsumer` |
| Process event timeout      | 30s timeout on `process_event()` via `asyncio.wait_for`; timeouts count as failures toward retry/DLQ |
| Retry cap                  | Per-message failure count dict; entries pruned after 300s |
| Stream cap (publish)       | `_known_stream_keys` set; excess routed to `unknown` |
| Stream cap (discovery)     | WorkerPool caps at MAX_STREAMS during scan          |
| Per-worker stream cap      | 100 streams max per worker                          |
| Batch size limit           | /ingest rejects payloads > 1000 logs (HTTP 400)     |
| Exponential backoff        | On consecutive consume errors: min(2^n, 60) seconds |
| Batched ACK                | Pipeline ACK for success + DLQ'd messages           |
| Graceful shutdown          | 30s timeout, then cancel                            |
| Fallback on bus failure    | /ingest falls back to synchronous processing        |
| NOGROUP recovery           | Invalidates `_known_groups` cache, re-creates group |
| Poison message handling    | Malformed messages are ACKed immediately             |
| Startup degradation        | If worker pool fails to start, app continues without it |

### 8.6 Consumer Group Semantics

- **At-least-once delivery**: Messages are ACKed only after successful
  processing or DLQ write.
- **XAUTOCLAIM**: Stale messages (idle > 30s) are reclaimed by other
  workers every 10 iterations.
- **Consumer group**: `trigger-workers` -- shared across all workers in
  the pool. Created on-demand with MKSTREAM.

### 8.7 DLQ Consumer

`RedisDLQConsumer` (`tripwire/ingestion/dlq_consumer.py`) is a background
task that reads permanently-failed events from the `tripwire:dlq` stream.
Started only when `EVENT_BUS_ENABLED=true`.

- Uses XREAD (not consumer groups) since it is a single consumer
- Polls every 30s, reads up to 50 messages per cycle
- For each message: logs the dead-lettered event, fires an alert webhook
  to `DLQ_ALERT_WEBHOOK_URL` (if configured), increments a Prometheus
  counter (`tripwire_redis_dlq_total`)
- Trims the DLQ stream to 10,000 entries after processing 100+ messages
- Tracks position via last-seen message ID (survives poll cycles but not
  restarts -- will replay from beginning on restart)

---

## 9. Trigger Registry

The trigger registry allows AI agents to create triggers for any EVM event
via MCP tools or the REST API, without deploying new infrastructure.

### 9.1 Data Model

Three tables, introduced in migration 013:

**trigger_templates** -- Pre-built templates for the Bazaar:

| Column              | Type        | Description                           |
|---------------------|-------------|---------------------------------------|
| id                  | UUID PK     | Auto-generated                        |
| name                | TEXT        | Human-readable name                   |
| slug                | TEXT UNIQUE | URL-safe identifier                   |
| version             | TEXT        | Template version (default: "1.0.0") (migration 025) |
| description         | TEXT        | Template description                  |
| category            | TEXT        | e.g. 'defi', 'payments', 'nft'       |
| event_signature     | TEXT        | Solidity signature (e.g. `Transfer(address,address,uint256)`) |
| topic0              | TEXT        | Precomputed keccak256 hash (migration 017) |
| abi                 | JSONB       | ABI fragment for decoding             |
| default_chains      | JSONB       | Default chain IDs                     |
| default_filters     | JSONB       | Default filter rules                  |
| parameter_schema    | JSONB       | User-configurable parameters          |
| webhook_event_type  | TEXT        | Event type string for webhooks        |
| reputation_threshold| FLOAT       | Min reputation score to use           |
| author_address      | TEXT        | Template author                       |
| is_public           | BOOLEAN     | Visible in Bazaar                     |
| install_count       | BIGINT      | Active installations (balanced counter) |

Seeded templates: `whale-transfer`, `dex-swap`, `nft-mint`,
`erc3009-payment`, `ownership-transfer`.

**triggers** -- Active trigger definitions:

| Column              | Type        | Description                           |
|---------------------|-------------|---------------------------------------|
| id                  | UUID PK     | Auto-generated                        |
| owner_address       | TEXT        | Wallet that created the trigger       |
| endpoint_id         | TEXT FK     | Target endpoint for delivery          |
| name                | TEXT        | Human-readable name                   |
| event_signature     | TEXT        | Solidity event signature              |
| topic0              | TEXT        | Precomputed keccak256 hash (migration 017) |
| abi                 | JSONB       | ABI fragment for decoding             |
| contract_address    | TEXT        | Optional: specific contract to watch  |
| chain_ids           | JSONB       | Chain IDs to monitor (GIN indexed)    |
| filter_rules        | JSONB       | JMESPath filter predicates            |
| webhook_event_type  | TEXT        | Event type string for webhooks        |
| reputation_threshold| FLOAT       | Min reputation score                  |
| required_agent_class| TEXT        | Required ERC-8004 agent class (null = any) (migration 025) |
| version             | TEXT        | Trigger definition version (default: "1.0.0") (migration 025) |
| batch_id            | UUID        | Groups triggers created together      |
| active              | BOOLEAN     | Soft-delete flag                      |

**trigger_instances** -- Template installations (M2M between templates and endpoints):

| Column              | Type        | Description                           |
|---------------------|-------------|---------------------------------------|
| id                  | UUID PK     | Auto-generated                        |
| template_id         | UUID FK     | Source template                       |
| owner_address       | TEXT        | Installing agent                      |
| endpoint_id         | TEXT FK     | Target endpoint                       |
| contract_address    | TEXT        | Optional override                     |
| chain_ids           | JSONB       | Chain override                        |
| parameters          | JSONB       | User-supplied params                  |
| resolved_filters    | JSONB       | Computed filters after param merge    |
| active              | BOOLEAN     | Active flag                           |

Unique partial index: one active instance per (template_id, owner_address).
The `install_count` on `trigger_templates` is maintained by a balanced
INSERT/UPDATE/DELETE trigger function (migration 015).

### 9.2 Lookup Path

```
  topic0 from raw log
       |
       v
  TriggerRepository.find_by_topic(topic0)
       |  Queries: SELECT * FROM triggers WHERE topic0 = $1 AND active = true
       |  Cached with 30s TTL (module-level dict)
       v
  Filter locally by chain_id and contract_address
       |
       v
  [matched triggers]
```

The TriggerIndex (used by event bus workers) maintains an in-memory dict
rebuilt from the database every 10 seconds. Uses the precomputed `topic0`
column (keccak256 hash), added in migration 017.

### 9.3 Decoder Abstraction (Phase C1)

The `tripwire/ingestion/decoders/` package provides a unified decoder
interface that all event processing flows through. This is Phase C1 of a
planned three-phase unification.

**Package layout:**

```
tripwire/ingestion/decoders/
  __init__.py        — re-exports all public names
  protocol.py        — Decoder protocol + DecodedEvent dataclass
  erc3009.py         — ERC3009Decoder (wraps decode_transfer_event)
  abi_generic.py     — AbiGenericDecoder (wraps decode_event_with_abi)
```

**`Decoder` protocol** (`protocol.py`, `@runtime_checkable`):

| Method/Property | Signature                                       | Description                    |
|-----------------|-------------------------------------------------|--------------------------------|
| `name`          | `-> str`                                        | Decoder identifier             |
| `can_decode()`  | `(raw_log) -> bool`                             | Check if this decoder handles the log |
| `decode()`      | `(raw_log, ...) -> DecodedEvent`                | Decode raw log into envelope   |

**`DecodedEvent` dataclass** (`protocol.py`) -- unified output envelope:

| Field              | Type              | Description                              |
|--------------------|-------------------|------------------------------------------|
| tx_hash            | str               | Transaction hash                         |
| block_number       | int               | Block number                             |
| block_hash         | str               | Block hash                               |
| log_index          | int               | Log index within transaction             |
| chain_id           | int               | Chain ID                                 |
| contract_address   | str               | Emitting contract address                |
| topic0             | str               | Event signature hash                     |
| fields             | dict              | Decoded event fields                     |
| raw_log            | dict              | Original raw log data                    |
| decoder_name       | str               | Which decoder produced this              |
| typed_model        | object \| None    | Typed Pydantic model (e.g. ERC3009Transfer) |
| identity_address   | str \| None       | Address to resolve identity for          |
| dedup_key          | str \| None       | Deduplication key                        |

**Concrete decoders:**

- **`ERC3009Decoder`** (`erc3009.py`): Wraps existing `decode_transfer_event()`. Sets `typed_model` to `ERC3009Transfer`, `identity_address` to the authorizer.
- **`AbiGenericDecoder(abi_fragment)`** (`abi_generic.py`): Wraps existing `decode_event_with_abi()`. Stores the full decoded dict in `fields`. Accepts any ABI event fragment; decodes indexed params from topics[1:] and non-indexed params from data via `eth_abi.decode()`. Now performs best-effort payment field extraction (`_extract_payment_fields`): scans decoded fields for amount-like, from-like, and to-like keys and populates `payment_amount`, `payment_token`, `payment_from`, `payment_to` on the `DecodedEvent`. This enables C3 payment gating to work for dynamic triggers, not just ERC-3009 events.

**Integration**: `processor.py` now calls `ERC3009Decoder().decode()` and
`AbiGenericDecoder(trigger.abi).decode()` instead of invoking the raw
decode functions directly. The raw functions remain available for backward
compatibility but the processor routes through the decoder abstraction.

**Planned phases:**

| Phase | Status          | Description                                          |
|-------|-----------------|------------------------------------------------------|
| C1    | **Implemented** | Decoder protocol + DecodedEvent envelope + two concrete decoders |
| C2    | **Implemented** | Unified processing loop (feature-flagged) -- single code path for ERC-3009 and dynamic triggers |
| C3    | **Implemented** | Per-trigger payment gating via decoder metadata      |

### 9.4 Unified Processing Loop (Phase C2)

`_process_unified()` in `processor.py` replaces the separate `_process_erc3009_event`
and `_process_dynamic_event` methods with a single code path. Feature-flagged via
`UNIFIED_PROCESSOR=true` (default: false). Legacy split paths remain as fallback.

**What dynamic triggers gain in the unified path:**
- Finality checking (block confirmations via RPC)
- Full policy evaluation (endpoint policies, not just reputation threshold)
- Finality depth gating per endpoint
- Execution state metadata via nested `ExecutionBlock` (`execution.state`, `execution.safe_to_execute`, `execution.trust_source`, `execution.finality`)
- Notify mode support (Supabase Realtime push)
- OpenTelemetry tracing spans
- Prometheus pipeline metrics

**Pipeline stages (unified):**

```
  1. DECODE         ERC3009Decoder or AbiGenericDecoder(trigger.abi)
  2. FILTER         evaluate_filters(decoded.fields, trigger.filter_rules)
  3. PAYMENT GATE   _check_payment_gate(decoded, trigger)           [C3]
  4. DEDUP          nonce_repo (correlation-aware for ERC-3009, simple for dynamic)
  5. FINALITY       check_finality_generic() (works with raw values, no ERC3009Transfer required) in parallel with...
     IDENTITY       resolver.resolve(decoded.identity_address)
  6. REPUTATION     trigger.reputation_threshold gating
  7. ENDPOINT       recipient matching (ERC-3009) or trigger.endpoint_id (dynamic)
     POLICY         evaluate_policy(transfer_data, identity, endpoint.policies)
     FINALITY GATE  endpoint.policies.finality_depth check
  8. DISPATCH       Convoy webhook (execute) or Supabase Realtime (notify)
  9. RECORD         event_repo.insert + link_endpoints
```

### 9.5 Per-Trigger Payment Gating (Phase C3)

Triggers can require that a decoded event contains payment metadata meeting a
minimum threshold before dispatch proceeds. Controlled by three fields on the
`Trigger` model (migration 024):

| Field              | Type     | Description                                    |
|--------------------|----------|------------------------------------------------|
| require_payment    | bool     | Enable payment gating (default: false)         |
| payment_token      | str/null | Required token contract (null = any token)     |
| min_payment_amount | str/null | Minimum amount in smallest unit                |

**DecodedEvent payment fields** (populated by decoders):

| Field          | Populated by                  | Source                                         |
|----------------|-------------------------------|------------------------------------------------|
| payment_amount | ERC3009Decoder                | `transfer.value`                               |
| payment_amount | AbiGenericDecoder (best-effort) | Heuristic scan for amount-like decoded fields |
| payment_token  | ERC3009Decoder                | `transfer.token`                               |
| payment_token  | AbiGenericDecoder (best-effort) | Emitting contract address                     |
| payment_from   | ERC3009Decoder                | `transfer.from_address`                        |
| payment_from   | AbiGenericDecoder (best-effort) | Heuristic scan for from-like decoded fields   |
| payment_to     | ERC3009Decoder                | `transfer.to_address`                          |
| payment_to     | AbiGenericDecoder (best-effort) | Heuristic scan for to-like decoded fields     |

`_check_payment_gate(decoded, trigger)` validates: (1) payment data exists,
(2) token matches if specified, (3) amount >= minimum. Returns `(bool, reason)`.

### 9.6 JMESPath Filter Engine

`evaluate_filters()` in `tripwire/ingestion/filter_engine.py`:

All filters use AND logic. Supported operators:

| Operator  | Description                                    |
|-----------|------------------------------------------------|
| eq        | Equality (case-insensitive for addresses)      |
| neq       | Not equal                                      |
| gt/gte    | Greater than / greater-or-equal (numeric)      |
| lt/lte    | Less than / less-or-equal (numeric)            |
| in        | Value in list                                  |
| not_in    | Value not in list                              |
| between   | Value between [lo, hi] inclusive               |
| contains  | Substring match (case-insensitive)             |
| regex     | Regular expression match                       |
| jmespath  | Full JMESPath expression (must eval to truthy) |

Field paths support JMESPath syntax (e.g. `args.recipient`). Hex values
are auto-converted to Decimal for numeric comparisons.

---

## 10. Database Schema

All tables live in Supabase-managed PostgreSQL. Row Level Security is
enabled on `endpoints`, `subscriptions`, `events`, and `webhook_deliveries`
(migration 011) using the session variable `app.current_wallet`.

### 10.1 Core Tables

```
  +------------------+       +------------------+       +------------------+
  |   endpoints      |       |  subscriptions   |       |  events          |
  +------------------+       +------------------+       +------------------+
  | id          PK   |<------| endpoint_id  FK  |       | id          PK   |
  | url              |       | filters    JSONB |       | type             |
  | mode             |       | active           |       | data       JSONB |
  | chains     JSONB |       | created_at       |       | chain_id         |
  | recipient        |       +------------------+       | tx_hash          |
  | owner_address    |                                  | block_number     |
  | policies   JSONB |       +------------------+       | block_hash       |
  | active           |       | event_endpoints  |       | log_index        |
  | convoy_project_id|       +------------------+       | from_address     |
  | convoy_endpoint_id|      | event_id     FK  |------>| to_address       |
  | registration_tx  |       | endpoint_id  FK  |------>| amount           |
  | registration_chain|      | created_at       |       | authorizer       |
  | created_at       |       +------------------+       | nonce            |
  | updated_at       |              (M2M join)          | token            |
  +--------+---------+                                  | status           |
           |                                            | finality_depth   |
           |        +------------------+                | identity_data    |
           |        | webhook_deliveries|               | endpoint_id (legacy)|
           |        +------------------+                | confirmed_at     |
           +------->| endpoint_id  FK  |                | created_at       |
                    | event_id     FK  |                +------------------+
                    | provider_msg_id  |
                    | status           |
                    | created_at       |
                    +------------------+
```

### 10.2 endpoints

| Column              | Type        | Notes                                  |
|---------------------|-------------|----------------------------------------|
| id                  | TEXT PK     |                                        |
| url                 | TEXT        | Webhook target URL                     |
| mode                | TEXT        | 'notify' or 'execute'                  |
| chains              | JSONB       | Array of chain IDs to monitor          |
| recipient           | TEXT        | Watched recipient address              |
| owner_address       | TEXT        | Wallet that registered (migration 010) |
| policies            | JSONB       | EndpointPolicies object                |
| active              | BOOLEAN     | Soft-delete flag                       |
| convoy_project_id   | TEXT        | Convoy project ID (migration 008)      |
| convoy_endpoint_id  | TEXT        | Convoy endpoint ID (migration 008)     |
| registration_tx_hash| TEXT        | x402 payment tx (migration 010)        |
| registration_chain_id| INTEGER   | Payment chain (migration 010)          |
| created_at          | TIMESTAMPTZ |                                        |
| updated_at          | TIMESTAMPTZ |                                        |

Key indexes: `recipient`, `owner_address`, `(recipient) WHERE active`,
`(active) WHERE active`, `mode`.

Dropped columns: `api_key_hash` (migration 010), `webhook_secret`
(migration 016).

### 10.3 events

| Column              | Type        | Notes                                  |
|---------------------|-------------|----------------------------------------|
| id                  | TEXT PK     |                                        |
| type                | TEXT        | e.g. 'payment.confirmed' (migration 002)|
| data                | JSONB       | Full event payload (migration 002)     |
| chain_id            | INTEGER     |                                        |
| tx_hash             | TEXT        | Nullable (migration 026)               |
| block_number        | BIGINT      | Nullable (migration 002)               |
| block_hash          | TEXT        | Nullable                               |
| log_index           | INTEGER     | Nullable                               |
| from_address        | TEXT        | Nullable                               |
| to_address          | TEXT        | Nullable                               |
| amount              | TEXT        | String for USDC 6-decimal precision    |
| authorizer          | TEXT        | Nullable                               |
| nonce               | TEXT        | Nullable                               |
| token               | TEXT        | Nullable                               |
| status              | TEXT        | 'pre_confirmed', 'pending', 'confirmed', 'finalized', 'reorged' |
| finality_depth      | INTEGER     | Current confirmation count             |
| identity_data       | JSONB       | Resolved AgentIdentity                 |
| endpoint_id         | TEXT        | Legacy single-endpoint FK              |
| event_type          | TEXT        | Semantic type, e.g. 'erc3009.transfer' (migration 026, default 'erc3009.transfer') |
| decoded_fields      | JSONB       | Generic decoded event data for Pulse triggers (migration 026, default '{}') |
| source              | TEXT        | 'onchain', 'facilitator', 'manual' (migration 026, default 'onchain') |
| trigger_id          | TEXT        | Nullable FK to triggers (Pulse events only, migration 026) |
| product_source      | TEXT        | 'keeper' or 'pulse' (migration 026, default 'keeper') |
| confirmed_at        | TIMESTAMPTZ |                                        |
| created_at          | TIMESTAMPTZ |                                        |

Key indexes: `(chain_id, tx_hash)`, `to_address`, `from_address`,
`authorizer`, `(chain_id, nonce, authorizer)`, `status`,
`(chain_id, block_number)`, `type`, `(endpoint_id, created_at DESC)`,
`event_type`, `(trigger_id) WHERE trigger_id IS NOT NULL`,
`product_source`, `(event_type, created_at DESC)`.

### 10.4 event_endpoints (M2M join table, migration 014)

| Column      | Type        | Notes                                       |
|-------------|-------------|---------------------------------------------|
| event_id    | TEXT FK PK  | References events(id)                       |
| endpoint_id | TEXT FK PK  | References endpoints(id)                    |
| created_at  | TIMESTAMPTZ |                                             |

Resolves the issue where `events.endpoint_id` only recorded the first
matched endpoint. Backfilled from existing data on creation.

### 10.5 nonces (deduplication)

| Column      | Type        | Notes                                       |
|-------------|-------------|---------------------------------------------|
| chain_id    | INTEGER     |                                             |
| nonce       | TEXT        |                                             |
| authorizer  | TEXT        |                                             |
| event_id    | TEXT        | Links to originating event (migration 018)  |
| source      | TEXT        | `"facilitator"` or `"goldsky"` (migration 020) |
| reorged_at  | TIMESTAMPTZ | Set when reorg invalidates (migration 018)  |
| created_at  | TIMESTAMPTZ |                                             |

UNIQUE constraint: `(chain_id, nonce, authorizer)`.

Reorg-aware dedup (migration 018): The `record_nonce_with_reorg()` PL/pgSQL
function checks for reorged nonces and allows reuse. Also checks the
`nonces_archive` table for archived entries.

Correlation-aware dedup (migration 020): The `record_nonce_or_correlate()`
function returns `(is_new, existing_event_id, existing_source)` on conflict.
Uses `SELECT FOR UPDATE` to lock the row, preventing TOCTOU races. This
enables the Goldsky path to detect that the facilitator already claimed a
nonce and promote the pre_confirmed event to confirmed.

### 10.6 nonces_archive (migration 019)

| Column      | Type        | Notes                                       |
|-------------|-------------|---------------------------------------------|
| chain_id    | INTEGER     |                                             |
| nonce       | TEXT        |                                             |
| authorizer  | TEXT        |                                             |
| event_id    | TEXT        |                                             |
| reorged_at  | TIMESTAMPTZ |                                             |
| created_at  | TIMESTAMPTZ |                                             |
| archived_at | TIMESTAMPTZ | When the nonce was moved to archive         |

The `archive_old_nonces()` PL/pgSQL function moves confirmed nonces older
than 30 days (default) in batches of 5000, using `FOR UPDATE SKIP LOCKED`
for concurrency safety. The `NonceArchiver` background task runs this daily.

### 10.7 webhook_deliveries

| Column              | Type        | Notes                                  |
|---------------------|-------------|----------------------------------------|
| id                  | TEXT PK     |                                        |
| endpoint_id         | TEXT FK     |                                        |
| event_id            | TEXT FK     |                                        |
| provider_message_id | TEXT        | Convoy message/delivery ID             |
| status              | TEXT        | 'pending', 'sent', 'failed', 'dead_lettered' |
| dlq_retry_count     | INTEGER     | Persistent DLQ retry count (migration 021, default 0) |
| created_at          | TIMESTAMPTZ |                                        |

Key indexes: `(endpoint_id, status, created_at DESC)`,
`(event_id, created_at DESC)`, `provider_message_id WHERE NOT NULL`.

### 10.8 subscriptions

| Column      | Type        | Notes                                       |
|-------------|-------------|---------------------------------------------|
| id          | TEXT PK     |                                             |
| endpoint_id | TEXT FK     |                                             |
| filters     | JSONB       | SubscriptionFilter (chains, senders, etc.)  |
| active      | BOOLEAN     |                                             |
| created_at  | TIMESTAMPTZ |                                             |

### 10.9 audit_log (migration 012)

| Column              | Type        | Notes                                     |
|---------------------|-------------|-------------------------------------------|
| id                  | UUID PK     | Auto-generated                            |
| action              | TEXT        | e.g. 'mcp.tools.create_trigger'           |
| actor               | TEXT        | Wallet address or 'anonymous'             |
| resource_type       | TEXT        |                                           |
| resource_id         | TEXT        |                                           |
| details             | JSONB       | Tool arguments, auth tier, etc.           |
| ip_address          | TEXT        |                                           |
| execution_latency_ms| INTEGER     | End-to-end execution latency (migration 022) |
| created_at          | TIMESTAMPTZ |                                           |

### 10.10 realtime_events

Used by Supabase Realtime for notify-mode delivery. Rows are inserted by
`RealtimeNotifier` and automatically pushed to WebSocket subscribers.

| Column      | Type        | Notes                                       |
|-------------|-------------|---------------------------------------------|
| id          | TEXT PK     |                                             |
| endpoint_id | TEXT        |                                             |
| type        | TEXT        | Event type string                           |
| data        | JSONB       | Transfer + finality + identity              |
| chain_id    | INTEGER     |                                             |
| recipient   | TEXT        |                                             |
| created_at  | TIMESTAMPTZ |                                             |

### 10.11 Trigger Registry Tables

See Section 9.1 for `trigger_templates`, `triggers`, and
`trigger_instances`.

### 10.12 agent_metrics (materialized view, migration 023)

Aggregated per-agent metrics, refreshed periodically. Created by migration
`023_agent_metrics_view.sql`.

| Column                | Type    | Description                              |
|-----------------------|---------|------------------------------------------|
| agent_address         | TEXT    | Agent wallet address (grouping key)      |
| total_events          | BIGINT  | Total events associated with this agent  |
| finalized_events      | BIGINT  | Events that reached finalized status     |
| successful_deliveries | BIGINT  | Webhook deliveries with status 'sent'    |
| active_triggers       | BIGINT  | Currently active triggers owned by agent |

This is a materialized view (not a table), so it must be refreshed via
`REFRESH MATERIALIZED VIEW agent_metrics` to reflect current data.

---

## 11. MCP Server

Mounted at `/mcp` as a FastAPI sub-application. Implements JSON-RPC 2.0
per the Model Context Protocol specification (version `2024-11-05`).

### 11.1 Authentication Tiers

```
  PUBLIC    No auth required (initialize, tools/list)
  SIWX      Wallet signature via SIWE (free tools)
  SESSION   Pre-funded session with budget (bypasses per-call x402)
  X402      Per-call x402 micropayment (paid tools, hooks-based lifecycle via x402_tool_executor)
```

The four tiers are ordered by increasing trust and cost. The `tools/call`
handler checks for a session token BEFORE falling through to X402
payment verification. This means an agent with an active session can call
paid tools without per-call payment negotiation.

Per-address rate limiting: 60 calls/minute via Redis counter. Fails open
if Redis is unavailable.

Reputation gating: Tools with `min_reputation > 0` require the caller's
ERC-8004 reputation score to meet the threshold.

### 11.1a Session System (Keeper)

Sessions provide a pre-authorized spending limit so agents can make
multiple MCP tool calls without per-call x402 payment negotiation.
Feature-flagged via `SESSION_ENABLED` (default: false). Sessions are
currently free to create (gated only by SIWE auth); the budget is a
server-side spending limit, not a prepayment.

**Architecture:**

```
  Agent
    |
    +-- POST /auth/session (SIWE auth) ───> SessionManager.create()
    |     Returns: session_id, budget, expires_at
    |
    +-- POST /mcp (X-TripWire-Session: <session_id>)
    |     │
    |     ├── _verify_session() ──> SessionManager.validate_and_decrement()
    |     │     Atomic Lua script: check existence + expiry + budget → decrement
    |     │     Returns MCPAuthContext with auth_tier=SESSION
    |     │
    |     ├── Reputation check (cached from session creation)
    |     ├── Rate limit check
    |     ├── Tool execution
    |     └── On failure: SessionManager.refund() returns budget
    |
    +-- GET /auth/session/{id} (SIWE auth) ───> SessionManager.get()
    |     Returns: current budget_remaining, status
    |
    +-- DELETE /auth/session/{id} (SIWE auth) ───> SessionManager.close()
          Returns: final state, removes from Redis
```

**SessionManager** (`tripwire/session/manager.py`):

All state lives in Redis. Each session is a Redis hash at
`session:{session_id}` with fields: `wallet_address`, `budget_total`,
`budget_remaining`, `expires_at`, `ttl_seconds`, `chain_id`,
`reputation_score`, `agent_class`, `created_at`.

**Atomic Lua decrement**: The critical `validate_and_decrement()` operation
uses a pre-loaded Lua script that runs atomically inside Redis:

```
1. Check session existence (HGET) → -1 if not found
2. Check expiry (expires_at vs now) → -2 if expired
3. Check budget (budget_remaining vs cost) → -3 if insufficient
4. Decrement budget_remaining → return new balance
```

This prevents race conditions when an agent makes concurrent tool calls.

**Session data** (`SessionData` dataclass):

| Field             | Type    | Description                              |
|-------------------|---------|------------------------------------------|
| session_id        | str     | URL-safe random token (24 bytes)         |
| wallet_address    | str     | Verified wallet (from SIWE at creation)  |
| budget_total      | int     | Total budget in smallest USDC units      |
| budget_remaining  | int     | Current remaining budget                 |
| expires_at        | float   | Unix timestamp of expiry                 |
| ttl_seconds       | int     | Session lifetime                         |
| chain_id          | int     | Chain ID context                         |
| reputation_score  | float   | Cached from identity at creation         |
| agent_class       | str     | Cached from identity at creation         |

**Refund semantics**: If a tool call fails (reputation gate, execution
error), the session budget is refunded via `HINCRBY`. This is a
best-effort operation -- if the refund itself fails, the budget loss is
logged but not retried.

**Redis TTL**: Each session hash has a Redis-level `EXPIRE` set to
`ttl_seconds + 60` (slightly beyond the session's logical expiry) to
ensure automatic cleanup even if the agent never calls `DELETE`.

### 11.2 Tool Registry

| Tool               | Auth Tier | Price   | Product | Description                           |
|--------------------|-----------|---------|---------|---------------------------------------|
| register_middleware| X402      | $0.003  | keeper  | Create endpoint + triggers in one call|
| create_trigger     | X402      | $0.003  | pulse   | Create custom trigger for endpoint    |
| list_triggers      | SIWX      | free    | pulse   | List caller's active triggers         |
| delete_trigger     | SIWX      | free    | pulse   | Soft-delete a trigger                 |
| list_templates     | SIWX      | free    | pulse   | Browse Bazaar templates               |
| activate_template  | X402      | $0.001  | pulse   | Instantiate template for endpoint     |
| get_trigger_status | SIWX      | free    | pulse   | Check trigger health + event count    |
| search_events      | SIWX      | free    | both    | Query recent events                   |

The `product` tag on each tool (`ToolDef.product`) controls visibility
based on `PRODUCT_MODE`. Tools tagged `"pulse"` are hidden in Keeper-only
mode, and `"keeper"` tools are hidden in Pulse-only mode. Tools tagged
`"both"` are always visible. X402-tier tools can also be called via an
active session (SESSION tier), bypassing per-call payment negotiation.

### 11.3 x402 Payment Flow

For X402-tier tools that are NOT authenticated via a session, the
`tools/call` handler delegates to `_handle_x402_tool_call()`, which
invokes the `x402_tool_executor()` orchestrator. (If an
`X-TripWire-Session` header is present and `SESSION_ENABLED=true`, the
request is routed to `_handle_session_tool_call()` instead -- see
Section 11.1a.) The payment lifecycle is managed through a hooks pattern:

1. **Verify** -- The x402 SDK (`x402ResourceServer`) verifies the
   `PAYMENT-SIGNATURE` header and checks replay protection via Redis
   key `x402:payment:{hash}:{tool_name}`
2. **`before_execution` hooks** -- `TripWirePaymentHooks.before_execution`
   runs identity resolution (ERC-8004), reputation gating, and rate
   limiting
3. **Tool execution** -- The tool handler executes
4. **`after_execution` hooks** -- `TripWirePaymentHooks.after_execution`
   performs audit logging
5. **Settlement** -- The x402 SDK's `x402ResourceServer.settle()`
   finalizes the payment
6. **`on_settlement_success`** -- Confirms the dedup key
7. **`on_settlement_failure`** -- Cleans up the dedup key so the payer
   can retry, and withholds the tool result

The x402 SDK handles protocol-level verify/settle and
`PAYMENT-REQUIRED`/`PAYMENT-RESPONSE` headers automatically. TripWire
adds its unique value -- identity resolution, reputation gating, rate
limiting, execution states, and audit logging -- via the
`TripWirePaymentHooks` class.

`build_auth_context()` handles only PUBLIC and SIWX tiers; X402
authentication is fully delegated to the x402 SDK + hooks pattern.

Multi-chain support: tools declare `x402_networks: list[str]` (replacing
the former singular `x402_network: str`), and `ToolDef.networks: list[str]`
(replacing `ToolDef.network: str`). Payments are accepted on Base,
Ethereum, and Arbitrum.

### 11.4 x402 Bazaar

The legacy `/.well-known/x402-manifest.json` endpoint now returns
**410 Gone**. Service discovery is served by `GET /discovery/resources`
(the x402 V2 Bazaar endpoint).

---

## 12. Finality Poller

Background task that promotes pending events to confirmed once they reach
the required block depth. Spawns one asyncio task per chain. All RPC calls
go through Goldsky Edge managed endpoints (see Section 3.4).

### 12.1 Per-Chain Configuration

| Chain    | Chain ID | Finality Depth | Poll Interval | Block Time |
|----------|----------|----------------|---------------|------------|
| Arbitrum | 42161    | 1 confirmation | 5s            | ~250ms     |
| Base     | 8453     | 3 confirmations| 10s           | ~2s        |
| Ethereum | 1        | 12 confirmations| 30s          | ~12s       |

### 12.2 Poll Cycle

The poller queries events with status IN (`pending`, `confirmed`) that have
`block_number > 0`. This excludes `pre_confirmed` events (which have
`block_number=0` since they have no onchain tx yet) and picks up `confirmed`
events that need finalization promotion.

For each event on a given chain:

1. Fetch current block number via `eth_blockNumber` JSON-RPC (once per cycle)
2. Fetch canonical block hashes for unique block numbers (batch, for reorg detection)
3. Compare stored `block_hash` vs canonical hash:
   - **Mismatch**: reorg detected -- mark event `reorged`, invalidate nonce,
     dispatch `payment.reorged` webhook to all linked endpoints
   - **Match**: compute `confirmations = current_block - event.block_number`
4. Two finality transitions:
   - **pending → confirmed**: first finality threshold crossed; dispatch
     `payment.confirmed` webhook
   - **confirmed → finalized**: full finality reached; dispatch
     `payment.finalized` webhook
5. Otherwise: update `finality_depth` column (for dashboard visibility)

### 12.3 Reorg Handling

When a reorg is detected:

1. Event status set to `reorged`
2. Nonce invalidated via `nonce_repo.invalidate_by_event_id()` so it can
   be reused when the event re-appears
3. `payment.reorged` webhook dispatched to all linked endpoints via the
   `event_endpoints` join table

---

## 13. Latency Map

Estimated end-to-end latencies from event emission to webhook delivery:

### 13.1 x402 Facilitator Fast Path

| Stage              | Latency     | Notes                             |
|--------------------|-------------|-----------------------------------|
| Facilitator notify | ~10ms       | HTTP POST to TripWire             |
| Dedup              | ~2-5ms      | Supabase nonce insert             |
| Identity           | ~5-30ms     | ERC-8004 lookup (or cache)        |
| Policy eval        | <1ms        | In-memory evaluation              |
| Convoy dispatch    | ~20-80ms    | Convoy API call                   |
| **Total**          | **~40-125ms**|                                  |

### 13.2 Goldsky Reliable Path (ERC-3009)

| Stage              | Latency     | Notes                             |
|--------------------|-------------|-----------------------------------|
| Block to Goldsky   | ~1-4s       | Goldsky Turbo indexing             |
| Decode             | ~1ms        | In-process ABI decode             |
| Dedup              | ~2-5ms      | Supabase nonce insert             |
| Finality + Identity| ~10-50ms    | Parallel: RPC call + ERC-8004     |
| Endpoint match     | ~0-2ms      | 30s TTL cache                     |
| Policy eval        | <1ms        | In-memory                         |
| Convoy dispatch    | ~20-80ms    | Convoy API call                   |
| **Total**          | **~1.0-4.2s**| Dominated by Goldsky indexing    |

### 13.3 Dynamic Trigger Path

| Stage              | Latency     | Notes                             |
|--------------------|-------------|-----------------------------------|
| Block to Goldsky   | ~1-4s       | Goldsky Turbo indexing             |
| ABI decode         | ~1-5ms      | Generic eth-abi decode            |
| Filter evaluation  | ~1-2ms      | JMESPath engine                   |
| Dedup              | ~2-5ms      | Supabase nonce insert             |
| Identity           | ~5-30ms     | ERC-8004 lookup (or cache)        |
| Endpoint fetch     | ~5-10ms     | Single endpoint by ID             |
| Convoy dispatch    | ~20-80ms    | Convoy API call                   |
| **Total**          | **~1.0-4.2s**| Dominated by Goldsky indexing    |

### 13.4 Per-Chain Finality Promotion

Additional time from first delivery (pending) to confirmed webhook:

| Chain    | Finality Depth | Typical Time to Finality        |
|----------|----------------|---------------------------------|
| Arbitrum | 1 block        | ~250ms + 5s poll interval       |
| Base     | 3 blocks       | ~6s + 10s poll interval         |
| Ethereum | 12 blocks      | ~144s + 30s poll interval       |

---

## 14. Known Limitations

### PENDING Issues

**P1. Finality poller has no distributed lock.**
Multiple TripWire instances will each run their own finality poller,
causing duplicate confirmation webhooks for the same event. Mitigation:
run a single instance, or add a Redis/Postgres advisory lock.

**P2. Stranded pre_confirmed events.**
If the facilitator fast path creates a `pre_confirmed` event but the
transaction never lands onchain, the event stays in `pre_confirmed` status
indefinitely. The unified lifecycle (migration 020) ensures that when
Goldsky delivers the real tx, the event is promoted. However, if the tx
is NEVER mined (e.g. authorization expired), the event still has no
cleanup mechanism. Needs a TTL-based sweeper for pre_confirmed events
older than a configurable threshold.

**P3. In-process caches are not shared across instances.**
The endpoint cache (30s TTL in `processor.py`), trigger topic cache
(30s TTL in `triggers.py`), and the TriggerIndex (10s refresh in
`trigger_worker.py`) are all in-process dicts. Multiple instances will
have independent caches with potential staleness after writes.

**P4. No per-wallet trigger cap.**
A single wallet can create an unlimited number of triggers. Combined
with the 500-stream cap, this could starve other users. Needs a
per-owner-address limit enforced at the API/MCP layer.

**P5. Identity cache is in-process only.**
The ERC-8004 identity resolver caches results in-process with a
configurable TTL (default 300s). In a multi-instance deployment, each
instance makes redundant RPC calls. A shared Redis cache would reduce
RPC load.

### RESOLVED Issues

| Issue | Description                                     | Resolution           |
|-------|-------------------------------------------------|----------------------|
| #7    | events.endpoint_id only records first match     | Migration 014 (event_endpoints join table) |
| #8    | DB breach exposes webhook signing secrets        | Migration 016 (dropped webhook_secret; Convoy is sole HMAC signer) |
| #9    | topic0 key mismatch (human sig vs keccak hash)  | Migration 017 (precomputed topic0 column) |
| #14   | Gameable install_count on templates              | Migration 015 (balanced trigger + unique partial index) |
| #15   | nonces table grows forever                       | Migration 019 (nonces_archive + daily archival task) |
| #2    | Reorged nonces unrecoverable                     | Migration 018 (reorged_at column + reorg-aware dedup function) |
| #16   | Facilitator-claimed nonce silently drops Goldsky event | Migration 020 (unified event lifecycle: `record_nonce_or_correlate` with SELECT FOR UPDATE + event promotion) |
| #17   | Nonce TOCTOU race between facilitator and Goldsky | Migration 020 (`SELECT FOR UPDATE` in `record_nonce_or_correlate`) |
| #18   | Redis DLQ stream never consumed                  | `RedisDLQConsumer` in `tripwire/ingestion/dlq_consumer.py` |
| #19   | DLQ retry counts lost on restart                 | Migration 021 (`dlq_retry_count` column on webhook_deliveries) |

---

## 15. Configuration Reference

All settings are loaded via `pydantic-settings` from environment variables
or `.env` file. See `tripwire/config/settings.py`.

| Variable                      | Default                | Description                          |
|-------------------------------|------------------------|--------------------------------------|
| PRODUCT_MODE                  | both                   | Product mode: pulse, keeper, or both |
| APP_ENV                       | production             | development / production             |
| APP_PORT                      | 3402                   | HTTP listen port                     |
| APP_BASE_URL                  | http://localhost:3402  | Public base URL                      |
| LOG_LEVEL                     | info                   | Logging level                        |
| SUPABASE_URL                  | (required in prod)     | Supabase project URL                 |
| SUPABASE_SERVICE_ROLE_KEY     | (required in prod)     | Supabase service role key            |
| CONVOY_API_KEY                | (required in prod)     | Convoy API key                       |
| CONVOY_URL                    | http://localhost:5005  | Convoy server URL                    |
| GOLDSKY_WEBHOOK_SECRET        |                        | Validates inbound Goldsky Turbo webhooks |
| GOLDSKY_EDGE_API_KEY          |                        | Goldsky Edge RPC auth (Bearer token) |
| FACILITATOR_WEBHOOK_SECRET    |                        | Validates x402 facilitator webhooks  |
| BASE_RPC_URL                  |                        | Base chain RPC (Goldsky Edge endpoint) |
| ETHEREUM_RPC_URL              |                        | Ethereum RPC (Goldsky Edge endpoint) |
| ARBITRUM_RPC_URL              |                        | Arbitrum RPC (Goldsky Edge endpoint) |
| EVENT_BUS_ENABLED             | false                  | Enable Redis Streams event bus       |
| EVENT_BUS_WORKERS             | 3                      | Number of trigger workers            |
| REDIS_URL                     | redis://localhost:6379 | Redis connection URL                 |
| FINALITY_POLLER_ENABLED       | true                   | Enable finality confirmation poller  |
| FINALITY_POLL_INTERVAL_ARBITRUM| 5                     | Seconds between Arbitrum polls       |
| FINALITY_POLL_INTERVAL_BASE   | 10                     | Seconds between Base polls           |
| FINALITY_POLL_INTERVAL_ETHEREUM| 30                    | Seconds between Ethereum polls       |
| DLQ_ENABLED                   | true                   | Enable Convoy DLQ handler            |
| DLQ_POLL_INTERVAL_SECONDS     | 60                     | DLQ poll interval                    |
| DLQ_MAX_RETRIES               | 3                      | Max Convoy retries before dead-letter|
| DLQ_ALERT_WEBHOOK_URL         |                        | URL for DLQ alert webhooks (Redis + Convoy) |
| IDENTITY_CACHE_TTL            | 300                    | ERC-8004 cache TTL (seconds)         |
| TRIPWIRE_TREASURY_ADDRESS     | (required in prod)     | x402 payment recipient               |
| X402_FACILITATOR_URL          | https://x402.org/facilitator | Facilitator endpoint          |
| X402_REGISTRATION_PRICE       | $1.00                  | Endpoint registration price          |
| X402_NETWORKS                 | ["eip155:8453"]        | Payment networks (multi-chain list)  |
| SIWE_DOMAIN                   | tripwire.dev           | SIWE domain for auth                 |
| AUTH_TIMESTAMP_TOLERANCE_SECONDS| 300                  | SIWE timestamp tolerance             |
| SESSION_ENABLED               | false                  | Enable Redis-backed session system   |
| SESSION_DEFAULT_TTL_SECONDS   | 900                    | Default session lifetime (15 min)    |
| SESSION_MAX_TTL_SECONDS       | 1800                   | Maximum session lifetime (30 min)    |
| SESSION_DEFAULT_BUDGET_USDC   | 10000000               | Default budget: 10 USDC (6 decimals) |
| SESSION_MAX_BUDGET_USDC       | 100000000              | Maximum budget: 100 USDC             |
| OTEL_ENABLED                  | false                  | Enable OpenTelemetry tracing         |
| SENTRY_DSN                    |                        | Sentry error tracking DSN            |
| METRICS_BEARER_TOKEN          |                        | Protect /metrics endpoint            |

---

## 16. Background Tasks

The application lifespan starts and stops these background tasks:

| Task             | Condition                        | Interval  | Description                    |
|------------------|----------------------------------|-----------|--------------------------------|
| FinalityPoller   | FINALITY_POLLER_ENABLED          | Per-chain | Promotes pending→confirmed→finalized |
| WorkerPool       | EVENT_BUS_ENABLED                | Continuous| Redis Streams consumers        |
| RedisDLQConsumer | EVENT_BUS_ENABLED                | 30s       | Consumes dead-lettered events from tripwire:dlq |
| DLQHandler       | DLQ_ENABLED + CONVOY_API_KEY set | 60s       | Retries failed Convoy deliveries|
| NonceArchiver    | Always                           | 24h       | Archives old nonces            |
| Stream discovery | EVENT_BUS_ENABLED                | 30s       | Discovers new Redis streams    |

Graceful shutdown order: WorkerPool -> RedisDLQConsumer -> FinalityPoller ->
NonceArchiver -> DLQHandler -> RPC client close -> OTel flush.

---

## 17. Middleware Stack

FastAPI/Starlette middleware wraps last-added as outermost. The execution
order for an inbound request is:

1. **PaymentMiddlewareASGI** (conditional) -- x402 payment gating on
   `POST /api/v1/endpoints` when `TRIPWIRE_TREASURY_ADDRESS` is set
2. **CORSMiddleware** -- Configurable allowed origins
3. **SlowAPIMiddleware** -- Rate limiting via slowapi
4. **RequestLoggingMiddleware** -- Structured request/response logging

Global exception handlers: PostgrestAPIError (maps PG error codes to HTTP),
httpx.ConnectError (503), httpx.TimeoutException (503), catch-all (500 +
Sentry capture).

---

## 18. API Routes

| Method | Path                              | Auth      | Description                  |
|--------|-----------------------------------|-----------|------------------------------|
| POST   | /api/v1/ingest/goldsky            | Goldsky   | Batch event ingestion        |
| POST   | /api/v1/ingest/event              | Goldsky   | Single event ingestion       |
| POST   | /api/v1/ingest/facilitator        | HMAC      | x402 facilitator fast path   |
| POST   | /api/v1/endpoints                 | SIWE+x402 | Register endpoint            |
| GET    | /api/v1/endpoints                 | SIWE      | List endpoints               |
| POST   | /api/v1/subscriptions             | SIWE      | Create subscription          |
| GET    | /api/v1/events                    | SIWE      | Query events                 |
| GET    | /api/v1/deliveries                | SIWE      | Query webhook deliveries     |
| GET    | /api/v1/stats                     | SIWE      | Dashboard statistics         |
| POST   | /auth/verify                 | None      | SIWE signature verification  |
| GET    | /auth/nonce                  | None      | SIWE nonce generation        |
| POST   | /auth/session                     | SIWE      | Open a Keeper session (budget+TTL)  |
| GET    | /auth/session/{id}                | SIWE      | Get session status + remaining budget|
| DELETE | /auth/session/{id}                | SIWE      | Close session and return final state |
| POST   | /mcp                              | 4-tier    | MCP JSON-RPC endpoint        |
| GET    | /.well-known/x402-manifest.json   | None      | x402 Bazaar manifest (returns 410 Gone; use /discovery/resources) |
| GET    | /health                           | None      | Basic health check           |
| GET    | /health/detailed                  | None      | Deep health with components  |
| GET    | /ready                            | None      | Readiness probe              |
| GET    | /metrics                          | Bearer    | Prometheus metrics           |
