# TripWire Landing Page Structure Spec

**Version:** 1.0
**Date:** 2026-03-19
**Status:** Ready for frontend implementation
**Design System:** Dark theme, Inter (body) + JetBrains Mono (code), orange #FF4D00 + green #00E6A8 accents

---

## Global Rules

- **No mentions of:** REST API, tech stack internals (Goldsky, Convoy, Supabase, Redis, FastAPI), architecture layer diagrams (L0-L5)
- **No fake login button.** The only interactive auth element is a "Get API Key" CTA that links to the onboarding flow (future).
- **Voice:** Direct, concrete, confident. Speak to builders, not evaluators. Address both human developers and AI agents as first-class users.
- **Numbers over adjectives.** "3 chains, 100ms fast path, 99.9% delivery" -- not "blazing fast multi-chain support."
- **Dark aesthetic preserved.** All sections use the existing dark background (#0A0A0A or equivalent), with accent colors for interactive elements and highlights.

---

## Section 1: Hero

### Purpose
Communicate in 5 seconds: "Onchain events happen. TripWire tells your app when it's safe to act."

### Headline (primary)
```
Your app reacts to the chain.
TripWire makes sure it's safe.
```

### Headline (alternative options for A/B)
- "Onchain events in. Verified webhooks out."
- "The infrastructure between settlement and execution."

### Subtitle
```
Programmable onchain event triggers for AI agents and developers.
Payment webhooks. DeFi alerts. NFT monitors. One integration, every chain.
```

### Visual (right side)
Animated event flow -- a single glowing "pulse line" that travels left to right through three stages:

1. **Chain icon** (Base logo) with a small tx hash fading in -- represents the onchain event
2. **TripWire shield icon** with a brief green checkmark flash -- represents verification + finality
3. **Webhook arrow** landing on a code editor icon -- represents delivery to the developer's app

The animation loops every 4 seconds. Each stage takes ~1s with easing. The pulse line uses the green #00E6A8 color. On hover, the animation pauses and shows labels for each stage ("Event detected", "Verified + enriched", "Webhook delivered").

### Stats Row (below hero, horizontal strip)
Four stat cards, monospace font, with subtle count-up animation on scroll:

| Stat | Value | Label |
|------|-------|-------|
| 1 | 3 | Chains supported |
| 2 | ~100ms | Fastest detection |
| 3 | 99.9% | Webhook delivery rate |
| 4 | $0 | To start |

### CTA Buttons
- **Primary (orange #FF4D00):** "Start building -- it's free" --> links to onboarding/signup
- **Secondary (ghost/outline):** "See how it works" --> smooth scrolls to Section 2

### Nav Bar (sticky)
- Logo (left)
- Links: Products, How It Works, Pricing, Docs
- CTA button: "Get API Key" (orange, small)
- No login button. No hamburger menu on desktop.

---

## Section 2: See It In Action

### Purpose
Show two real-world scenarios end-to-end with concrete details. Replace abstract architecture with tangible stories the audience can project themselves into.

### Section Headline
```
See it in action
```

### Section Subtitle
```
Two products. Two real scenarios. Watch the event flow from chain to your code.
```

### Layout
Two interactive tabs (or toggle cards). Clicking a tab reveals a horizontal timeline animation specific to that product. Each tab has a colored indicator: orange for Keeper, green for Pulse.

---

### Tab 1: Keeper -- "AI agent pays for an API call"

**Scenario label:** "An AI trading agent pays $0.50 USDC for market data via x402"

**Timeline (5 steps, animated left to right with sequential reveal):**

| Step | Icon | Label | Detail text | Timing |
|------|------|-------|-------------|--------|
| 1 | Chain icon (Base) | Payment settles | `transferWithAuthorization` on Base. 0.50 USDC from `0x7a3f...` to `0xb2c1...`. Block #29,841,207. | "0s" |
| 2 | Lightning bolt | Fast path fires | TripWire detects the payment via facilitator pre-confirmation. Provisional event created. | "~100ms" |
| 3 | Shield + checkmark | Verified + finalized | 3/3 block confirmations on Base. Nonce checked (unique). ERC-8004 identity resolved: `trading-bot`, reputation 87. | "~4s" |
| 4 | Arrow hitting target | Webhook delivered | HMAC-signed POST to `https://data-api.example.com/webhook`. Payload includes `safe_to_execute: true`. | "~4.1s" |
| 5 | Gear turning | Your app executes | API returns market data to the agent. Payment logged. Done. | "~4.2s" |

**Below the timeline:** A mini code block (JetBrains Mono, dark card) showing the key part of the webhook payload the developer receives at step 4:

```json
{
  "type": "payment.finalized",
  "execution": {
    "state": "finalized",
    "safe_to_execute": true
  },
  "data": {
    "transfer": {
      "amount": "500000",
      "from_address": "0x7a3f...trading-agent",
      "chain_id": 8453
    },
    "identity": {
      "agent_class": "trading-bot",
      "reputation_score": 87.0
    }
  }
}
```

**Callout badge (green):** "Your app never touches the chain. TripWire handles detection, verification, identity, and delivery."

---

### Tab 2: Pulse -- "Monitor large USDC transfers on Arbitrum"

**Scenario label:** "A DeFi dashboard monitors USDC transfers over $10,000 on Arbitrum"

**Timeline (5 steps):**

| Step | Icon | Label | Detail text | Timing |
|------|------|-------|-------------|--------|
| 1 | Chain icon (Arbitrum) | Transfer detected | `Transfer(address,address,uint256)` on USDC contract `0xaf88...`. 15,000 USDC from `0x91a2...` to `0x44bf...`. Block #287,412,083. | "0s" |
| 2 | Filter funnel | Trigger matched | Trigger filter: `value >= 10000000000` (10,000 USDC). Match. 2 other transfers in this block were below threshold -- filtered out. | "~1s" |
| 3 | Shield + checkmark | Confirmed + finalized | 1/1 confirmation on Arbitrum (instant finality). Replay protection: nonce is unique. | "~2s" |
| 4 | Arrow hitting target | Webhook delivered | HMAC-signed POST to `https://dashboard.example.com/alerts`. Event type: `trigger.finalized`. | "~2.1s" |
| 5 | Dashboard icon | Dashboard updates | Real-time card appears: "$15,000 USDC transfer on Arbitrum." Team gets a Slack notification. | "~2.2s" |

**Below the timeline:** Mini code block showing the trigger definition (what the developer configured):

```json
{
  "name": "Large USDC transfers",
  "event_signature": "Transfer(address,address,uint256)",
  "contract_address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",
  "chain_ids": [42161],
  "filter_rules": [
    { "field": "value", "op": "gte", "value": "10000000000" }
  ]
}
```

**Callout badge (green):** "No RPC nodes. No indexer setup. Define what you care about, get a webhook when it happens."

---

## Section 3: Products

### Purpose
Position Keeper and Pulse as distinct, outcome-focused products. Not feature lists -- business outcomes.

### Section Headline
```
Two products. One platform.
```

### Section Subtitle
```
Deploy Keeper for payment webhooks, Pulse for any onchain event, or both.
```

### Layout
Two side-by-side cards, equal width. Orange left border for Keeper, green left border for Pulse. Each card has: product name, one-line tagline, 3 outcome bullets, a mini code snippet, and a CTA.

---

### Card 1: Keeper

**Product name:** Keeper
**Color accent:** Orange #FF4D00
**Icon:** Shield with a dollar sign

**Tagline:**
```
x402 payment webhooks that tell you when it's safe to execute.
```

**Three outcome bullets (each with a small icon):**

1. **Know the moment you're paid.** ~100ms detection via facilitator fast path, finalized confirmation in seconds. Your app never polls.
2. **Know who paid you.** Every webhook includes ERC-8004 identity: agent class, capabilities, reputation score. Gate access on trust, not just amount.
3. **Never double-process.** Nonce-based replay protection and idempotency keys built in. The same payment cannot trigger your app twice.

**Mini code snippet (4 lines, SDK quickstart):**
```python
async with TripwireClient(private_key=key) as client:
    await client.register_endpoint(
        url="https://your-api.com/webhook",
        mode="execute", chains=[8453],
    )
# That's it. You'll receive signed webhooks.
```

**CTA:** "Set up Keeper" --> onboarding flow (future product page)

---

### Card 2: Pulse

**Product name:** Pulse
**Color accent:** Green #00E6A8
**Icon:** Radio wave / broadcast signal

**Tagline:**
```
Watch any onchain event. Get a webhook when it matches.
```

**Three outcome bullets:**

1. **Monitor anything on-chain.** ERC-20 transfers, DeFi swaps, NFT mints, governance votes -- if it emits an event, Pulse can watch it.
2. **Filter at the source.** Define trigger rules (amount thresholds, specific contracts, sender allowlists). Only matching events reach your app.
3. **No infrastructure to manage.** No RPC nodes, no indexer config, no reorg handling. Define a trigger, get webhooks.

**Mini code snippet (MCP tool call -- shows the AI agent path):**
```json
{
  "method": "tools/call",
  "params": {
    "name": "create_trigger",
    "arguments": {
      "endpoint_id": "ep_abc123",
      "event_signature": "Transfer(address,address,uint256)",
      "contract_address": "0x833589fCD...USDC",
      "filter_rules": [
        { "field": "value", "op": "gte", "value": "1000000000" }
      ]
    }
  }
}
```

**CTA:** "Set up Pulse" --> onboarding flow (future product page)

---

## Section 4: How Developers Integrate

### Purpose
Show the developer's actual experience -- not internal architecture. Two paths: Python SDK (for developers) and MCP (for AI agents). The message: "You're 5 minutes from your first webhook."

### Section Headline
```
Five minutes to your first webhook
```

### Section Subtitle
```
Two integration paths. Same verified payloads.
```

### Layout
Two-column layout with a toggle/tab selector at the top: "Python SDK" (left, default) and "MCP for AI Agents" (right). Each tab reveals a vertical 3-step flow with code blocks.

---

### Path 1: Python SDK

**Path label:** "For developers"
**Icon:** Python logo (subtle, monochrome)

**Step 1: Install**
```bash
pip install tripwire-sdk
```
Small note below: "Supports Python 3.11+. TypeScript SDK coming soon."

**Step 2: Register your endpoint**
```python
from tripwire_sdk import TripwireClient

async with TripwireClient(private_key=key) as client:
    endpoint = await client.register_endpoint(
        url="https://your-api.com/webhook",
        mode="execute",
        chains=[8453],           # Base
        recipient="0xYourAddress",
        policies={
            "min_amount": "1000000",      # 1 USDC minimum
            "min_reputation_score": 70,   # Trusted agents only
        },
    )
    print(f"Endpoint live: {endpoint.id}")
```

**Step 3: Handle the webhook**
```python
from tripwire_sdk import verify_webhook_signature, WebhookPayload

@app.post("/webhook")
async def handle(request: Request):
    body = await request.body()
    verify_webhook_signature(body, dict(request.headers), SECRET)

    payload = WebhookPayload.model_validate_json(body)

    if payload.execution.safe_to_execute:
        # Payment is finalized on-chain. Safe to act.
        fulfill_order(payload.data.transfer)

    return {"status": "ok"}
```

**Annotation below step 3:** "Branch on `safe_to_execute`. Show a spinner on `provisional`. Commit on `finalized`. Roll back on `reorged`."

---

### Path 2: MCP for AI Agents

**Path label:** "For AI agents"
**Icon:** Robot/agent icon (subtle, monochrome)

**Step 1: Discover**
```
Agent fetches GET /discovery/resources
--> finds TripWire's MCP endpoint and available tools
```
Small note: "x402 Bazaar compatible. Your agent discovers TripWire automatically."

**Step 2: Register via MCP tool call**
```json
{
  "jsonrpc": "2.0",
  "method": "tools/call",
  "params": {
    "name": "register_middleware",
    "arguments": {
      "url": "https://my-agent.example.com/webhook",
      "mode": "execute",
      "chains": [8453],
      "template_slugs": ["x402-usdc-payment"],
      "custom_triggers": [{
        "event_signature": "Transfer(address,address,uint256)",
        "name": "Large USDC transfers",
        "filter_rules": [
          { "field": "value", "op": "gte", "value": "1000000000" }
        ]
      }]
    }
  }
}
```
Small note: "One tool call sets up your endpoint AND your triggers. Costs $0.003 USDC via x402."

**Step 3: Receive webhooks**
```
Same HMAC-signed payloads, same execution states,
same safe_to_execute flag.

Your agent processes webhooks the same way
whether it registered via SDK or MCP.
```

**Annotation below step 3:** "8 MCP tools available. Manage triggers, browse templates, search events -- all via JSON-RPC."

---

## Section 5: The Webhook Payload

### Purpose
Show exactly what lands on the developer's server. This section already works well -- enhance it with the execution state timeline.

### Section Headline
```
What you receive
```

### Section Subtitle
```
Every webhook is HMAC-signed, enriched with identity data,
and tagged with an execution state you can trust.
```

### Layout
Three-part vertical layout:

1. **Execution state timeline** (top, horizontal)
2. **Full payload code block** (center)
3. **Field reference table** (bottom)

---

### Part 1: Execution State Timeline

A horizontal timeline with 4 nodes. The active node glows. Clicking a node updates the payload below to show what that state looks like.

```
[PROVISIONAL] ----> [CONFIRMED] ----> [FINALIZED]
                                          |
                                     [REORGED] (branch off, red)
```

**Node details (shown on hover/click):**

| State | safe_to_execute | trust_source | What your app should do |
|-------|----------------|--------------|------------------------|
| **Provisional** | `false` | `facilitator` | Show a pending indicator. Do NOT execute. |
| **Confirmed** | `false` | `onchain` | Event is on-chain but not yet final. Still wait. |
| **Finalized** | `true` | `onchain` | Safe to execute. Fulfill the order, grant access, trigger the workflow. |
| **Reorged** | `false` | `onchain` | Chain reorganization detected. Roll back any provisional actions. |

Each node should animate with a color: orange for provisional, yellow for confirmed, green for finalized, red for reorged.

---

### Part 2: Full Payload Code Block

Dark card, JetBrains Mono, syntax highlighted. The `execution` block should be highlighted with a subtle green border/glow when "Finalized" is the selected state.

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
      "tx_hash": "0x9f86d081884c7d659a2feaa0c55ad015a3bf4f1b...",
      "block_number": 28451023,
      "from_address": "0x7a3f...91c2",
      "to_address": "0xb2c1...8f3a",
      "amount": "5000000",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "identity": {
      "address": "0x7a3f...91c2",
      "agent_class": "trading-bot",
      "deployer": "0xdeadbeef...abcd",
      "capabilities": ["swap", "limit-order"],
      "reputation_score": 87.5
    }
  }
}
```

**Interactive annotations:** Hovering over sections of the payload shows tooltip explanations:
- Hover `execution` block: "The execution state tells you whether it's safe to act."
- Hover `identity` block: "ERC-8004 identity data. Resolved automatically for every sender."
- Hover `idempotency_key`: "Deterministic. Safe to deduplicate on your end."
- Hover `amount`: "In smallest units. 5000000 = 5.00 USDC."

---

### Part 3: Field Reference (compact table)

| Field | What it tells you |
|-------|------------------|
| `execution.state` | How far the event has progressed: `provisional` -> `confirmed` -> `finalized` |
| `execution.safe_to_execute` | The one field that matters. `true` = safe to commit irreversible actions. |
| `execution.trust_source` | Who vouches: `facilitator` (pre-settlement) or `onchain` (verified on-chain) |
| `idempotency_key` | Deterministic SHA-256. Use it to deduplicate on your end. |
| `data.identity` | ERC-8004 agent identity: class, capabilities, reputation. Auto-resolved. |

---

## Section 6: Features Grid

### Purpose
Six benefit-focused feature cards. Each card: icon, headline (benefit), one-sentence description. No jargon.

### Section Headline
```
Built for what comes next
```

### Section Subtitle
```
Every feature exists because we've seen what breaks
when AI agents start transacting at machine speed.
```

### Layout
3x2 grid of cards. Each card has a subtle border that glows on hover (alternating orange and green accents).

---

### Card 1: Execution States
**Icon:** Traffic light (or state machine icon)
**Headline:** "Know exactly when to act"
**Description:** "Every event progresses through provisional, confirmed, and finalized states. Branch your logic on `safe_to_execute` -- never guess whether a payment is real."

### Card 2: Agent Identity
**Icon:** Fingerprint / ID badge
**Headline:** "Know exactly who's paying"
**Description:** "Every webhook includes ERC-8004 identity: agent class, capabilities, deployer, reputation score. Enforce trust policies without writing identity code."

### Card 3: Policy Engine
**Icon:** Shield with rules / filter
**Headline:** "Automate your acceptance criteria"
**Description:** "Set minimum payment amounts, reputation thresholds, sender allowlists, and agent class requirements. Policies evaluate in real time -- no manual review at agent scale."

### Card 4: Guaranteed Delivery
**Icon:** Checkmark in a circle / delivery truck
**Headline:** "Every webhook lands"
**Description:** "HMAC-signed payloads, 10 automatic retries with exponential backoff, and a dead-letter queue for anything that fails. 99.9% delivery rate."

### Card 5: Replay Protection
**Icon:** Lock / shield with X
**Headline:** "Never process the same event twice"
**Description:** "Nonce-based deduplication at the database level plus deterministic idempotency keys. The same on-chain transfer cannot trigger your app twice, even across restarts."

### Card 6: MCP Tools
**Icon:** Robot arm / tool
**Headline:** "AI agents manage their own triggers"
**Description:** "8 MCP tools let agents discover, create, and manage triggers via JSON-RPC. Authenticate with wallet signatures or pay-per-call with x402 micropayments."

---

## Section 7: Chains + Social Proof

### Purpose
Build confidence through supported chains, protocol standards, and concrete performance numbers. This is not a "logos of customers" section (too early for that) -- it's a "built on open standards, running on real chains" section.

### Section Headline
```
Live on mainnet
```

### Section Subtitle
```
Three chains. Two open standards. Running now.
```

### Layout
Three rows:

---

### Row 1: Supported Chains
Three chain cards, horizontal. Each card: chain logo, chain name, finality depth, and a subtle "live" indicator (green dot).

| Chain | Logo | Finality | Note |
|-------|------|----------|------|
| Base | Base logo | 3 blocks (~6s) | Primary chain for x402 |
| Ethereum | Ethereum logo | 12 blocks (~2.5 min) | Full ERC-3009 support |
| Arbitrum | Arbitrum logo | 1 block (instant) | Fastest finality |

Small text below: "Optimism, Polygon, and Avalanche coming in Phase 2."

---

### Row 2: Protocol Standards
Two protocol badges with brief explanations. No deep technical content -- just enough for credibility.

**Badge 1: x402**
- Icon/logo: "x402" in monospace
- One-liner: "HTTP-native micropayments. Any client pays any API with USDC -- no credit cards, no invoices."

**Badge 2: ERC-8004**
- Icon/logo: "ERC-8004" in monospace
- One-liner: "Onchain AI agent identity. Every registered agent gets a class, capabilities, and a reputation score."

---

### Row 3: Performance Stats (alternative to customer logos)
Four large stat numbers, same style as hero stats row but bigger, with brief context:

| Stat | Value | Context |
|------|-------|---------|
| Uptime | 99.9% | Since launch |
| Delivery rate | 99.9% | Webhooks successfully delivered |
| Fast path | ~100ms | Payment detection to first webhook |
| Finality | 1-12 blocks | Chain-specific, fully tracked |

---

## Section 8: Pricing

### Purpose
Clear, simple pricing. Three tiers. Show what's included per product (Keeper vs Pulse vs both).

### Section Headline
```
Start free. Scale when you're ready.
```

### Section Subtitle
```
Every tier includes both products. No feature gates on Starter.
```

### Layout
Three pricing cards, center-aligned. The middle card ("Scale") is slightly elevated / highlighted as the recommended tier.

---

### Tier 1: Starter (Free)

**Price:** $0/month
**Volume:** Up to 10,000 events/month
**Badge:** "Free forever"

**Includes:**
- Keeper (x402 payment webhooks)
- Pulse (custom onchain triggers)
- 3 chains (Base, Ethereum, Arbitrum)
- HMAC-signed webhook delivery
- Execution state lifecycle (provisional -> finalized)
- ERC-8004 identity enrichment
- Replay protection
- Community support
- Up to 5 endpoints
- Up to 10 triggers

**CTA:** "Start building" (primary orange button)

---

### Tier 2: Scale

**Price:** $0.003 / event
**Volume:** 10,001+ events/month
**Badge:** "Most popular" (highlighted)

**Everything in Starter, plus:**
- Unlimited events
- Unlimited endpoints and triggers
- Priority support (48h response)
- Advanced policy engine (time-based rules, rate limiting, composite conditions)
- Webhook delivery dashboard with replay
- Custom finality depth per endpoint
- SLA: 99.9% uptime, 99.9% delivery

**CTA:** "Get started" (primary orange button)

---

### Tier 3: Enterprise

**Price:** Custom
**Volume:** Unlimited
**Badge:** "Contact us"

**Everything in Scale, plus:**
- Dedicated infrastructure
- Custom integrations and onboarding
- SOC 2 compliance (Phase 2)
- On-premise / self-hosted option
- Direct engineering support
- Custom SLA

**CTA:** "Talk to us" (secondary ghost button) --> opens contact/calendly

---

### Pricing footnote
Small text below the cards:
"All tiers include both Keeper and Pulse. Event pricing applies to delivered webhooks -- filtered events don't count. x402 MCP tool calls are priced separately ($0.001-$0.003 per call)."

---

## Section 9: Final CTA + Footer

### Purpose
One last push to convert. Then a clean footer with essential links.

---

### CTA Block

**Background:** Subtle gradient from dark to slightly lighter, with a faint grid pattern.

**Headline:**
```
Your app shouldn't have to watch the chain.
```

**Subtitle:**
```
Register an endpoint. Define your policies.
Start receiving verified webhooks in minutes.
```

**Two buttons, centered:**
- **Primary (orange, large):** "Start building -- it's free"
- **Secondary (ghost):** "Read the docs"

**Below buttons, single line in monospace:**
```
pip install tripwire-sdk
```
(clickable -- copies to clipboard on click)

---

### Footer

**Layout:** 4-column grid on desktop, stacked on mobile.

**Column 1: TripWire**
- Logo
- One-liner: "Programmable onchain event triggers."
- Copyright: "(c) 2026 TripWire. All rights reserved."

**Column 2: Products**
- Keeper
- Pulse
- Pricing
- Changelog

**Column 3: Developers**
- Documentation
- SDK Reference
- MCP Server
- Webhook Payload Format
- Status Page

**Column 4: Company**
- About
- Contact
- Twitter/X
- GitHub

**Bottom bar:** Privacy Policy | Terms of Service

---

## Responsive Behavior Notes

### Mobile (< 768px)
- Hero: Stack vertically. Animation moves below headline. Stats row becomes 2x2 grid.
- Section 2 (See It In Action): Tabs stack. Timeline becomes vertical (top to bottom instead of left to right).
- Section 3 (Products): Cards stack vertically.
- Section 4 (Integration): Tabs stack. Code blocks are full-width with horizontal scroll.
- Section 5 (Payload): Execution timeline becomes vertical. Payload code block has horizontal scroll.
- Section 6 (Features): 2x3 grid becomes single column.
- Section 7 (Chains): Chain cards stack. Stats become 2x2.
- Section 8 (Pricing): Cards stack vertically, Scale card stays highlighted.
- Footer: Single column.

### Tablet (768px - 1024px)
- Section 6: 2x3 grid.
- Section 8: Three cards, slightly compressed.
- Everything else: Same as desktop with tighter spacing.

---

## Animation Notes (GSAP)

All animations use GSAP ScrollTrigger. Key behaviors:

1. **Hero pulse animation:** GSAP timeline, loops infinitely, 4s duration. Uses `motionPath` for the pulse traveling across the three stages.
2. **Stats count-up:** `ScrollTrigger` fires once when stats row enters viewport. Numbers count from 0 to final value over 1.5s.
3. **Section 2 timeline:** Sequential reveal with `stagger: 0.3`. Each step fades in and slides from left. The connecting line draws itself with `drawSVG`.
4. **Section 5 state timeline:** Click/hover interaction (not scroll-triggered). Active state node scales up 1.2x, inactive nodes dim to 40% opacity.
5. **Feature cards (Section 6):** Fade in with `stagger: 0.15` on scroll. Border glow on hover uses CSS transition (not GSAP) for performance.
6. **Section 9 CTA:** Fade up on scroll. The `pip install` line has a typing animation (GSAP `TextPlugin` or CSS keyframes).

**Performance rule:** No animation runs if `prefers-reduced-motion: reduce` is set. All ScrollTrigger animations use `once: true` so they don't replay on scroll-up.

---

## Content Inventory (what the frontend team needs from other teams)

| Item | Owner | Status |
|------|-------|--------|
| Chain logos (Base, Ethereum, Arbitrum) | Design | Needed -- use official logos, monochrome white versions |
| x402 and ERC-8004 wordmarks/badges | Design | Needed -- simple monospace text badges are fine |
| Product icons (Keeper shield, Pulse broadcast) | Design | Needed |
| Feature card icons (6 total) | Design | Needed |
| Hero animation assets (chain icon, shield, webhook arrow) | Design + Frontend | Needed |
| SDK code examples | Backend (available now in `sdk/examples/`) | Done |
| MCP tool call examples | Backend (available now in `docs/MCP-SERVER.md`) | Done |
| Webhook payload JSON | Backend (available now in `README.md`) | Done |
| Performance stats (uptime, delivery rate) | Ops | Needed -- placeholder values in spec, confirm before launch |
| Pricing confirmation | Business | Needed -- confirm event pricing and tier boundaries |
| Status page URL | Ops | Needed |
| Onboarding flow URL (for CTA links) | Product | Needed -- placeholder until onboarding is built |
