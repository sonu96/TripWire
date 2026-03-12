# TripWire — Detailed Architecture Diagram

> Programmable onchain event triggers for AI agents.
> x402 payment middleware + general-purpose blockchain event triggering.

---

## Master Architecture: The Complete Picture

```
┌─────────────────────────────────────────────────────────────────────────────────────┐
│                              INPUT SOURCES                                          │
│                                                                                     │
│  ┌──────────────────────┐  ┌──────────────────────┐  ┌───────────────────────────┐  │
│  │  FAST PATH (~100ms)  │  │ RELIABLE PATH (~2-4s)│  │  FUTURE: CUSTOM WIRES     │  │
│  │                      │  │                      │  │                           │  │
│  │  x402 Facilitator    │  │  Goldsky Turbo       │  │  Any Contract Event       │  │
│  │  onAfterVerify hook  │  │  Webhook Sink        │  │  Pool State Changes       │  │
│  │                      │  │                      │  │  Governance Votes         │  │
│  │  "Check is signed,   │  │  "Money moved        │  │  NFT Mints, etc.         │  │
│  │   not yet cashed"    │  │   onchain, here's    │  │                           │  │
│  │                      │  │   the proof"         │  │  "Anything onchain that   │  │
│  │  POST /ingest/       │  │                      │  │   crosses your threshold" │  │
│  │    facilitator       │  │  POST /ingest/       │  │                           │  │
│  │                      │  │    goldsky           │  │  POST /ingest/            │  │
│  │  Bearer token auth   │  │                      │  │    wire                   │  │
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
│  │   ┌─────────────────┐  ┌─────────────────┐  ┌──────────────────────┐       │     │
│  │   │ erc3009_transfer │  │ pool_state      │  │ custom_wire          │       │     │
│  │   │ (NOW)            │  │ (PHASE 3)       │  │ (PHASE 3)            │       │     │
│  │   └────────┬────────┘  └────────┬────────┘  └──────────┬───────────┘       │     │
│  │            │                    │                       │                   │     │
│  └────────────┼────────────────────┼───────────────────────┼───────────────────┘     │
│               │                    │                       │                         │
│               ▼                    ▼                       ▼                         │
│  ┌─────────────────────────────────────────────────────────────────────────────┐     │
│  │                    GENERIC PIPELINE (runs for ALL event types)               │     │
│  │                                                                             │     │
│  │   ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌────────────┐  │     │
│  │   │  DEDUP   │  │ FINALITY │  │ IDENTITY │  │  POLICY  │  │  DISPATCH  │  │     │
│  │   │          │  │          │  │          │  │          │  │            │  │     │
│  │   │ Nonce or │  │ Block    │  │ ERC-8004 │  │ Amount   │  │ Convoy +   │  │     │
│  │   │ event    │  │ confirm  │  │ agent    │  │ Sender   │  │ Direct     │  │     │
│  │   │ hash     │  │ depth    │  │ lookup   │  │ Class    │  │ httpx +    │  │     │
│  │   │          │  │          │  │ (cached) │  │ Reputa-  │  │ Realtime   │  │     │
│  │   │          │  │          │  │          │  │ tion     │  │            │  │     │
│  │   └──────────┘  └──────────┘  └──────────┘  └──────────┘  └────────────┘  │     │
│  │                                                                             │     │
│  │   ◄──── These 4 stages run in PARALLEL via asyncio.gather (~5-10ms) ────►  │     │
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
│  WebSocket              ~300ms        ~250ms*        ~12.1s            │
│  eth_subscribe          (post-seq)    (flashblocks)  (post-block)      │
│                                                                        │
│  Goldsky Turbo          ~500ms-1s     ~1-2s          ~12.5s            │
│  Webhook                (reliable)    (reliable)     (reliable)        │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────────  │
│  TripWire Processing    ~5-10ms       ~5-10ms        ~5-10ms           │
│  (parallel pipeline)    (warm cache)  (warm cache)   (warm cache)      │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────────  │
│  Webhook Delivery       ~2-5ms        ~2-5ms         ~2-5ms            │
│  (HTTP/2 pre-warmed)                                                   │
│                                                                        │
│  Realtime Push          ~sub-1ms      ~sub-1ms       ~sub-1ms          │
│  (WebSocket)                                                           │
│                                                                        │
│  ─────────────────────────────────────────────────────────────────────  │
│  TOTAL END-TO-END                                                      │
│                                                                        │
│  Fast path (facilitator)  ~107-115ms   ~107-115ms    ~107-115ms        │
│  Medium path (WebSocket)  ~307-315ms   ~257-265ms    ~12.1s            │
│  Reliable path (Goldsky)  ~507ms-1s    ~1-2s         ~12.5s            │
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
│  │  0x98de50... → erc3009_transfer  │                               │
│  │  0xddf252... → erc3009_transfer  │                               │
│  │  0x??????... → pool_state (v2)   │                               │
│  │  0x??????... → custom_wire (v2)  │                               │
│  └──────────────┬───────────────────┘                               │
│                 │                                                    │
│                 ▼                                                    │
│  ┌──────────────────────────────────┐                               │
│  │  2. TYPE-SPECIFIC HANDLER        │                               │
│  │     (e.g., _process_erc3009)     │                               │
│  │                                  │                               │
│  │  Decode → ERC3009Transfer model  │  <0.1ms (CPU only)            │
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
```

---

## Phase Roadmap: From Payment Middleware to Event Platform

```
PHASE 1 — Foundation (NOW)                    PHASE 2 — Speed
━━━━━━━━━━━━━━━━━━━━━━━━━━                    ━━━━━━━━━━━━━━━━

  Goldsky Turbo pipeline ✅                     WebSocket eth_subscribe
  ERC-3009 decode ✅                            fast path (~200-500ms)
  Nonce dedup ✅
  Finality tracking ✅                           x402 facilitator hook
  ERC-8004 identity ✅                           fast path (~100ms)
  Policy engine ✅
  Convoy + direct delivery ✅                    HTTP/2 + connection
  Supabase Realtime ✅                           pre-warming
  Generic event processor ✅
  CI/CD ✅                                       Probabilistic finality
                                               (risk-tiered by amount)
  Latency: ~1-4s (Goldsky path)
                                               Latency: ~100ms (fast)
                                                        ~1-2s (reliable)

PHASE 3 — Platform                            PHASE 4 — Scale
━━━━━━━━━━━━━━━━━━                            ━━━━━━━━━━━━━━━━

  Custom wire types:                            Edge deployment
   - Pool APR thresholds                        (Cloudflare Workers)
   - Whale movement alerts
   - Governance vote triggers                   More EVM chains
   - NFT mint notifications                     (Optimism, Polygon,
   - Contract state changes                      Avalanche)

  Wire SDK:                                     Non-EVM chains
   client.create_wire(                          (Solana, Aptos)
     contract="0x...",
     event="PoolStateChanged",                  Multi-tenant
     condition={"apr": {"lt": 9}},              white-label
     webhook_url="https://..."
   )                                            Agent-to-agent
                                               payment routing
  Latency: same engine,
  same ~100-450ms                               Marketplace for
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
│ Wire trigger       │              │              │              │
│ (DeFi/custom)      │  ~300ms      │  ~450ms      │  ~12.5s      │
│                    │              │              │              │
│ Confirmation       │              │              │              │
│ (full finality)    │  ~15min      │  ~15min      │  ~13min      │
│ (L1 Casper)        │              │              │              │
│                    │              │              │              │
└────────────────────┴──────────────┴──────────────┴──────────────┘

Your app decides what to act on. TripWire delivers all of them.
```
