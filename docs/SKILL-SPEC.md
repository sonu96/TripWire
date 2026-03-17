# TWSS-1: TripWire Skill Spec

**Version:** 1.0.0-draft
**Status:** Draft
**Date:** 2026-03-17
**Authors:** TripWire Team

> Everyone defines skills as "what to do." This spec defines "when it's safe to do it."

---

## 1. Overview

TWSS-1 defines execution-aware skills for onchain AI agent systems. A TripWire
Skill is a programmable onchain event trigger that carries execution semantics
-- lifecycle state, trust attribution, safety guarantees, payment gating, and
identity verification.

Skills are the atomic unit of the TripWire platform. They are discoverable,
composable, and monetizable via the x402 Bazaar.

### 1.1 Design Principles

- **Simple over complete.** V1 covers the 80% case.
- **Opinionated over flexible.** One way to express execution safety.
- **Implementable over theoretical.** Every field maps to running code.

### 1.2 What This Spec Covers

| Section | Purpose |
|---------|---------|
| Execution Semantics | The core: lifecycle, trust, safety |
| Two-Phase Execution | prepare (provisional) / commit (finalized) |
| Three-Layer Gating | can_pay? can_trust? is_safe? |
| Skill Definition | Schema for declaring a skill |
| Skill Output Contract | Schema for skill results |
| Skill Lifecycle | draft / active / deprecated / archived |
| Determinism Guarantees | What agents can rely on |
| Integration Points | MCP, x402, ERC-8004 |

---

## 2. Execution Semantics (CORE)

This is the centerpiece. Every other section builds on this.

### 2.1 Execution State Lifecycle

An onchain event progresses through states. A skill's output reflects
which state the triggering event has reached.

```
              [EVENT DETECTED]
                    |
                    v
             +--------------+
             | PROVISIONAL  |  ~100ms    facilitator verified signature
             | prepare()    |            tx NOT yet onchain
             +--------------+
                    |
          tx lands onchain
                    |
                    v
             +--------------+
             | CONFIRMED    |  ~1-13s    1+ block confirmations
             |              |            onchain but not yet final
             +--------------+
                    |
        +-----------+-----------+
        |                       |
  finality reached        block reorged
        |                       |
        v                       v
 +--------------+        +--------------+
 | FINALIZED    |        | REORGED      |
 | commit()     |        | rollback()   |
 | safe = true  |        | safe = false |
 +--------------+        +--------------+
```

### 2.2 State Definitions

| State | Confirmations | Trust Source | safe_to_execute | Agent Action |
|-------|---------------|-------------|-----------------|--------------|
| `provisional` | 0 | `facilitator` | `false` | Show spinner. Preview only. |
| `confirmed` | 1+ | `onchain` | `false` | Update balances. No irreversible ops. |
| `finalized` | chain depth* | `onchain` | `true` | Transfer funds. Mint. Grant access. |
| `reorged` | invalidated | `onchain` | `false` | Undo everything. Notify user. |

*Chain finality depths: Ethereum 12, Base 3, Arbitrum 1. Configurable per-endpoint.

### 2.3 Trust Sources

| Source | Meaning | Confidence |
|--------|---------|------------|
| `facilitator` | Off-chain signature verification (x402) | ~0.99 |
| `onchain` | Block confirmations verified via RPC | 1.0 |

---

## 3. Two-Phase Execution Model

This is the killer concept. Skills operate in two phases:

### 3.1 prepare() -- Provisional Phase

Triggered when the facilitator verifies an ERC-3009 signature but the
transaction is not yet onchain. Latency: ~100ms.

```
Agent receives: execution.state = "provisional"
Agent action:   prepare() -- optimistic UI, hold resources, do NOT commit
```

The agent CAN:
- Show success UI to the user
- Reserve inventory
- Start background processing

The agent MUST NOT:
- Transfer funds
- Mint tokens
- Grant permanent access
- Write irreversible state

### 3.2 commit() -- Finalized Phase

Triggered when the transaction reaches chain-specific finality depth.
Latency: 250ms (Arbitrum) to ~2.5min (Ethereum).

```
Agent receives: execution.state = "finalized", execution.safe_to_execute = true
Agent action:   commit() -- execute irreversible business logic
```

The agent CAN:
- Transfer funds
- Mint tokens
- Grant permanent access
- Finalize orders

### 3.3 rollback() -- Reorg Phase

Triggered when a block reorganization invalidates the transaction.

```
Agent receives: execution.state = "reorged"
Agent action:   rollback() -- undo prepare(), restore previous state
```

---

## 4. Three-Layer Gating

Every skill invocation passes through three gates. All three must pass.

```
  Event arrives
       |
       v
  [1. CAN PAY?]     Payment gating
       |
       v
  [2. CAN TRUST?]   Identity + reputation gating
       |
       v
  [3. IS SAFE?]      Execution state gating
       |
       v
  Skill executes
```

### 4.1 Layer 1: Payment Gate (`can_pay?`)

Does this event carry sufficient payment?

**MCP tool level:**

| Field | Type | Description |
|-------|------|-------------|
| `auth_tier` | enum | `PUBLIC` / `SIWX` / `X402` |
| `price` | string | Per-invocation price (e.g. `"$0.003"`) |
| `network` | string | CAIP-2 chain (e.g. `"eip155:8453"`) |

**Event trigger level:**

| Field | Type | Description |
|-------|------|-------------|
| `require_payment` | bool | Gate on payment metadata |
| `payment_token` | address / null | Required token (null = any) |
| `min_payment_amount` | string / null | Minimum in smallest unit |

### 4.2 Layer 2: Identity Gate (`can_trust?`)

Is the sender a known, reputable agent?

| Field | Type | Description |
|-------|------|-------------|
| `min_reputation` | float (0-100) | Minimum ERC-8004 reputation score |
| `required_agent_class` | string / null | Required agent type (e.g. `"trading-bot"`) |

Identity is resolved from ERC-8004 onchain registry. Reputation is
aggregated from onchain feedback. Cache TTL: 300s.

### 4.3 Layer 3: Execution Gate (`is_safe?`)

Has the triggering event reached sufficient finality?

| Field | Type | Description |
|-------|------|-------------|
| `execution.state` | enum | Current lifecycle state |
| `execution.safe_to_execute` | bool | `true` only when finalized |
| `execution.finality.confirmations` | int | Current block depth |
| `execution.finality.required` | int | Required depth for this endpoint |

`safe_to_execute = true` requires ALL of:
- `state == "finalized"`
- `confirmations >= required`
- No reorg detected
- `trust_source == "onchain"`

---

## 5. Skill Definition Schema

A skill definition declares what the skill watches for and how it behaves.

```json
{
  "name": "whale-usdc-transfer",
  "slug": "whale-usdc-transfer",
  "version": "1.0.0",
  "description": "Fires when a USDC transfer exceeds a threshold",
  "category": "payments",
  "author": "0xAuthorAddress",

  "event": {
    "signature": "Transfer(address,address,uint256)",
    "abi": [{"type": "event", "name": "Transfer", "inputs": [...]}],
    "chains": [8453, 1, 42161],
    "contract": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
  },

  "parameters": [
    {
      "name": "threshold",
      "type": "uint256",
      "required": true,
      "description": "Minimum transfer amount (smallest unit)"
    }
  ],

  "filters": [
    {"field": "value", "op": "gte", "value": "${threshold}"}
  ],

  "gating": {
    "payment": {
      "require_payment": false
    },
    "identity": {
      "min_reputation": 0.0,
      "required_agent_class": null
    }
  },

  "delivery": {
    "webhook_event_type": "transfer.whale",
    "modes": ["execute", "notify"]
  }
}
```

### 5.1 Required Fields

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Human-readable name |
| `slug` | string | URL-safe identifier (kebab-case) |
| `version` | string | Semantic version |
| `event.signature` | string | Solidity event signature |
| `event.abi` | array | ABI fragment for decoding |
| `delivery.webhook_event_type` | string | Event type in webhook payload |

### 5.2 Optional Fields

| Field | Default | Description |
|-------|---------|-------------|
| `event.chains` | `[8453]` | Supported chain IDs |
| `event.contract` | `null` | Specific contract (null = any) |
| `parameters` | `[]` | User-configurable inputs |
| `filters` | `[]` | JMESPath filter rules |
| `gating.payment` | `{require_payment: false}` | Payment requirements |
| `gating.identity` | `{min_reputation: 0}` | Identity requirements |

---

## 6. Skill Output Contract

This is the canonical output format. Every TripWire skill produces this.

```json
{
  "id": "evt_a1b2c3d4",
  "idempotency_key": "sha256:8453:0xabc...:7:ep_xyz:finalized",
  "type": "transfer.whale",
  "version": "v1",
  "timestamp": 1710700800,

  "execution": {
    "state": "finalized",
    "safe_to_execute": true,
    "trust_source": "onchain",
    "finality": {
      "confirmations": 3,
      "required": 3,
      "is_finalized": true
    }
  },

  "data": {
    "chain_id": 8453,
    "tx_hash": "0x9f86d081...",
    "block_number": 28451023,
    "from_address": "0x1234...5678",
    "to_address": "0xabcd...efab",
    "amount": "5000000000",
    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "decoded_fields": {}
  },

  "identity": {
    "address": "0x1234...5678",
    "agent_class": "trading-bot",
    "reputation_score": 87.5,
    "capabilities": ["swap", "limit-order"]
  }
}
```

### 6.1 The `execution` Block (Required)

Every skill output MUST include this block. It is the contract between
TripWire and the consuming agent.

```json
{
  "execution": {
    "state": "provisional | confirmed | finalized | reorged",
    "safe_to_execute": false,
    "trust_source": "facilitator | onchain",
    "finality": {
      "confirmations": 0,
      "required": 3,
      "is_finalized": false
    }
  }
}
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `state` | enum | yes | Current lifecycle state |
| `safe_to_execute` | bool | yes | Gate for irreversible actions |
| `trust_source` | enum | yes | Who vouches for this state |
| `finality` | object/null | yes | Block confirmation data (null if provisional) |

### 6.2 The `identity` Block (Optional)

Present when the sender is a registered ERC-8004 agent.

| Field | Type | Description |
|-------|------|-------------|
| `address` | string | Agent wallet |
| `agent_class` | string | Registered type |
| `reputation_score` | float | 0-100 aggregated score |
| `capabilities` | string[] | Declared capabilities |

### 6.3 The `idempotency_key` (Required)

Deterministic. Derived from `SHA256(chain_id : tx_hash : log_index : endpoint_id : event_type)`.

Consuming agents MUST deduplicate on this key. TripWire guarantees:
same event + same endpoint + same type = same idempotency_key.

---

## 7. Determinism Guarantees

What agents can rely on:

### 7.1 Idempotency

Given the same onchain event, TripWire produces the same `idempotency_key`.
Replays (Convoy retries, DLQ reprocessing) are safe to deduplicate.

### 7.2 Ordering

Events for the same `event_id` are delivered in lifecycle order:
`provisional` -> `confirmed` -> `finalized`. A `reorged` event may
arrive at any point after `provisional`.

### 7.3 At-Least-Once Delivery

Every event is delivered at least once via Convoy (10 retries, exponential
backoff, DLQ). Consuming agents MUST handle duplicates via `idempotency_key`.

### 7.4 Finality Monotonicity

Once `safe_to_execute = true` is delivered for an event, it will not be
revoked unless a `reorged` event follows. Finality confirmations only
increase (except on reorg).

### 7.5 Nonce Uniqueness

Each ERC-3009 authorization nonce is processed exactly once. The
`(chain_id, nonce, authorizer)` tuple is unique across the system.
Reorged nonces are released for reuse.

---

## 8. Skill Lifecycle

Skills progress through four states:

```
DRAFT ──> ACTIVE ──> DEPRECATED ──> ARCHIVED
```

| State | Discoverable | Activatable | Existing Instances | Editable |
|-------|:---:|:---:|:---:|:---:|
| `draft` | no | no | n/a | yes |
| `active` | yes | yes | running | no |
| `deprecated` | yes (marked) | no (new) | running | no |
| `archived` | no | no | running | no |

### 8.1 Versioning

Skills use semantic versioning (`major.minor.patch`):

- **Major**: Breaking changes (renamed params, removed fields)
- **Minor**: New optional parameters, filter improvements
- **Patch**: Bug fixes, security updates

Active instances continue running on their installed version.
New activations use the latest active version.

### 8.2 Discovery

Skills are discoverable via:

1. **MCP**: `list_templates` tool returns active skill definitions
2. **Bazaar**: `/.well-known/x402-manifest.json` advertises available skills
3. **API**: `GET /marketplace/discover` with category/search filters

---

## 9. Integration Points

### 9.1 MCP (Model Context Protocol)

Skills are exposed as MCP tools at `/mcp`. The 8 tools map to skill
lifecycle operations:

| Operation | MCP Tool | Auth |
|-----------|----------|------|
| Create endpoint + skills | `register_middleware` | X402 |
| Create custom skill | `create_trigger` | X402 |
| Install from Bazaar | `activate_template` | X402 |
| List my skills | `list_triggers` | SIWX |
| Remove skill | `delete_trigger` | SIWX |
| Browse Bazaar | `list_templates` | SIWX |
| Check health | `get_trigger_status` | SIWX |
| Query events | `search_events` | SIWX |

### 9.2 x402 (Payment Protocol)

- Skill invocation can require x402 micropayment (X402 auth tier)
- Skill events can gate on payment metadata (C3 payment gating)
- Settlement via ERC-3009 `transferWithAuthorization` (gasless USDC)
- Bazaar manifest at `/.well-known/x402-manifest.json`

### 9.3 ERC-8004 (Agent Identity)

- Agent identity resolved from onchain registry (CREATE2, all chains)
- Reputation score gates skill access (`min_reputation`)
- Agent class gates event delivery (`required_agent_class`)
- Every skill output includes `identity` block when available

### 9.4 CAIP-2 (Chain Identification)

- All chain references use CAIP-2 format: `eip155:{chain_id}`
- Supported: `eip155:1` (Ethereum), `eip155:8453` (Base), `eip155:42161` (Arbitrum)

---

## 10. Examples

### 10.1 Skill: x402 Payment Monitor

```json
{
  "name": "x402-payment",
  "slug": "x402-payment",
  "version": "1.0.0",
  "description": "Monitor x402 USDC payments to your endpoint",
  "category": "payments",

  "event": {
    "signature": "AuthorizationUsed(address,address,uint256,bytes32)",
    "abi": [{"type": "event", "name": "AuthorizationUsed", "inputs": [...]}],
    "chains": [8453, 1, 42161],
    "contract": null
  },

  "parameters": [
    {"name": "min_amount", "type": "uint256", "required": false, "description": "Min USDC (6 decimals)"}
  ],

  "filters": [
    {"field": "value", "op": "gte", "value": "${min_amount}"}
  ],

  "gating": {
    "payment": {"require_payment": false},
    "identity": {"min_reputation": 0}
  },

  "delivery": {
    "webhook_event_type": "payment.confirmed",
    "modes": ["execute", "notify"]
  }
}
```

### 10.2 Skill Output: Provisional (prepare)

```json
{
  "id": "evt_abc123",
  "idempotency_key": "sha256:8453:0x...:0:ep_xyz:pre_confirmed",
  "type": "payment.pre_confirmed",
  "version": "v1",
  "timestamp": 1710700800,

  "execution": {
    "state": "provisional",
    "safe_to_execute": false,
    "trust_source": "facilitator",
    "finality": null
  },

  "data": {
    "chain_id": 8453,
    "tx_hash": "0x0000000000000000000000000000000000000000",
    "block_number": 0,
    "from_address": "0xAgent...",
    "to_address": "0xYourAPI...",
    "amount": "5000000",
    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
  },

  "identity": {
    "address": "0xAgent...",
    "agent_class": "trading-bot",
    "reputation_score": 87.5,
    "capabilities": ["swap"]
  }
}
```

### 10.3 Skill Output: Finalized (commit)

```json
{
  "id": "evt_abc123",
  "idempotency_key": "sha256:8453:0x9f86...:7:ep_xyz:finalized",
  "type": "payment.finalized",
  "version": "v1",
  "timestamp": 1710700860,

  "execution": {
    "state": "finalized",
    "safe_to_execute": true,
    "trust_source": "onchain",
    "finality": {
      "confirmations": 3,
      "required": 3,
      "is_finalized": true
    }
  },

  "data": {
    "chain_id": 8453,
    "tx_hash": "0x9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "block_number": 28451023,
    "from_address": "0xAgent...",
    "to_address": "0xYourAPI...",
    "amount": "5000000",
    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
  },

  "identity": {
    "address": "0xAgent...",
    "agent_class": "trading-bot",
    "reputation_score": 87.5,
    "capabilities": ["swap"]
  }
}
```

### 10.4 Skill Output: Reorged (rollback)

```json
{
  "id": "evt_abc123",
  "idempotency_key": "sha256:8453:0x9f86...:7:ep_xyz:reorged",
  "type": "payment.reorged",
  "version": "v1",
  "timestamp": 1710700900,

  "execution": {
    "state": "reorged",
    "safe_to_execute": false,
    "trust_source": "onchain",
    "finality": {
      "confirmations": 0,
      "required": 3,
      "is_finalized": false
    }
  },

  "data": {
    "chain_id": 8453,
    "tx_hash": "0x9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
    "block_number": 28451023,
    "from_address": "0xAgent...",
    "to_address": "0xYourAPI...",
    "amount": "5000000",
    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
  }
}
```

### 10.5 Consumer Pattern

```python
@webhook("/tripwire")
async def handle(payload: dict):
    execution = payload["execution"]

    match execution["state"]:
        case "provisional":
            # prepare() — optimistic UI, hold resources
            await show_success_screen(payload["id"])
            await reserve_inventory(payload["data"]["amount"])

        case "confirmed":
            # waiting — update balance display, no irreversible ops
            await update_balance(payload["data"])

        case "finalized":
            if execution["safe_to_execute"]:
                # commit() — safe to execute irreversible logic
                await transfer_funds(payload["data"])
                await grant_access(payload["data"]["from_address"])

        case "reorged":
            # rollback() — undo everything from prepare()
            await rollback_reservation(payload["id"])
            await notify_user("Payment was reorganized. Please retry.")
```

---

## 11. JSON Schema Reference

Machine-readable schema for validation:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://tripwire.dev/schemas/twss-1/v1",
  "title": "TWSS-1 Skill Output",

  "type": "object",
  "required": ["id", "idempotency_key", "type", "version", "timestamp", "execution", "data"],

  "properties": {
    "id": {"type": "string"},
    "idempotency_key": {"type": "string"},
    "type": {"type": "string"},
    "version": {"type": "string", "const": "v1"},
    "timestamp": {"type": "integer"},

    "execution": {
      "type": "object",
      "required": ["state", "safe_to_execute", "trust_source"],
      "properties": {
        "state": {"enum": ["provisional", "confirmed", "finalized", "reorged"]},
        "safe_to_execute": {"type": "boolean"},
        "trust_source": {"enum": ["facilitator", "onchain"]},
        "finality": {
          "oneOf": [
            {"type": "null"},
            {
              "type": "object",
              "required": ["confirmations", "required", "is_finalized"],
              "properties": {
                "confirmations": {"type": "integer", "minimum": 0},
                "required": {"type": "integer", "minimum": 1, "maximum": 64},
                "is_finalized": {"type": "boolean"}
              }
            }
          ]
        }
      }
    },

    "data": {"type": "object"},
    "identity": {
      "type": "object",
      "properties": {
        "address": {"type": "string"},
        "agent_class": {"type": "string"},
        "reputation_score": {"type": "number", "minimum": 0, "maximum": 100},
        "capabilities": {"type": "array", "items": {"type": "string"}}
      }
    }
  }
}
```

---

## Appendix A: Landscape Comparison

| System | Execution State | Finality | Payment Gate | Identity Gate |
|--------|:---:|:---:|:---:|:---:|
| OpenAI Functions | - | - | - | - |
| Anthropic MCP | - | - | - | - |
| LangChain/CrewAI | - | - | - | - |
| Coinbase AgentKit | - | - | - | partial |
| ERC-7579/6900 | - | - | - | - |
| ERC-8004 | - | - | - | yes |
| x402 | - | - | yes | - |
| **TWSS-1** | **yes** | **yes** | **yes** | **yes** |

---

## Appendix B: Chain Finality Reference

| Chain | CAIP-2 | Default Depth | Block Time | Time to Finalize |
|-------|--------|---------------|------------|------------------|
| Ethereum | eip155:1 | 12 | ~12s | ~2.5 min |
| Base | eip155:8453 | 3 | ~2s | ~6s |
| Arbitrum | eip155:42161 | 1 | ~250ms | ~5s |

---

*TWSS-1 is an open specification. Feedback: github.com/tripwire/skill-spec*
