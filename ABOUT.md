# TripWire — Programmable Onchain Event Triggers for AI Agents

## Vision

The agentic web is no longer a whiteboard sketch. AI agents are live on mainnet, transacting autonomously, settling payments in USDC over HTTP. The protocol powering those transactions is **x402** -- an HTTP-native micropayment standard that turns a `402 Payment Required` response into a gasless, authorized onchain transfer.

x402 provides the rails. **TripWire provides the infrastructure.**

x402 payment webhooks are the first use case, but TripWire is built as a **programmable onchain event trigger platform**. Any onchain event -- token transfers, contract interactions, governance votes, NFT mints -- can be mapped to a webhook, filtered through policies, gated on payment requirements, and delivered to your application or AI agent. A unified processing pipeline (Phase C2) routes both ERC-3009 payments and dynamic triggers through the same decode-filter-gate-dedup-finality-identity-policy-dispatch path. Per-trigger payment gating (Phase C3) lets triggers require minimum payment amounts before firing.

Every API developer accepting x402 payments today faces the same problem: the payment settles onchain, but the application logic lives offchain. Between those two worlds sits a gap -- a gap filled with chain watchers, finality checks, replay protection, identity lookups, webhook delivery, and audit logging. Six distinct pieces of infrastructure that have nothing to do with your product.

TripWire closes that gap. One integration. One SDK call. Verified, enriched, policy-filtered payment events delivered to your application the moment they finalize onchain.

---

## The Problem: The x402 Infrastructure Gap

When a user (or an AI agent) pays for an x402 API call, the flow looks simple on paper:

1. Client sends an HTTP request
2. Server responds `402 Payment Required` with payment details
3. Client signs an ERC-3009 `transferWithAuthorization` and resubmits
4. Server verifies the authorization, serves the response
5. Payment settles onchain

Steps 1-4 happen in milliseconds. Step 5 takes blocks. And between step 5 and "your application knows about it," there are **six components** every developer must build from scratch:

| Component | What It Does | Why It's Hard |
|---|---|---|
| **Chain Watcher** | Monitor ERC-3009 `TransferWithAuthorization` events across chains | Multi-chain indexing, RPC reliability, reorg handling |
| **Finality Tracker** | Wait for sufficient block confirmations before trusting a transfer | Different finality depths per chain (Ethereum: 12, Base: 3, Arbitrum: 1) |
| **Replay Protection** | Deduplicate nonces to prevent double-processing | Distributed nonce tracking across concurrent requests |
| **Identity Resolution** | Look up onchain AI agent identities (ERC-8004) | Registry queries, metadata decoding, reputation scoring |
| **Webhook Delivery** | Deliver verified payloads to application endpoints with retries | Exponential backoff, HMAC signing, dead letter queues |
| **Audit Log** | Record every event for compliance and debugging | Immutable, queryable, correlated with chain data |

That is weeks of engineering. None of it is your product. All of it is required.

---

## The Solution: One Integration, Full Coverage

TripWire replaces all six components with a single API call:

```python
async with TripwireClient(api_key="tw_...") as client:
    endpoint = await client.register_endpoint(
        url="https://your-api.com/webhook",
        mode="execute",
        chains=[8453],           # Base
        recipient="0xYourAddress",
        policies={
            "min_amount": "1000000",        # 1 USDC minimum
            "min_reputation_score": 70,     # Only trusted agents
        },
    )
```

Register an endpoint. Define a policy. Receive verified, enriched payloads. Execute your business logic. That's it.

TripWire handles the chain watching (Goldsky Turbo via webhooks), the finality tracking (per-chain confirmation depths), the replay protection (nonce deduplication), the identity resolution (ERC-8004), the payment gating (per-trigger amount and token requirements), the webhook delivery (Convoy self-hosted), and the audit logging (Supabase) -- so you never have to.

---

## How It Works: Two Modes

TripWire supports two delivery modes, designed for different integration patterns.

### Notify Mode -- Push Notifications for Payments

Notify mode uses **Supabase Realtime** to push payment events directly to subscribed clients. Think of it as a live feed: your application opens a connection and receives events as they happen.

Best for:
- Dashboards and monitoring UIs
- Real-time payment feeds
- Applications that need instant visibility without executing logic

Subscriptions support granular filters -- by chain, sender, recipient, minimum amount, or agent class -- so you only see the events you care about.

### Execute Mode -- Stripe Webhooks for x402

Execute mode uses **Convoy self-hosted + a direct httpx fast path** to deliver HMAC-signed webhook payloads to your HTTPS endpoint with guaranteed delivery. This is the Stripe model: TripWire sends a POST to your URL, you verify the signature, parse the payload, and execute.

Best for:
- API backends that need to fulfill requests on payment confirmation
- Automated pipelines triggered by agent payments
- Any workflow where a payment should cause an action

Convoy handles retries with exponential backoff, dead letter queues for failed deliveries, and delivery logging. The direct httpx fast path fires simultaneously for lowest-latency delivery.

### The Payload

Both modes deliver the same enriched payload structure:

```json
{
  "id": "evt_abc123",
  "type": "payment.finalized",
  "mode": "execute",
  "timestamp": 1709856000,
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
      "tx_hash": "0x...",
      "from_address": "0xAgent...",
      "to_address": "0xYourAPI...",
      "amount": "5000000",
      "nonce": "0x...",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "identity": {
      "address": "0xAgent...",
      "agent_class": "trading-bot",
      "deployer": "0xDeployer...",
      "capabilities": ["swap", "limit-order"],
      "reputation_score": 85.0
    }
  }
}
```

Every payload includes a nested `execution` block with `state` (`provisional`, `confirmed`, `finalized`, `reorged`), `safe_to_execute` (true only after finality), `trust_source` (`facilitator` or `onchain`), and `finality` data. Your application branches on `execution.safe_to_execute` without making a single chain call.

---

## The Protocols

TripWire is built on three open standards. Here is what they do and why they matter, without the jargon.

### x402 -- HTTP Payments

x402 turns HTTP's `402 Payment Required` status code into a real payment flow. When your API returns a 402, it includes the price and a payment address. The client signs a USDC transfer authorization and resends the request. The server verifies the signature and serves the response. The actual USDC transfer settles onchain in the background.

The result: **any HTTP client can pay for any HTTP API**, programmatically, without credit cards, invoices, or payment processors.

### ERC-3009 -- Gasless Transfers

ERC-3009 (`transferWithAuthorization`) lets a user authorize a USDC transfer with a cryptographic signature rather than an onchain transaction. The payer signs; a relayer submits. The payer never needs ETH for gas. This is what makes x402 practical -- agents can pay for API calls without managing gas tokens on every chain.

### ERC-8004 -- AI Agent Identity

ERC-8004 is an onchain identity registry for AI agents, deployed on mainnet in January 2026. Every registered agent gets:
- A unique **agent ID** (ERC-721 NFT)
- An **agent class** (e.g., `trading-bot`, `data-oracle`, `payment-agent`)
- **Capabilities** declared on-chain
- A **reputation score** aggregated from onchain feedback

TripWire queries the ERC-8004 registry in real time and enriches every webhook payload with the sender's identity. This means your application can enforce policies like "only accept payments from agents with reputation above 70" or "only serve trading-bot class agents" -- without writing a single line of identity code.

---

## TripWire vs. Stripe Webhooks

If you have used Stripe Webhooks, TripWire will feel familiar. The core model is the same: register an endpoint, receive signed payloads, verify and execute. The difference is what sits underneath.

| | **Stripe Webhooks** | **TripWire** |
|---|---|---|
| **Payment rail** | Card networks, ACH, wire | ERC-3009 on EVM chains (Base, Ethereum, Arbitrum) |
| **Settlement** | 2-7 business days | Seconds to minutes (block finality) |
| **Payer identity** | Email + billing address | Onchain wallet + ERC-8004 agent identity |
| **Event source** | Stripe's internal ledger | Onchain ERC-3009 `TransferWithAuthorization` events |
| **Delivery** | Stripe infrastructure | Convoy self-hosted + direct POST (dual-path delivery) |
| **Signing** | Stripe HMAC | HMAC-SHA256 (X-TripWire-Signature header) |
| **Retries** | Exponential backoff, 72h | Exponential backoff, configurable |
| **Payer type** | Humans with credit cards | Humans and autonomous AI agents |
| **Policies** | Radar fraud rules | Policy engine (amount, sender, agent class, reputation) |
| **Integration** | `stripe.webhookEndpoints.create()` | `client.register_endpoint()` |
| **Identity enrichment** | None | Agent class, capabilities, reputation score |
| **Real-time mode** | None | Supabase Realtime subscriptions (Notify mode) |

The mental model transfers directly. The capabilities are built for what comes next.

---

## The Agent Economy

The next wave of internet commerce will not be driven by humans clicking checkout buttons. It will be driven by AI agents calling APIs, paying in USDC, and executing workflows autonomously.

This shift creates a new category of infrastructure need:

- **Agent-to-API payments** happen 24/7, at machine speed, across chains. You cannot process them with batch jobs or manual review.
- **Agent identity matters.** When a bot pays your API, you need to know: who deployed it? What class of agent is it? What is its reputation? Can you trust it?
- **Policy enforcement must be automatic.** At agent scale, you cannot manually review every transaction. Policies -- minimum amounts, sender allowlists, reputation thresholds, agent class requirements -- must evaluate in real time.
- **Webhook delivery must be reliable.** When an agent pays for a computation, it expects a result. Failed deliveries mean failed agents, which means lost revenue.

TripWire is built for this world. Every payload is enriched with ERC-8004 identity data. Every delivery is filtered through a policy engine. Every webhook is signed, retried, and logged. The infrastructure is agent-ready from day one.

---

## Business Model

### Pricing

| Tier | Price | Volume | Includes |
|---|---|---|---|
| **Free** | $0 | Up to 10,000 events/month | Both modes, 3 chains, community support |
| **Scale** | $0.003/event | 10,001+ events/month | Priority support, advanced policies, SLA |
| **Enterprise** | Custom | Unlimited | Dedicated infrastructure, custom integrations, onboarding |

### Unit Economics

TripWire's cost structure scales linearly with event volume:

- **Goldsky indexing**: ~$0.0005/event (chain data pipeline)
- **Supabase**: ~$0.0002/event (storage + realtime)
- **Convoy**: self-hosted (near zero marginal cost)
- **Compute**: ~$0.0002/event (verification, policy, identity)

**Total cost per event: ~$0.0012**
**Revenue per event (Scale tier): $0.003**
**Gross margin: ~60%**

Break-even lands at roughly **98,500 events per month** -- approximately 330 active agents each making 10 API calls per day. In a market where agent-to-API transactions are growing exponentially, this threshold is conservative.

---

## Roadmap

### Phase 1 -- Foundation (Current)

Core infrastructure for programmable onchain event triggers:
- Goldsky Turbo-powered chain indexing for ERC-3009 events on Base, Ethereum, and Arbitrum
- **Dynamic trigger registry** -- create triggers for any EVM event via MCP or API, no deploy needed
- **Unified processing pipeline** (C2) -- single code path for ERC-3009 and dynamic triggers with finality, policy, identity, and metrics
- **Per-trigger payment gating** (C3) -- gate dispatch on payment amount and token requirements
- **Decoder abstraction** (C1) -- `Decoder` protocol with `DecodedEvent` envelope, `ERC3009Decoder`, `AbiGenericDecoder`
- Finality tracking with per-chain confirmation depths
- Nonce-based replay protection with facilitator-Goldsky correlation
- Notify mode (Supabase Realtime) and Execute mode (Convoy webhook delivery)
- Policy engine with amount, sender, agent class, reputation, and finality depth filters
- ERC-8004 identity resolution with reputation scoring
- MCP server with 8 tools, 3-tier auth (PUBLIC / SIWX / X402), x402 Bazaar
- Execution state lifecycle: `provisional` → `confirmed` → `finalized` (with `reorged` branch)
- Redis Streams event bus for horizontal scaling (optional, feature-flagged)
- Python SDK (`tripwire-sdk`)
- REST API for endpoint and subscription management

### Phase 2 -- Scale

Expanding chain coverage, delivery options, and developer tooling:
- Additional EVM chains (Optimism, Polygon, Avalanche)
- TypeScript/JavaScript SDK
- Webhook delivery dashboard with replay and debugging
- Advanced policy conditions (time-based, rate limiting, composite rules)
- Batch event queries and analytics API
- SOC 2 compliance

### Phase 3 -- Platform

Becoming the infrastructure standard for agentic commerce:
- Non-EVM chain support (Solana, Aptos)
- Agent-to-agent payment routing
- Marketplace for policy templates and integrations
- Multi-tenant white-label deployment
- On-premise / self-hosted option for enterprise
- Governance and community-driven protocol extensions

---

## Architecture at a Glance

```
L0  Chain         Base / Ethereum / Arbitrum (ERC-3009 + any EVM event)
                          |
L1  Indexing       Goldsky Turbo --> Webhook POST to TripWire /ingest
                          |
L2  Middleware     TripWire (decode, filter, payment gate, dedup, finality, identity, policy)
                          |
L3  Delivery       Convoy webhooks (Execute) | Supabase Realtime (Notify)
                          |
L4  Application    Your API (execute business logic)
                          |
L5  MCP Server     8 tools for AI agent trigger CRUD (3-tier auth)
```

Six layers. One integration point. Every layer is managed, scalable, and observable.

---

## Get Started

```bash
pip install tripwire-sdk
```

```python
from tripwire_sdk import TripwireClient

async with TripwireClient(api_key="tw_...") as client:
    # Register a webhook endpoint on Base
    endpoint = await client.register_endpoint(
        url="https://your-api.com/x402/webhook",
        mode="execute",
        chains=[8453],
        recipient="0xYourAddress",
    )

    # List recent payment events
    events = await client.list_events(limit=10)
```

That is all it takes to go from "payments settle onchain" to "my application reacts to payments."

---

*TripWire is a programmable onchain event trigger platform for AI agents. x402 payments are the first use case. We handle the chain. You handle the product.*
