# TWSS-1: TripWire Skill Spec

**Version:** 1.0.0
**Status:** Draft
**Date:** 2026-03-17

> Everyone defines skills as "what to do." This spec defines "when it's safe to do it."

The key words MUST, MUST NOT, SHOULD, and MAY in this document are to be
interpreted as described in RFC 2119.

---

## 1. Scope

TWSS-1 defines execution-aware skills for onchain AI agent systems.

A **Skill** is a declarative trigger bound to an onchain event that produces
execution-aware output. Skills carry lifecycle state, trust attribution,
safety guarantees, payment gating, and identity verification.

This spec defines:
- What a Skill Definition MUST contain
- What a Skill Output MUST contain
- What MUST be true about execution state
- What MUST be true about determinism

This spec does NOT define:
- How the runtime processes events internally
- What infrastructure delivers webhooks
- How identity is resolved

---

## 2. Execution Semantics

This is the core of the spec. Everything else depends on this.

### 2.1 States

Every skill output carries exactly one of four execution states:

```
              [EVENT DETECTED]
                    |
                    v
             +--------------+
             | PROVISIONAL  |  prepare()
             | safe = false |
             +--------------+
                    |
          tx lands onchain
                    |
                    v
             +--------------+
             | CONFIRMED    |  wait()
             | safe = false |
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

### 2.2 State Rules

| State | `safe_to_execute` | `trust_source` | `finality` |
|-------|:-:|---|---|
| `provisional` | `false` | `facilitator` | `null` |
| `confirmed` | `false` | `onchain` | object |
| `finalized` | `true` | `onchain` | object, `is_finalized: true` |
| `reorged` | `false` | `onchain` | object, `is_finalized: false` |

### 2.3 The Safety Rule

> **`safe_to_execute` MUST be `true` ONLY when `state` is `finalized`.**

This is the foundational invariant of TWSS-1. No exceptions.

Formally:

```
safe_to_execute == true
  REQUIRES state == "finalized"
  REQUIRES trust_source == "onchain"
  REQUIRES finality.confirmations >= finality.required_confirmations
  REQUIRES finality.is_finalized == true
```

A conforming implementation MUST NOT set `safe_to_execute = true` under
any other conditions.

### 2.4 Trust Sources

| Source | Meaning |
|--------|---------|
| `facilitator` | Off-chain cryptographic signature verification |
| `onchain` | Block confirmations verified against chain state |

A conforming implementation MUST set `trust_source = "facilitator"` only
for `provisional` state and `trust_source = "onchain"` for all other states.

---

## 3. Two-Phase Execution

Skills operate in two phases. Consuming agents MUST implement both.

### 3.1 Phase 1: prepare()

Triggered when `state == "provisional"`.

The agent MAY:
- Show optimistic UI
- Reserve resources
- Start background processing

The agent MUST NOT:
- Transfer funds
- Mint tokens
- Grant permanent access
- Write irreversible state

### 3.2 Phase 2: commit()

Triggered when `state == "finalized"` AND `safe_to_execute == true`.

The agent MAY:
- Transfer funds
- Mint tokens
- Grant permanent access
- Finalize orders

### 3.3 rollback()

Triggered when `state == "reorged"`.

The agent MUST undo all effects of `prepare()`.

---

## 4. Three-Layer Gating

Every skill invocation passes through three gates. All three MUST pass.

```
  Event arrives
       |
  [1. CAN PAY?]     → Payment gate
       |
  [2. CAN TRUST?]   → Identity gate
       |
  [3. IS SAFE?]      → Execution gate
       |
  Skill fires
```

### 4.1 Payment Gate

Does the event carry sufficient payment?

```json
{
  "payment": {
    "required": true,
    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "min_amount": "1000000"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `required` | bool | Whether payment metadata MUST be present |
| `token` | address / null | Required token contract. `null` = any. |
| `min_amount` | string / null | Minimum in smallest unit |

If `required` is `true` and the decoded event does not contain payment
metadata meeting all specified constraints, the skill MUST NOT fire.

### 4.2 Identity Gate

Is the sender a known, reputable agent?

```json
{
  "identity": {
    "min_reputation": 10.0,
    "required_agent_class": "trading-bot"
  }
}
```

| Field | Type | Description |
|-------|------|-------------|
| `min_reputation` | float (0-100) | Minimum reputation score |
| `required_agent_class` | string / null | Required agent type |

Identity MUST be resolved from an onchain registry (ERC-8004). If the
sender has no registered identity and either gate is set, the skill
MUST NOT fire.

### 4.3 Execution Gate

Has the event reached sufficient finality?

| Condition | Result |
|-----------|--------|
| `finality.confirmations >= finality.required_confirmations` | Gate passes |
| `finality.confirmations < finality.required` | Skill deferred |
| `state == "reorged"` | Gate fails |

The required finality depth MAY be configured per-skill or per-endpoint.
Chain defaults: Ethereum 12, Base 3, Arbitrum 1.

---

## 5. Skill Definition

A Skill Definition declares what event to watch and how to gate it.

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
    "abi": [{"type": "event", "name": "Transfer", "inputs": ["..."]}],
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
      "required": false
    },
    "identity": {
      "min_reputation": 0,
      "required_agent_class": null
    }
  },

  "delivery": {
    "type": "trigger",
    "event_type": "transfer.whale",
    "modes": ["execute", "notify"]
  }
}
```

### 5.1 Required Fields

| Field | Type | Rule |
|-------|------|------|
| `name` | string | MUST be human-readable |
| `slug` | string | MUST be kebab-case, unique |
| `version` | string | MUST be semver |
| `event.signature` | string | MUST be a valid Solidity event signature |
| `event.abi` | array | MUST be a valid ABI fragment for decoding |
| `delivery.event_type` | string | MUST identify the webhook event type |

### 5.2 Optional Fields

| Field | Default | Rule |
|-------|---------|------|
| `event.chains` | `[8453]` | SHOULD list all supported chain IDs |
| `event.contract` | `null` | MAY restrict to a specific contract |
| `parameters` | `[]` | MAY define user-configurable inputs |
| `filters` | `[]` | MAY define filter predicates on decoded fields |
| `gating.payment` | `{required: false}` | MAY require payment metadata |
| `gating.identity` | `{min_reputation: 0}` | MAY require agent identity |

---

## 6. Skill Instance

A Skill Instance is a deployed, configured binding of a Skill Definition
to a delivery endpoint.

```json
{
  "instance_id": "inst_abc123",
  "skill": "whale-usdc-transfer",
  "version": "1.0.0",
  "endpoint_id": "ep_xyz789",
  "owner": "0xAgentAddress",

  "config": {
    "threshold": "5000000000"
  },

  "status": "active"
}
```

| Field | Type | Rule |
|-------|------|------|
| `instance_id` | string | MUST be unique |
| `skill` | string | MUST reference a valid skill slug |
| `version` | string | MUST be pinned to a specific semver |
| `endpoint_id` | string | MUST reference a valid delivery endpoint |
| `owner` | address | MUST be the agent that created the instance |
| `config` | object | MUST satisfy the skill's `parameters` schema |
| `status` | enum | `active` / `paused` / `deleted` |

An agent MUST own the endpoint to create an instance.
One agent MAY create multiple instances of the same skill.

---

## 7. Skill Output Contract

Every skill output MUST conform to this schema. This is the contract
between the platform and the consuming agent.

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
      "required_confirmations": 3,
      "is_finalized": true
    }
  },

  "data": {},

  "identity": {
    "address": "0x1234...5678",
    "agent_class": "trading-bot",
    "reputation_score": 87.5,
    "capabilities": ["swap", "limit-order"]
  }
}
```

### 7.1 Required Fields

| Field | Type | Rule |
|-------|------|------|
| `id` | string | MUST be unique per event |
| `idempotency_key` | string | MUST be deterministic (see 8.1) |
| `type` | string | MUST match `delivery.event_type` from skill definition |
| `version` | string | MUST be `"v1"` |
| `timestamp` | integer | MUST be Unix epoch seconds |
| `execution` | object | MUST conform to Section 2 |
| `data` | object | MUST contain decoded event fields |

### 7.2 The `execution` Block

MUST be present on every output. MUST follow the safety rule (Section 2.3).

| Field | Type | Rule |
|-------|------|------|
| `execution.state` | enum | MUST be one of: `provisional`, `confirmed`, `finalized`, `reorged` |
| `execution.safe_to_execute` | bool | MUST be `true` ONLY when `state == "finalized"` |
| `execution.trust_source` | enum | MUST be `"facilitator"` or `"onchain"` |
| `execution.finality` | object / null | MUST be `null` when `state == "provisional"` |

### 7.3 The `identity` Block

SHOULD be present when the sender is a registered agent.

| Field | Type | Rule |
|-------|------|------|
| `identity.address` | string | MUST be a valid address |
| `identity.agent_class` | string | MUST match onchain registration |
| `identity.reputation_score` | float | MUST be 0-100 |
| `identity.capabilities` | string[] | SHOULD list declared capabilities |

### 7.4 The `idempotency_key`

MUST be deterministic. Given the same onchain event, the same endpoint,
and the same event type, the key MUST be identical across retries.

Consuming agents MUST deduplicate on this key.

---

## 8. Determinism Guarantees

A conforming implementation MUST provide these guarantees:

### 8.1 Idempotency

Same onchain event + same endpoint + same type = same `idempotency_key`.
Retries and replays produce identical keys.

### 8.2 Ordering

Outputs for the same `id` MUST be delivered in lifecycle order:
`provisional` -> `confirmed` -> `finalized`.

A `reorged` output MAY arrive at any point after `provisional`.

### 8.3 At-Least-Once Delivery

Every output MUST be delivered at least once. Consuming agents MUST
handle duplicates via `idempotency_key`.

### 8.4 Finality Monotonicity

Once `safe_to_execute = true` is delivered for an event, it MUST NOT be
revoked unless a `reorged` output follows.

`finality.confirmations` MUST only increase for the same event (except
on reorg, where it resets to 0).

### 8.5 Nonce Uniqueness

Each authorization nonce MUST be processed exactly once. The tuple
`(chain_id, nonce, authorizer)` MUST be globally unique.

Reorged nonces MUST be released for reuse.

---

## 9. Skill Lifecycle

```
DRAFT ──> ACTIVE ──> DEPRECATED ──> ARCHIVED
```

| State | Discoverable | Activatable | Instances Run | Editable |
|-------|:---:|:---:|:---:|:---:|
| `draft` | no | no | n/a | yes |
| `active` | yes | yes | yes | no |
| `deprecated` | yes (marked) | no (new) | yes | no |
| `archived` | no | no | yes | no |

- A Skill Definition MUST NOT be modified after entering `active` state.
- A new version MUST be published as a new Skill Definition.
- Existing instances MUST continue running on their pinned version.
- Versions MUST use semver. Major = breaking, minor = additive, patch = fix.

---

## 10. Chain Finality

| Chain | CAIP-2 | Default Depth | Block Time |
|-------|--------|:---:|---|
| Ethereum | `eip155:1` | 12 | ~12s |
| Base | `eip155:8453` | 3 | ~2s |
| Arbitrum | `eip155:42161` | 1 | ~250ms |

Finality depth MAY be overridden per-endpoint (range: 1-64).

All chain references MUST use CAIP-2 format: `eip155:{chain_id}`.

---

## 11. Conformance

An implementation is TWSS-1 conformant if:

1. Every skill output includes the `execution` block per Section 7.2
2. `safe_to_execute` is `true` ONLY when `state == "finalized"` (Section 2.3)
3. `trust_source` follows the rules in Section 2.4
4. Idempotency keys are deterministic (Section 8.1)
5. Delivery order follows Section 8.2
6. Finality monotonicity holds per Section 8.4
7. Nonce uniqueness holds per Section 8.5
8. Three-layer gating is enforced per Section 4

---

## Appendix A: Landscape

| System | Execution State | Finality | Payment Gate | Identity Gate |
|--------|:---:|:---:|:---:|:---:|
| OpenAI Functions | - | - | - | - |
| Anthropic MCP | - | - | - | - |
| LangChain / CrewAI | - | - | - | - |
| Coinbase AgentKit | - | - | - | partial |
| ERC-7579 / 6900 | - | - | - | - |
| ERC-8004 | - | - | - | yes |
| x402 | - | - | yes | - |
| **TWSS-1** | **yes** | **yes** | **yes** | **yes** |

---

## Appendix B: Consumer Reference

```python
@webhook("/tripwire")
async def handle(payload: dict):
    execution = payload["execution"]

    match execution["state"]:
        case "provisional":
            await prepare(payload)       # optimistic, reversible only

        case "confirmed":
            await wait(payload)          # update display, no commits

        case "finalized":
            if execution["safe_to_execute"]:
                await commit(payload)    # irreversible operations

        case "reorged":
            await rollback(payload)      # undo prepare()
```

---

*TWSS-1 is an open specification.*
*Machine-readable schema: `GET /.well-known/tripwire-skill-spec.json`*
