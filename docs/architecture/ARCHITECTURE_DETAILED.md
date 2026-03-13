# TripWire — Detailed Architecture Diagram

> Programmable onchain event triggers for AI agents.
> x402 payment middleware + general-purpose blockchain event triggering.

---

## Master Architecture: The Complete Picture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           AI AGENT INTERFACE                                         │
│                                                                                     │
│  ┌──────────────────────────────────┐  ┌──────────────────────────────────────────┐  │
│  │  MCP SERVER (JSON-RPC /mcp)      │  │  x402 BAZAAR DISCOVERY                   │  │
│  │                                  │  │                                          │  │
│  │  initialize / tools/list         │  │  /.well-known/x402-manifest.json         │  │
│  │  tools/call → 8 tools            │  │  Lists services, prices, MCP endpoint    │  │
│  │                                  │  │                                          │  │
│  │  Auth: Bearer <eth_address>      │  │  Agents discover TripWire, then call     │  │
│  │  Reputation gating per tool      │  │  MCP tools to register + configure       │  │
│  │  Audit log every call            │  │                                          │  │
│  └────────────────┬─────────────────┘  └──────────────────────────────────────────┘  │
│                   │                                                                  │
│   register_middleware / create_trigger / activate_template / ...                      │
│                   │                                                                  │
│                   ▼                                                                  │
│  ┌─────────────────────────────────────────────────────────────────────────────┐     │
│  │                     TRIGGER REGISTRY (Supabase)                              │     │
│  │                                                                             │     │
│  │  trigger_templates ──→ triggers ──→ trigger_instances                        │     │
│  │  (Bazaar catalog)      (active rules)  (template deployments)               │     │
│  │                                                                             │     │
│  │  TTL-cached lookups (30s) by topic0 + chain_id + contract_address           │     │
│  └─────────────────────────────────────────────────────────────────────────────┘     │
│                                                                                     │
└─────────────────────────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              INPUT SOURCES                                          │
│                                                                                     │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌───────────────────────────┐  │
│  │  FAST PATH (~100ms)  │  │ RELIABLE PATH (~2-4s)│  │  DYNAMIC TRIGGERS         │  │
│  │                      │  │                      │  │                           │  │
│  │  x402 Facilitator    │  │  Goldsky Turbo       │  │  Any Contract Event       │  │
│  │  onAfterVerify hook  │  │  Webhook Sink        │  │  Pool State Changes       │  │
│  │                      │  │                      │  │  Governance Votes         │  │
│  │  "Check is signed,   │  │  "Money moved        │  │  NFT Mints, DEX Swaps    │  │
│  │   not yet cashed"    │  │   onchain, here's    │  │                           │  │
│  │                      │  │   the proof"         │  │  "Anything onchain that   │  │
│  │  POST /ingest/       │  │                      │  │   crosses your threshold" │  │
│  │    facilitator       │  │  POST /ingest/       │  │                           │  │
│  │                      │  │    goldsky           │  │  POST /ingest/            │  │
│  │  Bearer token auth   │  │                      │  │    goldsky (same path)    │  │
│  │                      │  │  Bearer token auth   │  │                           │  │
│  └──────────┬───────────┘  └──────────┬───────────┘  └─────────────┬─────────────┘  │
│             │                         │                            │                │
└─────────────┼─────────────────────────┼────────────────────────────┼────────────────┘
              │                         │                            │
              ▼                         ▼                            ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                          TRIPWIRE ENGINE (FastAPI :3402)                             │
│                                                                                     │
│  ┌─────────────────────────────────────────────────────────────────────────────┐     │
│  │                        EVENT TYPE ROUTER                                    │     │
│  │                                                                             │     │
│  │   raw_log → _detect_event_type() → route to handler                         │     │
│  │                                                                             │     │
│  │   1. Check hardcoded _EVENT_SIGNATURES (topic0 → type)                      │     │
│  │   2. Fallback: query Trigger Registry by topic0                             │     │
│  │      filter by chain_id + contract_address locally                          │     │
│  │   3. Return ("dynamic", matched_triggers) or "unknown"                      │     │
│  │                                                                             │     │
│  │   ┌─────────────────┐  ┌──────────────────────┐                            │     │
│  │   │ erc3009_transfer │  │ dynamic (trigger-    │                            │     │
│  │   │ (hardcoded)      │  │ registry matched)    │                            │     │
│  │   └────────┬────────┘  └──────────┬───────────┘                            │     │
│  │            │                       │                                        │     │
│  └────────────┼───────────────────────┼────────────────────────────────────────┘     │
│               │                       │                                              │
│               ▼                       ▼                                              │
│  ┌─────────────────────────────────────────────────────────────────────────────┐     │
│  │                    GENERIC PIPELINE (runs for ALL event types)               │     │
│  │                                                                             │     │
│  │   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │     │
│  │   │  DECODE  │  │  DEDUP   │  │ IDENTITY │  │ FILTER / │  │  DISPATCH  │  │     │
│  │   │          │  │          │  │          │  │ POLICY   │  │            │  │     │
│  │   │ ABI-     │  │ Nonce or │  │ ERC-8004 │  │ Trigger  │  │ Convoy +   │  │     │
│  │   │ driven   │  │ event    │  │ agent    │  │ filters  │  │ Direct     │  │     │
│  │   │ generic  │  │ hash     │  │ lookup   │  │ + policy │  │ httpx +    │  │     │
│  │   │ decoder  │  │          │  │ (cached) │  │ engine   │  │ Realtime   │  │     │
│  │   │          │  │          │  │          │  │          │  │            │  │     │
│  │   └──────────┘  └──────────┘  └──────────┘  └──────────┘  └────────────┘  │     │
│  │                                                                             │     │
│  │   ◄──── These stages run in PARALLEL via asyncio.gather (~5-10ms) ──────►  │     │
│  │                                                                             │     │
│  └─────────────────────────────────────────────────────────────────────────────┘     │
│                                                                                     │
└────────────────────────────────────────┬────────────────────────────────────────────┘
                                         │
                                         ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              DELIVERY LAYER                                         │
│                                                                                     │
│  ┌───────────────────────┐  ┌───────────────────────┐  ┌─────────────────────────┐  │
│  │  EXECUTE MODE         │  │  EXECUTE MODE         │  │  NOTIFY MODE            │  │
│  │  (Reliable Path)      │  │  (Fast Path)          │  │                         │  │
│  │                       │  │                       │  │  Supabase Realtime      │  │
│  │  Convoy self-hosted   │  │  Direct httpx POST    │  │  WebSocket push         │  │
│  │  ┌─────────────────┐  │  │                       │  │                         │  │
│  │  │ HMAC-SHA256 sign│  │  │  HTTP/2 + pre-warmed  │  │  No server needed       │  │
│  │  │ Retry 6x (17h)  │  │  │  connection pool      │  │  Client subscribes      │  │
│  │  │ DLQ on failure   │  │  │                       │  │  with filters           │  │
│  │  │ Delivery logs    │  │  │  ~2-5ms delivery      │  │                         │  │
│  │  └─────────────────┘  │  │                       │  │  ~sub-1ms delivery      │  │
│  │                       │  │                       │  │                         │  │
│  │  Fires simultaneously │◄─┤  via asyncio.gather   │  │                         │  │
│  └───────────┬───────────┘  └───────────┬───────────┘  └────────────┬────────────┘  │
│              │                          │                           │                │
└──────────────┼──────────────────────────┼───────────────────────────┼────────────────┘
               │                          │                           │
               ▼                          ▼                           ▼
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                           YOUR APPLICATION / AI AGENT                                │
│                                                                                     │
│  Receives signed, enriched, policy-filtered webhooks.                                │
│  Acts on them. That's it.                                                            │
└─────────────────────────────────────────────────────────────────────────────────────┘
```

---

## Example 1: x402 Payment (Middleware Mode)

**Scenario:** A trading bot pays $0.05 USDC to call your AI analysis API on Arbitrum.

```
TIMELINE (Arbitrum)
════════════════════════════════════════════════════════════════════════════

0ms     Bot signs ERC-3009 authorization
        │
        ▼
10ms    x402 facilitator verifies signature ✅
        │
        ├──── facilitator.onAfterVerify() fires
        │     │
        ▼     ▼
50ms    Your API serves      TripWire receives via
        the response         POST /ingest/facilitator
        to the bot           │
                             ├─ Dedup (nonce check)        ─┐
                             ├─ Identity (ERC-8004 cached)  ├─ parallel
                             ├─ Policy (amount ✅ rep ✅)    ─┘
                             │
                             ▼
100ms   ┌──────────────────────────────────────────────────────┐
        │  WEBHOOK 1: payment.pre_confirmed                    │
        │                                                      │
        │  { "type": "payment.pre_confirmed",                  │
        │    "confidence": 0.9995,                             │
        │    "data": {                                         │
        │      "transfer": {                                   │
        │        "chain_id": 42161,                            │
        │        "from_address": "0xTradingBot...",            │
        │        "to_address": "0xYourAPI...",                 │
        │        "amount": "50000"                             │
        │      },                                              │
        │      "identity": {                                   │
        │        "agent_class": "trading-bot",                 │
        │        "reputation_score": 85.0,                     │
        │        "capabilities": ["swap", "limit-order"]       │
        │      }                                               │
        │    }                                                 │
        │  }                                                   │
        └──────────────────────────────────────────────────────┘
        │
        │ (meanwhile, facilitator submits tx to Arbitrum)
        │
250ms   Arbitrum sequencer confirms transfer
        │
        ▼
500ms   Goldsky Turbo detects → webhooks to TripWire
        │
        ├─ Decode ERC-3009 (Transfer + AuthorizationUsed)
        ├─ Dedup ✅ (nonce already seen → links to pre_confirmed event)
        ├─ Finality ✅ (1 block on Arbitrum)
        │
        ▼
600ms   ┌──────────────────────────────────────────────────────┐
        │  WEBHOOK 2: payment.confirmed                        │
        │                                                      │
        │  { "type": "payment.confirmed",                      │
        │    "confidence": 1.0,                                │
        │    "data": {                                         │
        │      "transfer": {                                   │
        │        "chain_id": 42161,                            │
        │        "tx_hash": "0xabc123...",                     │
        │        "block_number": 284501023,                    │
        │        "from_address": "0xTradingBot...",            │
        │        "to_address": "0xYourAPI...",                 │
        │        "amount": "50000"                             │
        │      },                                              │
        │      "finality": {                                   │
        │        "confirmations": 1,                           │
        │        "required_confirmations": 1,                  │
        │        "is_finalized": true                          │
        │      }                                               │
        │    }                                                 │
        │  }                                                   │
        └──────────────────────────────────────────────────────┘

YOUR APP:
  → On webhook 1 (100ms): Log revenue, update dashboard
  → On webhook 2 (600ms): Store tx_hash for audit trail
  → For $0.05, you act on webhook 1. Done.
```

---

## Example 2: Aerodrome Pool APR Alert (Event Trigger Mode)

**Scenario:** Your DeFi bot wants to know when USDC APR on Aerodrome pool drops below 9%.

```
TIMELINE (Base with Flashblocks)
════════════════════════════════════════════════════════════════════════════

0ms     Whale withdraws $2M from Aerodrome USDC pool
        │
        ▼
200ms   Base Flashblock produced (200ms mini-block)
        Pool emits Withdraw event + state changes
        │
        ▼
400ms   Goldsky Turbo detects pool event → webhooks to TripWire
        │
        ├─ Event type router: "pool_state_change"
        ├─ Wire evaluation: current APR = 8.7% < threshold 9%  ─┐
        ├─ Identity: resolve whale via ERC-8004 on Base          ├─ parallel
        │   (same contract 0x8004...a432 on all EVM chains)      │
        │   → agent_class: "yield-optimizer"                     │
        │   → reputation: 78.0                                   │
        │   → capabilities: ["lp-manage", "rebalance"]          ─┘
        ├─ Policy check: ✅ (reputation > 50)
        │
        ▼
450ms   ┌──────────────────────────────────────────────────────┐
        │  WEBHOOK: wire.triggered                             │
        │                                                      │
        │  { "type": "wire.triggered",                         │
        │    "wire_id": "wire_aerodrome_apr",                  │
        │    "wire_name": "aerodrome-apr-alert",               │
        │    "data": {                                         │
        │      "chain_id": 8453,                               │
        │      "contract": "0xAerodrome123...",                │
        │      "pool_name": "USDC/ETH #123",                  │
        │      "metric": "usdc_apr",                           │
        │      "current_value": 8.7,                           │
        │      "threshold": 9.0,                               │
        │      "operator": "less_than",                        │
        │      "previous_value": 9.2,                          │
        │      "block_number": 28451023,                       │
        │      "tx_hash": "0xdef456...",                       │
        │      "triggered_by": "0xWhale...",                   │
        │      "identity": {                                   │
        │        "address": "0xWhale...",                       │
        │        "agent_class": "yield-optimizer",             │
        │        "deployer": "0xDeployerABC...",               │
        │        "capabilities": ["lp-manage", "rebalance"],   │
        │        "reputation_score": 78.0                      │
        │      }                                               │
        │    }                                                 │
        │  }                                                   │
        └──────────────────────────────────────────────────────┘

YOUR BOT:
  → Receives webhook at ~450ms
  → Knows WHO caused the APR drop (yield-optimizer bot, rep 78)
  → Automatically moves liquidity to higher-yield pool
  → Can filter future wires: "only alert me if the withdrawer has rep > 60"
  → Confirms rebalance tx back via TripWire payment webhook
```

---

## Example 3: Whale Alert (Event Trigger Mode)

**Scenario:** Your analytics dashboard wants to know when any wallet moves > 100K USDC on Ethereum.

```
TIMELINE (Ethereum L1)
════════════════════════════════════════════════════════════════════════════

0s      Whale initiates 500K USDC transfer
        │
        ▼
~12s    Block mined on Ethereum
        Transfer event emitted
        │
        ▼
~12.5s  Goldsky Turbo detects → webhooks to TripWire
        │
        ├─ Event type router: "erc20_transfer"
        ├─ Wire evaluation: 500,000 > threshold 100,000 ✅
        ├─ Identity: resolves sender via ERC-8004
        │   → agent_class: "treasury-manager"
        │   → reputation: 92.0
        ├─ Policy: ✅
        │
        ▼
~12.6s  ┌──────────────────────────────────────────────────────┐
        │  WEBHOOK: wire.triggered                             │
        │                                                      │
        │  { "type": "wire.triggered",                         │
        │    "wire_id": "wire_whale_alert",                    │
        │    "data": {                                         │
        │      "chain_id": 1,                                  │
        │      "from_address": "0xWhale...",                   │
        │      "to_address": "0xExchange...",                  │
        │      "amount": "500000000000",                       │
        │      "identity": {                                   │
        │        "agent_class": "treasury-manager",            │
        │        "reputation_score": 92.0                      │
        │      }                                               │
        │    }                                                 │
        │  }                                                   │
        └──────────────────────────────────────────────────────┘

YOUR DASHBOARD:
  → Shows real-time whale movement
  → Enriched with WHO moved it (ERC-8004 identity)
  → No chain infrastructure needed
```

---

## Latency Map: Every Path, Every Chain

```
┌─────────────────────────────────────────────────────────────────────────┐
│                         LATENCY BY PATH & CHAIN                        │
│                                                                        │
│  INPUT SOURCE           ARBITRUM      BASE           ETHEREUM          │
│  ─────────────────────────────────────────────────────────────────────  │
│                                                                        │
│  x402 Facilitator       ~100ms        ~100ms         ~100ms            │
│  Hook (pre-chain)       (fastest)     (fastest)      (fastest)         │
│                                                                        │
│  Goldsky Turbo          ~500ms-1s     ~1-2s          ~12.5s            │
│  Webhook                (reliable)    (reliable)     (reliable)        │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────────  │
│  TripWire Processing    ~5-10ms       ~5-10ms        ~5-10ms           │
│  (parallel pipeline)    (warm cache)  (warm cache)   (warm cache)      │
│                                                                        │
│  Generic Decode         ~<0.1ms       ~<0.1ms        ~<0.1ms           │
│  (ABI-driven, CPU only)                                                │
│                                                                        │
│  Filter Engine          ~<0.01ms      ~<0.01ms       ~<0.01ms          │
│  (per trigger, 1-5 rules)                                              │
│                                                                        │
│  Trigger Registry       ~0ms (hit)    ~0ms (hit)     ~0ms (hit)        │
│  Lookup (30s TTL cache) ~3-10ms(miss) ~3-10ms(miss)  ~3-10ms(miss)    │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────────  │
│  Webhook Delivery       ~2-5ms        ~2-5ms         ~2-5ms            │
│  (HTTP/2 pre-warmed)                                                   │
│                                                                        │
│  Realtime Push          ~sub-1ms      ~sub-1ms       ~sub-1ms          │
│  (WebSocket)                                                           │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────────  │
│  MCP OPERATIONS (not latency-critical, one-time setup)                 │
│                                                                        │
│  register_middleware    ~50-150ms     (DB writes + cache invalidation)  │
│  create_trigger         ~30-80ms      (single DB insert)               │
│  list_templates         ~0ms (cached) / ~20-50ms (cold)                │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────────  │
│  TOTAL END-TO-END                                                      │
│                                                                        │
│  Fast path (facilitator)  ~107-115ms   ~107-115ms    ~107-115ms        │
│  Reliable path (Goldsky)  ~507ms-1s    ~1-2s         ~12.5s            │
│  Dynamic trigger path     ~510ms-1s    ~1-2s         ~12.5s            │
│  (same as Goldsky, +<1ms filter overhead)                              │
│                                                                        │
│  * Base Flashblocks = 200ms mini-blocks (requires Flashblocks RPC)     │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## Data Flow: Hybrid Model (Recommended Production Architecture)

```
                    THE HYBRID MODEL
                    ═══════════════

                 ┌──────────────────┐
                 │   AI Agent /     │
                 │   Human User     │
                 └────────┬─────────┘
                          │
                    1. HTTP Request
                    "GET /api/analyze"
                          │
                          ▼
                 ┌──────────────────┐
                 │  Your API Server │
                 │  (x402-enabled)  │
                 └────────┬─────────┘
                          │
                    2. "402 Payment Required"
                    "Pay $0.05 USDC to 0xYou"
                          │
                          ▼
                 ┌──────────────────┐
                 │   AI Agent       │
                 │   signs ERC-3009 │
                 │   authorization  │
                 └────────┬─────────┘
                          │
                    3. Resubmits with X-PAYMENT header
                          │
                          ▼
              ┌───────────────────────────┐
              │   x402 FACILITATOR        │
              │                           │
              │   4. Verifies signature   │
              │      ✅ Valid             │
              │      ✅ Has balance       │
              │      ✅ Nonce unused      │
              │                           │
              │   5. onAfterVerify fires  │──────────┐
              │                           │          │
              │   6. Serves API response  │          │  FAST PATH
              │      to the agent         │          │  (~50ms)
              │                           │          │
              │   7. Submits tx to chain  │──┐       │
              │      (background)         │  │       │
              └───────────────────────────┘  │       │
                                             │       │
            ┌────────────────────────────────┘       │
            │                                        │
            ▼                                        ▼
  ┌──────────────────┐                  ┌──────────────────────┐
  │  BLOCKCHAIN       │                  │  TRIPWIRE            │
  │                   │                  │  /ingest/facilitator │
  │  Arbitrum: 250ms  │                  │                      │
  │  Base: 2s         │                  │  Dedup ──┐           │
  │  Ethereum: 12s    │                  │  Identity ├ parallel │
  │                   │                  │  Policy ──┘          │
  │  Transfer event   │                  │                      │
  │  emitted          │                  │  → payment.          │
  └────────┬──────────┘                  │    pre_confirmed     │
           │                             │    (confidence 0.99) │
           ▼                             └──────────┬───────────┘
  ┌──────────────────┐                              │
  │  GOLDSKY TURBO   │                              │  ~100ms
  │                  │                              │
  │  Indexes block   │                              ▼
  │  Decodes event   │                    ┌──────────────────┐
  │  Webhooks to     │                    │  YOUR APP        │
  │  TripWire        │                    │                  │
  └────────┬─────────┘                    │  "Got it!        │
           │                              │   Logged $0.05   │
           ▼                              │   revenue."      │
  ┌──────────────────────┐                │                  │
  │  TRIPWIRE            │                └──────────────────┘
  │  /ingest/goldsky     │                         ▲
  │                      │                         │
  │  Decode ERC-3009     │                         │
  │  Dedup (links to     │                         │
  │    pre_confirmed)    │                         │
  │  Finality ✅          │    RELIABLE PATH        │
  │                      │    (~500ms-2s)          │
  │  → payment.confirmed │                         │
  │    (confidence 1.0)  │─────────────────────────┘
  │    (includes tx_hash)│   "Confirmed. Here's
  └──────────────────────┘    the proof: 0xabc..."
```

---

## Internal Processing Pipeline (Optimized)

```
┌─────────────────────────────────────────────────────────────────────┐
│                     EVENT PROCESSOR INTERNALS                       │
│                                                                     │
│  Raw event arrives at /ingest                                       │
│  │                                                                  │
│  ▼                                                                  │
│  ┌──────────────────────────────────┐                               │
│  │  1. EVENT TYPE ROUTER  (<0.1ms)  │                               │
│  │                                  │                               │
│  │  topics[0] → lookup in registry  │                               │
│  │                                  │                               │
│  │  Step 1: _EVENT_SIGNATURES dict  │                               │
│  │    0x98de50... → erc3009_transfer│                               │
│  │    0xddf252... → erc3009_transfer│                               │
│  │                                  │                               │
│  │  Step 2: TriggerRegistry fallback│                               │
│  │    topic0 → find_by_topic()      │                               │
│  │    (30s TTL cache, then filter   │                               │
│  │     by chain_id + contract_addr) │                               │
│  │    → ("dynamic", triggers[])     │                               │
│  └──────────────┬───────────────────┘                               │
│                 │                                                    │
│                 ▼                                                    │
│  ┌──────────────────────────────────┐                               │
│  │  2. TYPE-SPECIFIC HANDLER        │                               │
│  │                                  │                               │
│  │  erc3009: _process_erc3009()     │                               │
│  │    → decode_transfer_event()     │  <0.1ms (CPU only)            │
│  │                                  │                               │
│  │  dynamic: _process_dynamic_event │                               │
│  │    → decode_event_with_abi()     │  <0.1ms (ABI-driven generic)  │
│  │    → evaluate_filters()          │  <0.01ms (AND logic)          │
│  │    Per-trigger: decode → filter  │                               │
│  │      → dedup → identity → send   │                               │
│  └──────────────┬───────────────────┘                               │
│                 │                                                    │
│                 ▼                                                    │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  3. PARALLEL STAGES via asyncio.gather()     ~5-10ms total   │   │
│  │                                                              │   │
│  │  ┌────────────┐ ┌────────────┐ ┌────────────┐ ┌──────────┐  │   │
│  │  │   DEDUP    │ │  FINALITY  │ │  IDENTITY  │ │ ENDPOINT │  │   │
│  │  │            │ │            │ │            │ │  FETCH   │  │   │
│  │  │ Nonce      │ │ eth_block  │ │ ERC-8004   │ │          │  │   │
│  │  │ INSERT     │ │ Number     │ │ resolver   │ │ Supabase │  │   │
│  │  │            │ │            │ │            │ │ query    │  │   │
│  │  │ Supabase   │ │ Persistent │ │ 5-min TTL  │ │ 30s TTL  │  │   │
│  │  │ upsert     │ │ httpx      │ │ cache      │ │ cache    │  │   │
│  │  │            │ │ client     │ │            │ │          │  │   │
│  │  │ ~3-5ms     │ │ ~3-8ms     │ │ ~0ms hit   │ │ ~0ms hit │  │   │
│  │  │            │ │            │ │ ~10-40ms   │ │ ~3-10ms  │  │   │
│  │  │            │ │            │ │  miss      │ │  miss    │  │   │
│  │  └────────────┘ └────────────┘ └────────────┘ └──────────┘  │   │
│  │                                                              │   │
│  │  Total: bounded by slowest stage                             │   │
│  │  Warm cache: ~5ms  |  Cold cache: ~15ms                      │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                 │                                                    │
│                 ▼                                                    │
│  ┌──────────────────────────────────┐                               │
│  │  4. POLICY EVALUATION  (<0.5ms)  │                               │
│  │                                  │                               │
│  │  min_amount ✅                    │                               │
│  │  max_amount ✅                    │                               │
│  │  blocked_senders ✅               │                               │
│  │  allowed_senders ✅               │                               │
│  │  required_agent_class ✅          │                               │
│  │  min_reputation_score ✅          │                               │
│  └──────────────┬───────────────────┘                               │
│                 │                                                    │
│                 ▼                                                    │
│  ┌──────────────────────────────────────────────────────────────┐   │
│  │  5. DISPATCH  (fires all paths simultaneously)               │   │
│  │                                                              │   │
│  │  asyncio.gather(                                             │   │
│  │    convoy.send_webhook(payload),        # reliable, ~20-80ms │   │
│  │    convoy.direct_deliver(payload),      # fast, ~2-5ms       │   │
│  │    realtime.notify(payload),            # push, ~sub-1ms     │   │
│  │  )                                                           │   │
│  │                                                              │   │
│  │  + background: asyncio.create_task(record_event())           │   │
│  │  + background: asyncio.create_task(record_delivery())        │   │
│  └──────────────────────────────────────────────────────────────┘   │
│                                                                     │
│  TOTAL PROCESSING TIME: ~5-10ms (warm) | ~15-20ms (cold)           │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Delivery Layer Detail

```
┌─────────────────────────────────────────────────────────────────────┐
│                    DUAL-PATH DELIVERY SYSTEM                        │
│                                                                     │
│  WebhookPayload ready                                               │
│  │                                                                  │
│  ├─── EXECUTE MODE ──────────────────────────────────────────────── │
│  │                                                                  │
│  │    ┌──────────────────────┐    ┌──────────────────────────┐      │
│  │    │  CONVOY (Reliable)   │    │  DIRECT httpx (Fast)     │      │
│  │    │                      │    │                          │      │
│  │    │  POST /messages      │    │  POST to endpoint URL    │      │
│  │    │  Convoy signs HMAC   │    │  TripWire signs HMAC     │      │
│  │    │  Convoy delivers     │    │  HTTP/2 multiplexed      │      │
│  │    │  Convoy retries on   │    │  Pre-warmed TCP+TLS      │      │
│  │    │  failure (6 attempts)│    │  orjson serialization    │      │
│  │    │  DLQ after exhausted │    │                          │      │
│  │    │                      │    │  Headers:                │      │
│  │    │  ~20-80ms            │    │   X-TripWire-Signature   │      │
│  │    │                      │    │   X-TripWire-ID          │      │
│  │    │                      │    │   X-TripWire-Timestamp   │      │
│  │    │                      │    │                          │      │
│  │    │                      │    │  ~2-5ms                  │      │
│  │    └──────────┬───────────┘    └──────────┬───────────────┘      │
│  │               │                           │                      │
│  │               │    fire simultaneously    │                      │
│  │               │    via asyncio.gather     │                      │
│  │               ▼                           ▼                      │
│  │            Developer's HTTPS endpoint                            │
│  │            receives BOTH (deduplicates via idempotency_key)      │
│  │                                                                  │
│  ├─── NOTIFY MODE ───────────────────────────────────────────────── │
│  │                                                                  │
│  │    ┌──────────────────────────────────────────────────────┐      │
│  │    │  Supabase Realtime                                   │      │
│  │    │                                                      │      │
│  │    │  INSERT into realtime_events table                   │      │
│  │    │  → Postgres NOTIFY                                   │      │
│  │    │  → Supabase Realtime WebSocket                       │      │
│  │    │  → Client receives event                             │      │
│  │    │                                                      │      │
│  │    │  Subscriptions filter by:                            │      │
│  │    │   chains, senders, recipients, min_amount,           │      │
│  │    │   agent_class                                        │      │
│  │    │                                                      │      │
│  │    │  No webhook server needed. Client-side only.         │      │
│  │    └──────────────────────────────────────────────────────┘      │
│  │                                                                  │
└──┴──────────────────────────────────────────────────────────────────┘
```

---

## Security Model

```
┌─────────────────────────────────────────────────────────────────────┐
│                     SECURITY LAYERS                                  │
│                                                                     │
│  INGEST SECURITY                                                    │
│  ─────────────────                                                  │
│  Goldsky → TripWire:     Bearer token (Authorization header)        │
│  Facilitator → TripWire: Bearer token (separate secret)             │
│  Contract validation:    Only USDC contracts per chain              │
│                                                                     │
│  PROCESSING SECURITY                                                │
│  ────────────────────                                               │
│  Nonce dedup:            PostgreSQL UNIQUE constraint (atomic)      │
│  Policy engine:          Allowlist/blocklist + reputation gate       │
│  Identity verification:  Onchain ERC-8004 lookup (not self-reported)│
│                                                                     │
│  DELIVERY SECURITY                                                  │
│  ──────────────────                                                 │
│  HMAC-SHA256:            X-TripWire-Signature header                │
│                          Format: t={ts},v1={hex_digest}             │
│  Replay protection:      X-TripWire-Timestamp (5-min tolerance)     │
│  Message integrity:      X-TripWire-ID for dedup at consumer        │
│  Idempotency:            Deterministic key from (endpoint+chain+    │
│                          nonce+authorizer)                          │
│                                                                     │
│  API SECURITY                                                       │
│  ─────────────                                                      │
│  API key:                Hashed (never stored plaintext)             │
│  Key rotation:           24h grace period for old key               │
│  Rate limiting:          100 req/min ingest, 30 req/min CRUD        │
│  CORS:                   Configurable allowed origins (no wildcard)  │
│  Supabase:               Service role key (backend only, not anon)  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Database Schema (Entity Relationships)

```
┌─────────────────┐       ┌──────────────────┐       ┌──────────────────┐
│   endpoints     │       │   subscriptions  │       │   events         │
│                 │       │                  │       │                  │
│ id          PK  │──┐    │ id           PK  │       │ id           PK  │──┐
│ url             │  │    │ endpoint_id  FK  │←──────│ chain_id         │  │
│ mode            │  │    │ filters  (JSONB) │       │ tx_hash          │  │
│ chains  (JSONB) │  │    │ active           │       │ block_number     │  │
│ recipient       │  │    │ created_at       │       │ from_address     │  │
│ policies(JSONB) │  │    └──────────────────┘       │ to_address       │  │
│ api_key_hash    │  │                               │ amount           │  │
│ webhook_secret  │  │                               │ nonce            │  │
│ active          │  │                               │ status           │  │
│ created_at      │  │                               │ identity  (JSON) │  │
│ updated_at      │  │                               │ created_at       │  │
└─────────────────┘  │                               └──────────────────┘  │
                     │                                                     │
                     │    ┌──────────────────────┐                         │
                     └───→│ webhook_deliveries   │←────────────────────────┘
                          │                      │
                          │ id               PK  │
                          │ endpoint_id      FK  │
                          │ event_id         FK  │
                          │ convoy_message_id    │
                          │ status               │
                          │ created_at           │
                          └──────────────────────┘

┌──────────────────┐       ┌──────────────────┐
│   nonces         │       │   audit_log      │
│                  │       │                  │
│ chain_id         │       │ id           PK  │
│ nonce            │       │ action           │
│ authorizer       │       │ entity_type      │
│ created_at       │       │ entity_id        │
│                  │       │ metadata  (JSON) │
│ UNIQUE(chain_id, │       │ created_at       │
│  nonce,authorizer│       └──────────────────┘
└──────────────────┘

TRIGGER REGISTRY TABLES (Migration 013)
────────────────────────────────────────

┌──────────────────────┐      ┌──────────────────────┐      ┌──────────────────────┐
│  trigger_templates   │      │  triggers            │      │  trigger_instances   │
│                      │      │                      │      │                      │
│ id             PK    │      │ id             PK    │      │ id             PK    │
│ name                 │      │ owner_address        │      │ template_id    FK    │
│ slug       UNIQUE    │      │ endpoint_id    FK    │──────│ owner_address        │
│ description          │      │ name                 │      │ endpoint_id    FK    │
│ category             │      │ event_signature      │      │ contract_address     │
│ event_signature      │      │ abi        (JSONB)   │      │ chain_ids   (JSONB)  │
│ abi        (JSONB)   │      │ contract_address     │      │ parameters  (JSONB)  │
│ default_chains(JSONB)│      │ chain_ids   (JSONB)  │      │ resolved_filters     │
│ default_filters(JSONB)      │ filter_rules(JSONB)  │      │ active               │
│ parameter_schema     │      │ webhook_event_type   │      │ created_at           │
│ webhook_event_type   │      │ reputation_threshold │      │ updated_at           │
│ reputation_threshold │      │ batch_id             │      └──────────────────────┘
│ author_address       │      │ active               │
│ is_public            │      │ created_at           │
│ install_count        │      │ updated_at           │
│ created_at           │      └──────────────────────┘
│ updated_at           │
└──────────────────────┘

Triggers:
  - DB triggers auto-increment install_count on trigger_instances INSERT
  - DB triggers auto-update updated_at on UPDATE for all 3 tables
  - GIN index on triggers.chain_ids for JSONB containment queries
  - Partial index on active=TRUE for efficient hot-path queries
```

---

## Phase Roadmap: From Payment Middleware to Event Platform

```
PHASE 1 — Foundation (DONE)                   PHASE 2 — Turbo Hybrid (DONE)
━━━━━━━━━━━━━━━━━━━━━━━━━━━                   ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Goldsky Turbo pipeline ✅                     MCP Server (JSON-RPC /mcp) ✅
  ERC-3009 decode ✅                            Trigger Registry (3 tables) ✅
  Nonce dedup ✅                                Generic ABI decoder ✅
  Finality tracking ✅                           Filter engine (10 operators) ✅
  ERC-8004 identity ✅                           x402 Bazaar manifest ✅
  Policy engine ✅                               Bazaar trigger templates ✅
  Convoy + direct delivery ✅                    Dynamic trigger routing ✅
  Supabase Realtime ✅                           TTL-cached registry lookups ✅
  Generic event processor ✅                     Reputation-gated MCP tools ✅
  CI/CD ✅                                       Audit logging for all MCP calls ✅

  Latency: ~1-4s (Goldsky path)               Latency: ~100ms (fast path)
                                                        ~1-2s (reliable)
                                                        +<1ms filter overhead

PHASE 3 — Platform                            PHASE 4 — Scale
━━━━━━━━━━━━━━━━━━                            ━━━━━━━━━━━━━━━━

  SIWE auth for MCP (replace                    Edge deployment
    MVP Bearer address)                         (Cloudflare Workers)

  Goldsky pipeline provisioning                 More EVM chains
    per-trigger (auto-create                    (Optimism, Polygon,
    Goldsky sources on register)                 Avalanche)

  Wire SDK:                                     Non-EVM chains
   client.create_wire(                          (Solana, Aptos)
     contract="0x...",
     event="PoolStateChanged",                  Multi-tenant
     condition={"apr": {"lt": 9}},              white-label
     webhook_url="https://..."
   )                                            Agent-to-agent
                                               payment routing
  Template marketplace
  (community-authored templates)                Marketplace for
                                               wire templates
```

---

## Quick Reference: "When Do I Get My Webhook?"

```
┌────────────────────┬──────────────┬──────────────┬──────────────┐
│ What Happened      │  Arbitrum    │  Base        │  Ethereum    │
├────────────────────┼──────────────┼──────────────┼──────────────┤
│                    │              │              │              │
│ x402 payment       │              │              │              │
│ (with facilitator) │  ~100ms      │  ~100ms      │  ~100ms      │
│                    │              │              │              │
│ x402 payment       │              │              │              │
│ (Goldsky only)     │  ~500ms      │  ~2s         │  ~13s        │
│                    │              │              │              │
│ Dynamic trigger    │              │              │              │
│ (registry-matched) │  ~500ms      │  ~2s         │  ~13s        │
│                    │              │              │              │
│ MCP registration   │              │              │              │
│ (one-time setup)   │  ~50-150ms   │  ~50-150ms   │  ~50-150ms   │
│                    │              │              │              │
│ Confirmation       │              │              │              │
│ (full finality)    │  ~15min      │  ~15min      │  ~13min      │
│ (L1 Casper)        │              │              │              │
│                    │              │              │              │
└────────────────────┴──────────────┴──────────────┴──────────────┘

Your app decides what to act on. TripWire delivers all of them.
```

---

## MCP Server Architecture

TripWire exposes a Model Context Protocol (MCP) server at `/mcp` for AI agent integration. The server implements JSON-RPC 2.0 over HTTP as a FastAPI sub-application.

```
┌──────────────────────────────────────────────────────────────────────┐
│                    MCP SERVER (/mcp)                                   │
│                                                                      │
│  Transport: JSON-RPC 2.0 over HTTP POST                              │
│  Protocol version: 2024-11-05                                        │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  METHOD DISPATCH                                               │  │
│  │                                                                │  │
│  │  initialize      → protocol version, capabilities, server info │  │
│  │  tools/list      → enumerate all 8 tools + input schemas       │  │
│  │  tools/call      → auth → reputation gate → execute → audit    │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  TOOL REGISTRY (8 tools)                                       │  │
│  │                                                                │  │
│  │  ToolDef { name, description, input_schema, handler,           │  │
│  │            min_reputation }                                    │  │
│  │                                                                │  │
│  │  register_middleware  Create endpoint + triggers in one call    │  │
│  │  create_trigger       Add custom trigger to existing endpoint   │  │
│  │  list_triggers        List agent's active triggers              │  │
│  │  delete_trigger       Soft-delete (deactivate) a trigger        │  │
│  │  list_templates       Browse Bazaar trigger templates           │  │
│  │  activate_template    Instantiate template for an endpoint      │  │
│  │  get_trigger_status   Health check + event count for a trigger  │  │
│  │  search_events        Query recent events for agent's endpoints │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  ┌────────────────────────────────────────────────────────────────┐  │
│  │  AUTH + REPUTATION GATING                                      │  │
│  │                                                                │  │
│  │  1. Extract agent address from Authorization: Bearer <addr>    │  │
│  │  2. If tool.min_reputation > 0:                                │  │
│  │     → resolve ERC-8004 identity (5-min TTL cache)              │  │
│  │     → reject if reputation < threshold                         │  │
│  │  3. Execute tool handler with (params, agent_address, repos)   │  │
│  │  4. Audit log: action, actor, resource, arguments, IP          │  │
│  └────────────────────────────────────────────────────────────────┘  │
│                                                                      │
│  Error codes:                                                        │
│   -32700  Parse error          -32601  Method not found               │
│   -32600  Invalid request      -32602  Invalid params                 │
│   -32000  Auth required        -32001  Reputation too low             │
│   -32603  Internal error                                              │
└──────────────────────────────────────────────────────────────────────┘
```

**Key design decisions:**

- The MCP server is a separate FastAPI sub-app mounted on the main app. This isolates routing and middleware.
- Every `tools/call` invocation is audit-logged via `fire_and_forget` (non-blocking background task).
- Tool handlers are pure async functions with signature `(params, agent_address, repos) -> dict`. Repos are constructed per-request from the parent app's Supabase client.
- Auth is MVP (Bearer address). SIWE verification is planned for production.

---

## Trigger Registry

The trigger registry enables agents to define custom event triggers without code changes. It consists of three database tables and a TTL-cached lookup layer.

```
┌─────────────────────────────────────────────────────────────────────┐
│                    TRIGGER REGISTRY (3 tables)                       │
│                                                                     │
│  ┌─────────────────────┐                                            │
│  │  trigger_templates   │  Bazaar catalog of pre-built triggers      │
│  │                     │                                            │
│  │  slug (UNIQUE)      │  "whale-transfer", "dex-swap", "nft-mint"  │
│  │  event_signature    │  Solidity event signature                   │
│  │  abi (JSONB)        │  Full ABI fragment for decoding             │
│  │  default_chains     │  Default chain_ids                          │
│  │  default_filters    │  Default filter rules                       │
│  │  parameter_schema   │  Configurable params for instantiation      │
│  │  reputation_thresh  │  Min reputation to use template             │
│  │  install_count      │  Auto-incremented on instantiation          │
│  │  category           │  defi / payments / nft / governance         │
│  └─────────┬───────────┘                                            │
│            │ instantiate                                             │
│            ▼                                                        │
│  ┌─────────────────────┐       ┌─────────────────────────┐          │
│  │  trigger_instances   │       │  triggers                │          │
│  │                     │       │                         │          │
│  │  template_id FK     │       │  owner_address          │          │
│  │  owner_address      │       │  endpoint_id FK         │          │
│  │  endpoint_id FK     │       │  event_signature        │          │
│  │  parameters (JSONB) │       │  abi (JSONB)            │          │
│  │  resolved_filters   │       │  contract_address       │          │
│  │  active             │       │  chain_ids (JSONB, GIN) │          │
│  └─────────────────────┘       │  filter_rules (JSONB)   │          │
│                                │  webhook_event_type     │          │
│                                │  reputation_threshold   │          │
│                                │  active                 │          │
│                                └─────────────────────────┘          │
│                                                                     │
│  CACHING STRATEGY                                                   │
│  ─────────────────                                                  │
│  TriggerRepository.find_by_topic(topic0):                           │
│    Module-level dict: topic → (timestamp, list[Trigger])             │
│    TTL: 30 seconds                                                  │
│    Invalidated on create/deactivate via invalidate_trigger_cache()   │
│                                                                     │
│  TriggerRepository.list_active():                                   │
│    Module-level list cache, invalidated on any mutation              │
│                                                                     │
│  TriggerTemplateRepository.list_public():                           │
│    Module-level list cache, invalidated on mutation                  │
│                                                                     │
│  INDEX STRATEGY                                                     │
│  ───────────────                                                    │
│  idx_triggers_event_sig     B-tree on event_signature               │
│  idx_triggers_contract      B-tree on contract_address (partial)    │
│  idx_triggers_active        B-tree on active WHERE TRUE (partial)   │
│  idx_triggers_chain_ids     GIN on chain_ids (JSONB containment)    │
└─────────────────────────────────────────────────────────────────────┘
```

**How `_detect_event_type` falls back to the registry:**

1. Parse `topics[0]` from the raw log (handles both list and comma-separated string formats).
2. Look up `topic0` in the hardcoded `_EVENT_SIGNATURES` dict (O(1) hash lookup).
3. If no match, call `TriggerRepository.find_by_topic(topic0)` -- this hits the 30s TTL cache, not the DB on hot path.
4. Filter returned triggers locally by `chain_id` and `contract_address`.
5. If any triggers match, return `("dynamic", matched_triggers)` to route to `_process_dynamic_event`.
6. Otherwise return `"unknown"` and the event is skipped.

---

## Generic Decoder

The generic decoder (`tripwire/ingestion/generic_decoder.py`) enables ABI-driven decoding of any EVM event without hardcoding specific event types.

```
┌─────────────────────────────────────────────────────────────────────┐
│                  GENERIC ABI-DRIVEN DECODER                          │
│                                                                     │
│  Input: raw_log + abi_fragment (from trigger definition)             │
│                                                                     │
│  decode_event_with_abi(raw_log, abi_fragment)                        │
│  │                                                                  │
│  ├─ 1. Find first entry with type=="event" in abi_fragment           │
│  │                                                                  │
│  ├─ 2. Split inputs into indexed vs non-indexed                      │
│  │                                                                  │
│  ├─ 3. Decode indexed params from topics[1:]                         │
│  │     ┌──────────────────────────────────────────┐                 │
│  │     │ address  → extract last 40 hex chars      │                 │
│  │     │ bytes32  → pass through as hex            │                 │
│  │     │ uint/int → int(topic_hex, 16)             │                 │
│  │     │ bool     → int(topic_hex, 16) != 0        │                 │
│  │     └──────────────────────────────────────────┘                 │
│  │                                                                  │
│  ├─ 4. Decode non-indexed params from data via eth_abi.decode()      │
│  │     Uses ABI type strings: ["uint256", "int256", "address", ...]  │
│  │     Bytes values → "0x{hex}"                                      │
│  │                                                                  │
│  └─ 5. Attach _-prefixed metadata:                                   │
│        _tx_hash, _block_number, _block_hash, _log_index,            │
│        _address (contract), _chain_id                                │
│                                                                     │
│  Output: flat dict { field_name: value, _metadata: value }           │
│                                                                     │
│  Performance: CPU-only, no I/O. < 0.1ms per event.                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Filter Engine

The filter engine (`tripwire/ingestion/filter_engine.py`) evaluates trigger-specific predicates against decoded event fields. All filter rules use AND logic -- every rule must pass for the event to match.

```
┌─────────────────────────────────────────────────────────────────────┐
│                     FILTER ENGINE                                     │
│                                                                     │
│  evaluate_filters(decoded, filter_rules) → (passed, reason)          │
│                                                                     │
│  OPERATOR SET                                                       │
│  ────────────                                                       │
│  eq       Equality (case-insensitive, address-aware normalization)   │
│  neq      Not equal                                                 │
│  gt       Greater than (Decimal comparison)                          │
│  gte      Greater than or equal                                     │
│  lt       Less than                                                 │
│  lte      Less than or equal                                        │
│  in       Value in list                                             │
│  not_in   Value not in list                                         │
│  between  lo <= value <= hi (list of 2 targets)                     │
│  contains Case-insensitive substring match                          │
│  regex    Python re.search on string representation                 │
│                                                                     │
│  NORMALIZATION                                                      │
│  ─────────────                                                      │
│  - Addresses: lowercased, matched via regex 0x[0-9a-fA-F]{40}       │
│  - Numbers: converted to Decimal (handles string ints, hex values)   │
│  - Hex values: 0x-prefixed non-address strings → Decimal(int(v,16)) │
│                                                                     │
│  LOGIC: AND (all rules must pass)                                    │
│  PERFORMANCE: O(n) where n = number of rules. CPU-only, no I/O.     │
│  Typical: < 0.01ms for 1-5 rules.                                   │
│                                                                     │
│  EXAMPLE FILTER RULES                                                │
│  ─────────────────────                                               │
│  [                                                                  │
│    {"field": "value", "op": "gte", "value": "1000000000"},           │
│    {"field": "from", "op": "neq", "value": "0x000...000"},           │
│    {"field": "to", "op": "in", "value": ["0xabc...", "0xdef..."]}    │
│  ]                                                                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Middleware Registration Sequence Diagram

The `register_middleware` MCP tool is the primary entry point for AI agents to onboard. It creates an endpoint and optionally wires up triggers from templates or custom definitions in a single call.

```
REGISTER_MIDDLEWARE FLOW
════════════════════════════════════════════════════════════════════════

AI Agent                    MCP Server                  Supabase
   │                           │                           │
   │  POST /mcp                │                           │
   │  { "method": "tools/call",│                           │
   │    "params": {            │                           │
   │      "name":              │                           │
   │        "register_middleware",                          │
   │      "arguments": {       │                           │
   │        "url": "https://...",                           │
   │        "mode": "execute", │                           │
   │        "chains": [8453],  │                           │
   │        "template_slugs":  │                           │
   │          ["whale-transfer"],                          │
   │        "custom_triggers": │                           │
   │          [{ "event_signature":                        │
   │             "Swap(...)" }]│                           │
   │      }                    │                           │
   │    }                      │                           │
   │  }                        │                           │
   │ ─────────────────────────►│                           │
   │                           │                           │
   │                    1. AUTH │                           │
   │                    Extract agent address               │
   │                    from Bearer header                  │
   │                           │                           │
   │                    2. REPUTATION CHECK                 │
   │                    (skipped if min_reputation == 0)    │
   │                           │                           │
   │                    3. CREATE ENDPOINT                  │
   │                           │  INSERT endpoints         │
   │                           │──────────────────────────►│
   │                           │  { id, url, mode,         │
   │                           │    chains, recipient,     │
   │                           │    webhook_secret,        │
   │                           │    owner_address }        │
   │                           │◄──────────────────────────│
   │                           │                           │
   │                    4. INSTANTIATE TEMPLATE TRIGGERS    │
   │                    For each template_slug:             │
   │                           │  SELECT trigger_templates │
   │                           │  WHERE slug = ?           │
   │                           │──────────────────────────►│
   │                           │◄──────────────────────────│
   │                           │                           │
   │                           │  INSERT triggers          │
   │                           │  { event_signature,       │
   │                           │    abi, chain_ids,        │
   │                           │    filter_rules,          │
   │                           │    endpoint_id }          │
   │                           │──────────────────────────►│
   │                           │◄──────────────────────────│
   │                           │                           │
   │                    5. CREATE CUSTOM TRIGGERS           │
   │                    For each custom_trigger:            │
   │                           │  INSERT triggers          │
   │                           │  { event_signature,       │
   │                           │    abi, chain_ids,        │
   │                           │    filter_rules,          │
   │                           │    endpoint_id }          │
   │                           │──────────────────────────►│
   │                           │◄──────────────────────────│
   │                           │                           │
   │                    6. INVALIDATE TRIGGER CACHE         │
   │                    (module-level cache cleared)        │
   │                           │                           │
   │                    7. AUDIT LOG (fire-and-forget)      │
   │                           │  INSERT audit_log         │
   │                           │──────────────────────────►│
   │                           │                           │
   │  { "endpoint_id": "...",  │                           │
   │    "webhook_secret": "...",                           │
   │    "trigger_ids": [...],  │                           │
   │    "mode": "execute",     │                           │
   │    "url": "https://..." } │                           │
   │ ◄─────────────────────────│                           │
   │                           │                           │

   Once registered, the Goldsky ingest pipeline automatically picks up
   the new triggers via _detect_event_type() → TriggerRepository cache.
   No pipeline provisioning needed — triggers are data, not infrastructure.
```

---

## x402 Bazaar Integration

TripWire publishes a service manifest at `/.well-known/x402-manifest.json` for discovery by AI agents browsing the x402 Bazaar. This enables agents to find TripWire, understand its capabilities, and register programmatically via MCP.

```
┌─────────────────────────────────────────────────────────────────────┐
│                   x402 BAZAAR MANIFEST                               │
│                   /.well-known/x402-manifest.json                    │
│                                                                     │
│  {                                                                  │
│    "@context": "https://x402.org/context",                           │
│    "name": "TripWire",                                               │
│    "identity": {                                                    │
│      "protocol": "ERC-8004",                                         │
│      "registry": "0x8004...a432"                                     │
│    },                                                               │
│    "mcp": {                                                         │
│      "endpoint": "/mcp",                                             │
│      "transport": "streamable-http",                                 │
│      "tools": [ "register_middleware", "create_trigger", ... ]       │
│    },                                                               │
│    "services": [                                                    │
│      { "name": "register_middleware", "price": "$0.003" },           │
│      { "name": "create_trigger",     "price": "$0.003" },           │
│      { "name": "activate_template",  "price": "$0.001" }            │
│    ],                                                               │
│    "supported_chains": [                                             │
│      { "chain_id": 8453, "name": "Base" },                          │
│      { "chain_id": 1,    "name": "Ethereum" },                      │
│      { "chain_id": 42161, "name": "Arbitrum" }                      │
│    ],                                                               │
│    "trigger_templates": "/mcp (use list_templates tool)"             │
│  }                                                                  │
│                                                                     │
│  DISCOVERY FLOW                                                     │
│  ──────────────                                                     │
│                                                                     │
│  AI Agent                      Bazaar                 TripWire       │
│     │                            │                       │           │
│     │  browse services           │                       │           │
│     │───────────────────────────►│                       │           │
│     │  ◄── list of manifests     │                       │           │
│     │                            │                       │           │
│     │  GET /.well-known/x402-manifest.json               │           │
│     │────────────────────────────────────────────────────►│           │
│     │  ◄── manifest with MCP endpoint + pricing          │           │
│     │                                                    │           │
│     │  POST /mcp  (initialize)                           │           │
│     │────────────────────────────────────────────────────►│           │
│     │  ◄── capabilities, server info                     │           │
│     │                                                    │           │
│     │  POST /mcp  (tools/list)                           │           │
│     │────────────────────────────────────────────────────►│           │
│     │  ◄── 8 tools with input schemas                    │           │
│     │                                                    │           │
│     │  POST /mcp  (tools/call: register_middleware)      │           │
│     │────────────────────────────────────────────────────►│           │
│     │  ◄── endpoint_id, webhook_secret, trigger_ids      │           │
│     │                                                    │           │
│  Agent is now receiving onchain events via webhooks.      │           │
│  Zero human configuration required.                      │           │
└─────────────────────────────────────────────────────────────────────┘
```

**Pricing model:** Bazaar services are priced in USDC on Base (eip155:8453). Template activation is cheaper ($0.001) than custom trigger creation ($0.003) to incentivize reuse of battle-tested templates.
