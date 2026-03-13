# TripWire Architecture

## 1. System Overview

TripWire is a programmable onchain event trigger platform for AI agents -- the infrastructure layer between onchain events and application execution. While x402 payment webhooks are the founding use case ("Stripe Webhooks for x402"), the system now supports **any EVM event** through its dynamic trigger registry and MCP (Model Context Protocol) server.

TripWire is built for **AI agent developers** who need reliable, low-latency onchain event detection without running their own indexing infrastructure. An agent connects via MCP, registers triggers for the events it cares about (payments, swaps, mints, governance actions, or any custom event), and receives webhook callbacks whenever a matching event lands onchain.

Key design goals:

- **Any-event triggers** via a dynamic trigger registry with ABI-driven decoding
- **MCP-native** -- agents interact through 8 MCP tools, no REST API integration needed
- **Multi-path ingestion** with different latency/reliability tradeoffs
- **Sub-second detection** via the x402 facilitator fast path
- **Finality awareness** with per-chain confirmation depths and reorg detection
- **ERC-8004 agent identity** enrichment, reputation gating, and policy-based filtering
- **Wallet-native authentication** using SIWE (EIP-4361) with no API keys
- **Reliable delivery** through Convoy with exponential retry and dead-letter queues
- **Bazaar marketplace** for discovering and activating pre-built trigger templates

The stack is Python (FastAPI + uvicorn), Supabase (PostgreSQL + Realtime), Redis (nonce replay prevention), Convoy (webhook delivery), MCP JSON-RPC server, and direct JSON-RPC calls for finality and identity resolution.

---

## 2. Ingestion Paths

TripWire supports three ingestion paths, each optimized for a different point on the latency-reliability spectrum.

### 2a. Goldsky Turbo (Primary Path)

```
Chain -> Goldsky Indexer -> Webhook -> POST /api/v1/ingest -> TripWire Pipeline
```

- **Latency:** 2-4 seconds from block confirmation
- **Reliability:** High. Goldsky handles reorgs, backfills, and delivery guarantees.
- **Auth:** HMAC verification via `GOLDSKY_WEBHOOK_SECRET`
- **Batch support:** Goldsky sends batches of decoded logs; TripWire processes them concurrently with a semaphore (max 10 concurrent) via `EventProcessor.process_batch()`.

Goldsky's SQL transform JOINs the `Transfer` and `AuthorizationUsed` events from the same transaction, producing a pre-decoded row with both transfer fields and authorization fields.

### 2b. x402 Facilitator (Fast Path)

```
Agent -> x402 Facilitator -> POST /api/v1/ingest/facilitator -> TripWire Pipeline
```

- **Latency:** ~100ms (pre-confirmation)
- **Reliability:** Medium. The facilitator has verified the ERC-3009 signature but the transaction is NOT yet onchain.
- **Auth:** Bearer token via `FACILITATOR_WEBHOOK_SECRET`
- **Event type:** `payment.pre_confirmed`

This path skips decode and finality (no tx hash or block number yet). A synthetic pseudo-tx-hash is generated for correlation. The pipeline still runs nonce dedup, identity resolution, policy evaluation, and dispatch. The finality poller later promotes the event to `confirmed` once the real transaction lands.

---

## 3. Processing Pipeline

Every ingestion path feeds into the `EventProcessor`, which orchestrates the full pipeline. The processor handles two distinct paths: the **ERC-3009 path** (hardcoded, optimized for payments) and the **dynamic trigger path** (ABI-driven, supports any EVM event).

### Step 1: Event Type Detection (`_detect_event_type`)

The processor inspects `topic[0]` of the raw log through a two-tier lookup:

1. **Hardcoded signatures:** Check against `_EVENT_SIGNATURES` dict. Currently `Transfer` and `AuthorizationUsed` topic signatures map to `erc3009_transfer`.
2. **Dynamic trigger registry fallback:** If no hardcoded match, query the `triggers` table via `TriggerRepository.find_by_topic(topic0)` (cached with 30-second TTL). Results are further filtered locally by `chain_id` and `contract_address`. If triggers match, returns `("dynamic", matched_triggers)`.

This two-tier approach means hardcoded ERC-3009 processing remains fast while any new event type can be supported by creating a trigger -- no code changes required.

### Path A: ERC-3009 Pipeline (`_process_erc3009_event`)

For `erc3009_transfer` events, the original optimized pipeline runs:

#### Step 2: Decode

`decoder.py` extracts structured data from the raw log:

- **Goldsky path:** Parses the pre-decoded row with `decoded` (authorizer, nonce) and `transfer` (from, to, value) fields.
- **Raw path:** Uses `decode_erc3009_from_logs()` to decode both `Transfer(address,address,uint256)` and `AuthorizationUsed(address,bytes32)` from raw EVM log topics and data.
- **Facilitator path:** Skipped entirely; data arrives pre-structured.

Chain ID is resolved from the raw log's `chain_id` field or by reverse-mapping the USDC contract address.

#### Step 3: Nonce Deduplication

The `nonce` (bytes32) from the `AuthorizationUsed` event is checked against the `nonces` table, keyed by `(chain_id, nonce, authorizer)`. If already seen, the event is short-circuited as a duplicate. For plain Transfer events (no authorizer), deduplication uses `tx_hash:log_index`. This runs first because it is the cheapest rejection path.

#### Step 4: Finality Check + Identity Resolution (Parallel)

These two stages have zero data dependencies on each other and run concurrently via `asyncio.gather()`:

- **Finality:** `check_finality()` fetches the current block number via JSON-RPC and computes `confirmations = current_block - event.block_number`. Each chain has a configured finality depth (Ethereum: 12, Base: 3, Arbitrum: 1). Returns a `FinalityStatus` with `is_finalized` flag.
- **Identity:** The `IdentityResolver` looks up the sender's ERC-8004 agent identity (see section 9).

For the facilitator fast path, finality is skipped (returns `None`).

#### Steps 5-8: Endpoint Matching, Policy, Recording, Dispatch

These generic stages are shared with all transfer-like events via `_dispatch_for_transfer()`:

- **Step 5 -- Endpoint Matching:** Active endpoints fetched by `recipient` address (30-second in-memory TTL cache), filtered by recipient match (case-insensitive), chain ID membership, and active status.
- **Step 6 -- Policy Evaluation:** Each endpoint's `policies` evaluated against transfer data and identity: `min_amount`/`max_amount`, `allowed_senders`/`blocked_senders`, `required_agent_class`, `min_reputation_score`, `finality_depth`.
- **Step 7 -- Event Recording:** Event row inserted into `events` table with all structured columns plus JSONB `data` and optional `identity_data`.
- **Step 8 -- Dispatch:** Execute-mode endpoints get webhooks via Convoy; Notify-mode endpoints get events pushed via Supabase Realtime (subscription filters applied first).

### Path B: Dynamic Trigger Pipeline (`_process_dynamic_event`)

For events matched by the trigger registry, a per-trigger pipeline runs. This path handles **any EVM event** -- DEX swaps, NFT mints, governance actions, etc.

For each matched trigger:

1. **ABI Decode:** `decode_event_with_abi()` from the generic decoder uses the trigger's stored ABI fragment to decode indexed parameters from topics and non-indexed parameters from data. Returns a flat `field_name -> value` dict plus `_`-prefixed metadata (`_tx_hash`, `_block_number`, `_block_hash`, `_log_index`, `_address`, `_chain_id`).

2. **Filter Evaluation:** The trigger's `filter_rules` are evaluated against the decoded event via the filter engine. All rules use AND logic. If any rule fails, the event is rejected for that trigger (but may still match other triggers for the same topic).

3. **Deduplication:** Uses `tx_hash:log_index:trigger_id` as the dedup key, with authorizer set to `"dynamic_trigger"`.

4. **Identity Resolution:** Scans decoded fields for the first Ethereum address value and resolves its ERC-8004 identity.

5. **Endpoint Fetch:** Retrieves the endpoint linked to `trigger.endpoint_id` and checks it is active.

6. **Dispatch:** Builds a payload with the full decoded event data, trigger ID, and idempotency key (`dyn_{tx_hash}_{log_index}_{trigger_id}`). Dispatches via Convoy if the endpoint has a `convoy_project_id`.

7. **Event Recording:** Inserts into the `events` table with identity data if resolved.

---

## 4. Webhook Delivery

### Convoy Integration

All Execute-mode webhook deliveries route through [Convoy](https://getconvoy.io/), a self-hosted webhook gateway. TripWire uses the Convoy REST API via `convoy_client.py`:

- **Project creation:** Each registered endpoint gets a Convoy project (`create_application`) with exponential retry configured (10 retries, 10s base duration).
- **Endpoint registration:** The webhook URL is registered with Convoy along with an HMAC signing secret.
- **Event dispatch:** `send_webhook()` posts the event payload to the Convoy project, targeting the specific endpoint. Convoy handles delivery, retries, and logging.

### Retry Strategy

Convoy is configured with exponential backoff: 10 retries starting at 10 seconds. Failed deliveries land in Convoy's dead-letter queue.

### Dead Letter Queue

The `DLQHandler` runs as a background poller (configurable interval, default 60s) that queries Convoy for failed deliveries and can trigger batch retries via `force_resend()`.

### HMAC Signatures

Each endpoint gets a unique webhook secret (64-character hex token generated via `secrets.token_hex(32)`). This secret is passed to Convoy at endpoint creation time and used for HMAC signature headers on every delivery. The secret is returned to the caller exactly once at registration time.

### Idempotency

Every webhook delivery includes a deterministic idempotency key derived from `SHA256(chain_id:tx_hash:log_index:endpoint_id:event_type)`. This prevents duplicate deliveries even if the same event is processed multiple times.

### Webhook Payload Structure

```json
{
  "id": "uuid",
  "idempotency_key": "idem_<sha256_prefix>",
  "type": "payment.confirmed",
  "mode": "execute",
  "timestamp": 1710000000,
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0x...",
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
      "metadata": {}
    }
  }
}
```

### Event Types

| Type | Meaning |
|------|---------|
| `payment.pre_confirmed` | Facilitator fast path; ERC-3009 signature verified, tx not yet onchain |
| `payment.pending` | Onchain but below finality depth |
| `payment.confirmed` | Reached required finality depth |
| `payment.reorged` | Block hash mismatch detected by finality poller |
| `payment.failed` | Processing failure |

---

## 5. Authentication

TripWire uses **SIWE (Sign-In with Ethereum, EIP-4361)** for all API authentication. There are no API keys.

### Flow

1. **Get nonce:** `GET /auth/nonce` returns a cryptographically random nonce stored in Redis with a 5-minute TTL.
2. **Sign message:** The client constructs an EIP-4361 message with the method, path, and SHA-256 body hash as the statement, then signs it with `personal_sign`.
3. **Send request:** Every authenticated request includes five headers:
   - `X-TripWire-Address` -- the Ethereum address
   - `X-TripWire-Signature` -- the EIP-191 signature
   - `X-TripWire-Nonce` -- the nonce from step 1
   - `X-TripWire-Issued-At` -- ISO-8601 timestamp
   - `X-TripWire-Expiration` -- ISO-8601 expiration

### Verification (`require_wallet_auth`)

1. **Expiration check:** Reject if the current time is past the expiration.
2. **Body hash binding:** Read the full request body and compute `SHA-256(body)`. The hash is embedded in the SIWE statement as `METHOD /path <body_hash>`, binding the signature to the exact request content.
3. **Signature recovery:** Reconstruct the SIWE message and recover the signer address via `eth_account.Account.recover_message`.
4. **Address comparison:** Case-insensitive comparison of the recovered address with the claimed address.
5. **Nonce consumption:** Atomically delete the nonce key from Redis. If the key does not exist (already used or expired), the request is rejected. This prevents replay attacks.

### Ingestion Auth

The Goldsky and facilitator ingestion paths use separate shared-secret authentication (HMAC bearer tokens) rather than SIWE, since they are server-to-server.

---

## 6. Payment

### x402 Protocol

Endpoint registration (`POST /api/v1/endpoints`) is gated by the [x402 payment protocol](https://x402.org). When `TRIPWIRE_TREASURY_ADDRESS` is configured, the `PaymentMiddlewareASGI` middleware intercepts the request and requires an x402 payment proof.

### Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `x402_facilitator_url` | `https://x402.org/facilitator` | Coinbase x402 facilitator URL |
| `x402_registration_price` | `$1.00` | USDC payment required to register an endpoint |
| `x402_network` | `eip155:8453` | Base mainnet |
| `tripwire_treasury_address` | (empty) | USDC recipient; empty disables payment gating |

### Flow

1. Client sends `POST /api/v1/endpoints` without payment proof.
2. Middleware returns 402 Payment Required with payment options.
3. Client pays via the x402 facilitator and retries with the payment proof header.
4. Middleware verifies the payment using `ExactEvmServerScheme`.
5. The endpoint is created and the payment tx hash is recorded on the endpoint row.

---

## 7. Data Model

### Endpoints

The core registration entity. Each endpoint represents a webhook URL that receives payment notifications for a specific recipient address.

| Field | Type | Description |
|-------|------|-------------|
| `id` | TEXT PK | nanoid (21 chars) |
| `url` | TEXT | Webhook delivery URL |
| `mode` | TEXT | `execute` (webhook) or `notify` (Supabase Realtime) |
| `chains` | JSONB | Array of chain IDs to monitor |
| `recipient` | TEXT | Ethereum address receiving payments |
| `owner_address` | TEXT | Wallet address that owns this endpoint (SIWE auth) |
| `policies` | JSONB | Policy configuration (min/max amount, sender filters, etc.) |
| `active` | BOOLEAN | Soft-delete flag |
| `convoy_project_id` | TEXT | Convoy project ID for webhook delivery |
| `convoy_endpoint_id` | TEXT | Convoy endpoint ID |
| `webhook_secret` | TEXT | Per-endpoint HMAC signing secret |
| `registration_tx_hash` | TEXT | x402 payment transaction hash |
| `registration_chain_id` | INTEGER | Chain where x402 payment was made |

### Subscriptions

Notify-mode filter configuration. Each subscription defines which events should be pushed to the parent endpoint via Supabase Realtime.

| Field | Type | Description |
|-------|------|-------------|
| `id` | TEXT PK | nanoid |
| `endpoint_id` | TEXT FK | Parent endpoint |
| `filters` | JSONB | Filter criteria (chains, senders, recipients, min_amount, agent_class) |
| `active` | BOOLEAN | Active flag |

### Events

Every processed payment event, regardless of whether it matched any endpoints.

| Field | Type | Description |
|-------|------|-------------|
| `id` | TEXT PK | UUID |
| `chain_id` | INTEGER | Chain ID |
| `tx_hash` | TEXT | Transaction hash |
| `block_number` | BIGINT | Block number |
| `block_hash` | TEXT | Block hash (used for reorg detection) |
| `log_index` | INTEGER | Log index within the transaction |
| `from_address` | TEXT | Transfer sender |
| `to_address` | TEXT | Transfer recipient |
| `amount` | TEXT | Transfer value (string for USDC 6-decimal precision) |
| `authorizer` | TEXT | ERC-3009 authorization signer |
| `nonce` | TEXT | ERC-3009 bytes32 nonce |
| `token` | TEXT | USDC contract address |
| `status` | TEXT | `pending`, `confirmed`, or `reorged` |
| `finality_depth` | INTEGER | Current confirmation count |
| `identity_data` | JSONB | ERC-8004 agent identity snapshot |
| `endpoint_id` | TEXT FK | First matched endpoint |
| `confirmed_at` | TIMESTAMPTZ | When the event reached finality |

### Webhook Deliveries

Tracks every webhook dispatch attempt.

| Field | Type | Description |
|-------|------|-------------|
| `id` | TEXT PK | UUID |
| `endpoint_id` | TEXT FK | Target endpoint |
| `event_id` | TEXT FK | Source event |
| `provider_message_id` | TEXT | Convoy event/delivery ID |
| `status` | TEXT | `pending`, `sent`, `delivered`, `failed` |

### Nonces

Deduplication table for ERC-3009 authorization nonces.

| Field | Type | Description |
|-------|------|-------------|
| `chain_id` | INTEGER | Chain ID |
| `nonce` | TEXT | bytes32 hex nonce |
| `authorizer` | TEXT | Authorizer address |
| UNIQUE | | `(chain_id, nonce, authorizer)` |

### Triggers

Dynamic trigger definitions created by agents via MCP. Each trigger watches for a specific EVM event signature and routes matching events to its parent endpoint.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID PK | Auto-generated |
| `owner_address` | TEXT | Agent wallet that owns this trigger |
| `endpoint_id` | TEXT FK | Parent endpoint for webhook delivery |
| `name` | TEXT | Human-readable trigger name |
| `event_signature` | TEXT | Solidity event signature (e.g. `Transfer(address,address,uint256)`) |
| `abi` | JSONB | ABI fragment array for decoding the event |
| `contract_address` | TEXT | Specific contract to watch (null = any contract) |
| `chain_ids` | JSONB | Array of chain IDs to monitor |
| `filter_rules` | JSONB | Array of `{field, op, value}` filter predicates |
| `webhook_event_type` | TEXT | Event type string sent in webhook payload |
| `reputation_threshold` | FLOAT | Minimum ERC-8004 reputation score (0-100) |
| `batch_id` | UUID | Groups triggers created in one `register_middleware` call |
| `active` | BOOLEAN | Soft-delete flag |

### Trigger Templates

Pre-built trigger templates for the Bazaar marketplace. Templates define reusable event configurations that agents can instantiate with custom parameters.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID PK | Auto-generated |
| `name` | TEXT | Display name |
| `slug` | TEXT UNIQUE | URL-safe identifier (e.g. `whale-transfer`, `dex-swap`) |
| `description` | TEXT | Human-readable description |
| `category` | TEXT | Category for browsing (`defi`, `payments`, `nft`, `governance`) |
| `event_signature` | TEXT | Solidity event signature |
| `abi` | JSONB | ABI fragment for decoding |
| `default_chains` | JSONB | Default chain IDs |
| `default_filters` | JSONB | Default filter rules |
| `parameter_schema` | JSONB | Schema for customizable parameters |
| `webhook_event_type` | TEXT | Default webhook event type |
| `reputation_threshold` | FLOAT | Minimum reputation to use this template |
| `author_address` | TEXT | Template author's wallet |
| `is_public` | BOOLEAN | Visible in Bazaar |
| `install_count` | BIGINT | Number of activations (auto-incremented via DB trigger) |

### Trigger Instances

Tracks template activations. Each instance links a template to a specific endpoint with custom parameters. A DB trigger auto-increments the parent template's `install_count`.

| Field | Type | Description |
|-------|------|-------------|
| `id` | UUID PK | Auto-generated |
| `template_id` | UUID FK | Parent template |
| `owner_address` | TEXT | Agent wallet |
| `endpoint_id` | TEXT FK | Target endpoint |
| `contract_address` | TEXT | Overridden contract address |
| `chain_ids` | JSONB | Overridden chain IDs |
| `parameters` | JSONB | Custom parameter values |
| `resolved_filters` | JSONB | Final filter rules after parameter resolution |
| `active` | BOOLEAN | Active flag |

---

## 8. Security

### Row Level Security (RLS)

All four main tables (`endpoints`, `subscriptions`, `events`, `webhook_deliveries`) have RLS enabled and forced. Policies use the session variable `app.current_wallet` (set via `SET LOCAL` before each request) to restrict access:

- **endpoints:** Direct `owner_address` match.
- **subscriptions, events, webhook_deliveries:** JOIN through `endpoints.owner_address` for ownership verification.

A `set_wallet_context(wallet_address)` PostgreSQL function (SECURITY DEFINER) sets the session variable.

### Ownership Enforcement

Every API route that accesses a specific resource verifies ownership at the application layer:

- Endpoint routes: `owner_address` must match the authenticated wallet.
- Event routes: Ownership verified through the parent endpoint.
- Delivery routes: Ownership verified through the parent endpoint.
- Subscription routes: Ownership verified through the parent endpoint.

### Secret Management

- All secrets use Pydantic `SecretStr` to prevent accidental logging.
- Production startup validates that critical secrets are set (`supabase_service_role_key`, `convoy_api_key`, `tripwire_treasury_address`).
- Per-endpoint webhook secrets are generated with `secrets.token_hex(32)` and returned exactly once at registration.

### Fail-Secure Defaults

- Missing `GOLDSKY_WEBHOOK_SECRET` in production causes ingest endpoints to reject all requests.
- Missing `FACILITATOR_WEBHOOK_SECRET` in production returns 500 (refuses to operate without auth).
- The webhook provider falls back to `LogOnlyProvider` (no delivery) when `CONVOY_API_KEY` is missing.
- Identity resolution failures return `None` (event still processes, but identity-dependent policies will reject).
- Finality check failures default to `pending` status (never falsely confirms).

### Input Validation

- Endpoint URLs are validated via `validate_endpoint_url()`.
- Facilitator payloads validate that `token` is a known USDC contract and `chain_id` is supported.
- `signature_verified` must be `true` on facilitator payloads.

### Rate Limiting

SlowAPI middleware with configurable limits per route category (CRUD operations and ingestion endpoints).

---

## 9. Identity (ERC-8004)

TripWire resolves onchain AI agent identities via the [ERC-8004](https://eips.ethereum.org/EIPS/eip-8004) standard. The `IdentityRegistry` contract is an ERC-721 where each token represents a registered agent.

### Resolution Flow

1. `balanceOf(senderAddress)` -- check if the sender owns an ERC-8004 NFT.
2. `tokenOfOwnerByIndex(senderAddress, 0)` -- get the agent's token ID.
3. **Parallel RPC calls** (all depend only on `agentId`):
   - `tokenURI(agentId)` -- agent metadata URI
   - `getMetadata(agentId, "agentClass")` -- agent classification
   - `getMetadata(agentId, "capabilities")` -- comma-separated capability list
   - `ownerOf(agentId)` -- deployer address
   - `getSummary(agentId, [])` -- reputation score from the `ReputationRegistry`

### Contract Addresses

Both registries use CREATE2 and share the same address on all supported chains:

- **IdentityRegistry:** `0x8004A169FB4a3325136EB29fA0ceB6D2e539a432`
- **ReputationRegistry:** `0x8004BAa17C55a88189AE136b182e5fdA19dE9b63`

### AgentIdentity Model

```python
class AgentIdentity(BaseModel):
    address: EthAddress
    agent_class: str             # e.g., "trading-bot", "payment-agent"
    deployer: EthAddress
    capabilities: list[str]      # e.g., ["swap", "limit-order"]
    reputation_score: float      # 0-100 (derived from basis points)
    registered_at: int           # token ID as registration order proxy
    metadata: dict[str, Any]     # agent_id, agent_uri
```

### Caching

Resolved identities are cached in-memory with a configurable TTL (default 300 seconds). Cache key is `chain_id:address`.

### Policy Integration

Identity data feeds into the policy engine:

- `required_agent_class` -- only allow events from agents of a specific class.
- `min_reputation_score` -- reject agents below a reputation threshold.

In development, a `MockResolver` provides three pre-configured test agents.

---

## 10. Database Schema

### Entity Relationship Diagram

```
endpoints (PK: id)
    |-- owner_address (wallet auth)
    |-- recipient (payment matching)
    |-- convoy_project_id, convoy_endpoint_id (webhook wiring)
    |
    |--< subscriptions (FK: endpoint_id)
    |       |-- filters (JSONB: chains, senders, recipients, min_amount, agent_class)
    |
    |--< triggers (FK: endpoint_id)
    |       |-- owner_address, event_signature, abi
    |       |-- contract_address, chain_ids, filter_rules
    |       |-- webhook_event_type, reputation_threshold
    |       |-- active (soft delete)
    |
    |--< trigger_instances (FK: endpoint_id, template_id)
    |       |-- parameters, resolved_filters
    |       |-- contract_address, chain_ids
    |
    |--< events (FK: endpoint_id)
    |       |-- chain_id, tx_hash, block_number, block_hash
    |       |-- from_address, to_address, amount, authorizer, nonce
    |       |-- status (pending -> confirmed | reorged)
    |       |-- finality_depth, identity_data
    |       |
    |       |--< webhook_deliveries (FK: event_id, endpoint_id)
    |               |-- provider_message_id (Convoy)
    |               |-- status (pending -> sent -> delivered | failed)
    |
trigger_templates (PK: id)
    |-- slug (UNIQUE), name, category, event_signature, abi
    |-- default_chains, default_filters, parameter_schema
    |-- is_public, install_count (auto-incremented)
    |
    |--< trigger_instances (FK: template_id)
    |
nonces (UNIQUE: chain_id, nonce, authorizer)
    |-- deduplication table, no FK relationships
    |
audit_log
    |-- entity_type, entity_id, action, metadata
```

### Indexes

Key indexes optimized for the access patterns:

- `idx_endpoints_recipient` -- endpoint matching during event processing
- `idx_endpoints_owner_address` -- wallet-scoped queries
- `idx_events_chain_tx` -- tx lookup
- `idx_events_nonce` -- composite `(chain_id, nonce, authorizer)` for dedup
- `idx_events_status` -- finality poller queries for pending events
- `idx_events_block` -- composite `(chain_id, block_number)` for finality polling
- `idx_webhook_deliveries_status` -- DLQ handler queries
- `idx_triggers_event_sig` -- topic0 lookup during dynamic event detection
- `idx_triggers_contract` -- partial index on `contract_address` where not null
- `idx_triggers_active` -- partial index on active triggers only
- `idx_triggers_chain_ids` -- GIN index for chain_id containment queries
- `idx_triggers_owner` -- owner-scoped trigger queries
- `idx_trigger_templates_slug` -- slug lookup for template activation
- `idx_trigger_templates_category` -- category browsing in Bazaar
- `idx_trigger_templates_public` -- partial index for public templates

### Finality Poller

The `FinalityPoller` runs as a background asyncio task with one coroutine per chain:

| Chain | Finality Depth | Poll Interval |
|-------|---------------|---------------|
| Arbitrum | 1 confirmation | 5 seconds |
| Base | 3 confirmations | 10 seconds |
| Ethereum | 12 confirmations | 30 seconds |

Each poll cycle:

1. Fetches pending events for the chain.
2. Gets the current block number (single RPC call per cycle).
3. For each event: checks block hash for reorg, then checks confirmation count.
4. Transitions events to `confirmed` (fires `payment.confirmed` webhook) or `reorged` (fires `payment.reorged` webhook).

### Supported Chains

| Chain | Chain ID | USDC Contract |
|-------|----------|---------------|
| Ethereum | 1 | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` |
| Base | 8453 | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Arbitrum | 42161 | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` |

---

## 11. Trigger Registry

The trigger registry is the core data layer that makes TripWire event-agnostic. Instead of hardcoding event types, agents define triggers dynamically -- each trigger specifies an EVM event signature, an ABI fragment for decoding, optional contract/chain filters, and filter predicates on decoded fields.

### Three Tables

| Table | Purpose | Created By |
|-------|---------|------------|
| `trigger_templates` | Pre-built recipes in the Bazaar marketplace | Platform operators / community |
| `triggers` | Active trigger definitions (custom or from templates) | Agents via MCP |
| `trigger_instances` | Tracks which templates were activated and with what params | Agents via `activate_template` |

### How Dynamic Triggers Work

1. An agent creates a trigger (via `register_middleware` with `template_slugs`/`custom_triggers`, or via `create_trigger`). The trigger row stores: `event_signature` (keccak256 topic0), `abi` (JSON ABI fragment), `contract_address` (optional), `chain_ids`, and `filter_rules`.

2. When a raw log arrives at the `EventProcessor`, `_detect_event_type` checks `topic0` against hardcoded signatures first, then falls back to `TriggerRepository.find_by_topic(topic0)`. The topic lookup is cached with a 30-second TTL.

3. Matched triggers are filtered locally by `chain_id` and `contract_address`. Each surviving trigger's ABI is used to decode the event, its filter rules are evaluated, and the event is dispatched to the trigger's linked endpoint.

### Caching

- **Active triggers:** Module-level cache, invalidated on any create/deactivate operation.
- **Topic lookup:** Per-topic cache with 30-second TTL (`_topic_cache`).
- **Public templates:** Module-level cache, invalidated on trigger cache invalidation.

All caches are invalidated atomically via `invalidate_trigger_cache()`.

### The Bazaar

The Bazaar is the marketplace for trigger templates. Seed templates ship with the platform:

| Template | Slug | Category | Event |
|----------|------|----------|-------|
| Whale Transfer Monitor | `whale-transfer` | defi | `Transfer(address,address,uint256)` |
| DEX Swap Monitor | `dex-swap` | defi | `Swap(address,address,int256,int256,uint160,uint128,int24)` |
| NFT Mint Monitor | `nft-mint` | nft | `Transfer(address,address,uint256)` with `from == 0x0` filter |
| ERC-3009 Payment | `erc3009-payment` | payments | `AuthorizationUsed(address,bytes32)` |
| Ownership Transfer | `ownership-transfer` | governance | `OwnershipTransferred(address,address)` |

Templates with `reputation_threshold > 0` require the activating agent to have a minimum ERC-8004 reputation score (e.g., `ownership-transfer` requires 25).

---

## 12. Generic Event Decoder

The generic decoder (`tripwire/ingestion/generic_decoder.py`) enables ABI-driven decoding for **any** EVM event, replacing the need for hardcoded decoders when processing dynamic triggers.

### `decode_event_with_abi(raw_log, abi_fragment)`

Given a raw Goldsky log and a JSON ABI fragment array:

1. Finds the first entry with `type == "event"` in the ABI fragment.
2. Separates inputs into indexed (decoded from `topics[1:]`) and non-indexed (decoded from `data`).
3. **Indexed parameters:** Parsed by type -- `address` extracts the last 40 hex chars, `uint`/`int` converts from hex, `bytes32` kept as hex, `bool` checks non-zero.
4. **Non-indexed parameters:** Decoded via `eth_abi.decode()` using the type list. Bytes values are hex-encoded.
5. Attaches metadata as `_`-prefixed fields: `_tx_hash`, `_block_number`, `_block_hash`, `_log_index`, `_address`, `_chain_id`.

Returns a flat `dict[str, Any]` where keys are the ABI input names.

### Design

- Uses `eth-abi` for data decoding (lightweight, no web3.py dependency).
- Reuses `_parse_topics` and `_to_int` from the existing ERC-3009 decoder.
- Errors in data decoding are logged but do not crash the pipeline -- partially decoded events still propagate.

---

## 13. Filter Engine

The filter engine (`tripwire/ingestion/filter_engine.py`) evaluates trigger-specific predicates against decoded event fields. All filter rules use **AND logic** -- every rule must pass for the event to match.

### Filter Rule Schema

Each rule is a `TriggerFilter` with three fields:

```python
class TriggerFilter(BaseModel):
    field: str      # decoded event field name (e.g. "value", "from", "amount0")
    op: str = "eq"  # operator
    value: Any      # target value
```

### Supported Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `eq` | Equals (case-insensitive for addresses, numeric-aware) | `{"field": "to", "op": "eq", "value": "0xabc..."}` |
| `neq` | Not equals | `{"field": "from", "op": "neq", "value": "0x000..."}` |
| `gt` | Greater than (numeric) | `{"field": "value", "op": "gt", "value": "1000000"}` |
| `gte` | Greater than or equal | `{"field": "value", "op": "gte", "value": "1000000000"}` |
| `lt` | Less than (numeric) | `{"field": "tick", "op": "lt", "value": "100"}` |
| `lte` | Less than or equal | `{"field": "amount0", "op": "lte", "value": "0"}` |
| `in` | Value in list | `{"field": "to", "op": "in", "value": ["0xabc...", "0xdef..."]}` |
| `not_in` | Value not in list | `{"field": "from", "op": "not_in", "value": ["0x000..."]}` |
| `between` | Value in range [lo, hi] inclusive | `{"field": "value", "op": "between", "value": ["1000000", "10000000"]}` |
| `contains` | Substring match (case-insensitive) | `{"field": "name", "op": "contains", "value": "usdc"}` |
| `regex` | Regular expression match | `{"field": "symbol", "op": "regex", "value": "^USD.*"}` |

### Normalization

- Ethereum addresses are lowercased before comparison.
- String-encoded numbers and hex values (`0x...`) are converted to `Decimal` for numeric operations.
- Missing fields cause immediate rejection (fail-closed).

---

## 14. MCP Server

TripWire exposes a **Model Context Protocol** (MCP) server mounted at `/mcp` as a FastAPI sub-application. This is the primary interface for AI agents to interact with TripWire programmatically.

### Protocol

- **Transport:** HTTP POST (JSON-RPC 2.0)
- **Protocol version:** `2024-11-05`
- **Methods:** `initialize`, `tools/list`, `tools/call`

### Authentication

Bearer token containing the agent's Ethereum address (`Authorization: Bearer 0x...`). The MCP server extracts and validates the address format. Full SIWE verification is planned for a future release.

### 8 Tools

| Tool | Description | Required Params |
|------|-------------|-----------------|
| `register_middleware` | Create endpoint + triggers in one call | `url` |
| `create_trigger` | Add a trigger to an existing endpoint | `endpoint_id`, `event_signature` |
| `list_triggers` | List agent's triggers (with active_only filter) | -- |
| `delete_trigger` | Soft-delete a trigger | `trigger_id` |
| `list_templates` | Browse Bazaar templates (with category filter) | -- |
| `activate_template` | Instantiate a template for an endpoint | `slug`, `endpoint_id` |
| `get_trigger_status` | Check trigger health and event count | `trigger_id` |
| `search_events` | Query recent events across agent's endpoints | -- |

### Reputation Gating

Each tool definition has a `min_reputation` threshold (default 0.0). When a tool requires reputation > 0, the server resolves the agent's ERC-8004 identity and checks `reputation_score >= min_reputation` before execution. Agents below the threshold receive a `-32001 REPUTATION_TOO_LOW` JSON-RPC error.

### Audit Logging

Every `tools/call` invocation is recorded via `AuditLogger` with: action (`mcp.tools.<tool_name>`), actor (agent address), arguments, success/failure, and client IP. Audit writes are fire-and-forget to avoid blocking the response.

### Ownership Enforcement

All tool handlers verify that the authenticated agent owns the resources being accessed:
- `create_trigger`, `activate_template`: endpoint `owner_address` must match agent.
- `delete_trigger`, `get_trigger_status`: trigger `owner_address` must match agent.
- `search_events`: only returns events for the agent's own endpoints.

---

## 15. The `register_middleware` Flow

This is the key onboarding flow for AI agents. A single MCP tool call sets up the full middleware pipeline.

```
Agent                          MCP Server                      Supabase
  |                                |                              |
  |-- tools/call: register_middleware -->                          |
  |   {url, mode, chains,         |                              |
  |    template_slugs,             |                              |
  |    custom_triggers}            |                              |
  |                                |-- INSERT endpoint ---------->|
  |                                |                              |
  |                                |-- For each template_slug:    |
  |                                |   GET template by slug ----->|
  |                                |   INSERT trigger ----------->|
  |                                |                              |
  |                                |-- For each custom_trigger:   |
  |                                |   INSERT trigger ----------->|
  |                                |                              |
  |<-- {endpoint_id,              |                              |
  |     webhook_secret,            |                              |
  |     trigger_ids, mode, url}    |                              |
```

### Steps

1. **Endpoint creation:** A new endpoint row is inserted with a nanoid, the agent's webhook URL, delivery mode, chain IDs, recipient address (defaults to agent address), and endpoint policies. A unique webhook secret (`secrets.token_hex(32)`) is generated.

2. **Template triggers:** For each `template_slug`, the server looks up the template by slug, then creates a trigger row inheriting the template's `event_signature`, `abi`, `default_filters`, `webhook_event_type`, and `reputation_threshold`. Chain IDs come from the request or fall back to the template's defaults.

3. **Custom triggers:** For each entry in `custom_triggers`, a trigger row is created with the provided `event_signature`, optional `abi`, `contract_address`, `chain_ids`, `filter_rules`, and `webhook_event_type`.

4. **Response:** Returns `endpoint_id`, `webhook_secret` (one-time), all `trigger_ids`, `mode`, and `url`. The middleware is now live -- incoming events matching these triggers will be decoded, filtered, and dispatched to the agent's URL.

### Example

An agent that wants to monitor whale USDC transfers and Uniswap swaps on Base:

```json
{
  "url": "https://my-agent.example.com/webhook",
  "chains": [8453],
  "template_slugs": ["whale-transfer", "dex-swap"],
  "custom_triggers": [
    {
      "event_signature": "OwnershipTransferred(address,address)",
      "name": "Ownership Watch",
      "contract_address": "0x1234...",
      "filter_rules": [{"field": "newOwner", "op": "neq", "value": "0x0000000000000000000000000000000000000000"}]
    }
  ]
}
```

---

## 16. x402 Bazaar and Service Discovery

TripWire publishes a machine-readable service manifest at `/.well-known/x402-manifest.json` for agent discovery.

### Manifest Structure

```json
{
  "@context": "https://x402.org/context",
  "name": "TripWire",
  "description": "Programmable onchain event triggers for AI agents",
  "version": "1.0.0",
  "identity": {
    "protocol": "ERC-8004",
    "registry": "0x8004A169FB4a3325136EB29fA0ceB6D2e539a432"
  },
  "mcp": {
    "endpoint": "/mcp",
    "transport": "streamable-http",
    "tools": ["register_middleware", "create_trigger", "list_triggers", ...]
  },
  "services": [
    {"name": "register_middleware", "price": "$0.003", "network": "eip155:8453"},
    {"name": "create_trigger", "price": "$0.003", "network": "eip155:8453"},
    {"name": "activate_template", "price": "$0.001", "network": "eip155:8453"}
  ],
  "supported_chains": [
    {"chain_id": 8453, "name": "Base"},
    {"chain_id": 1, "name": "Ethereum"},
    {"chain_id": 42161, "name": "Arbitrum"}
  ],
  "trigger_templates": "/mcp (use list_templates tool)"
}
```

### Discovery Flow

1. An agent (or agent framework) fetches `/.well-known/x402-manifest.json`.
2. The manifest advertises MCP endpoint, available tools, pricing, and supported chains.
3. The agent calls `POST /mcp` with `initialize` to start an MCP session.
4. The agent calls `tools/list` to discover available tools and their input schemas.
5. The agent calls `register_middleware` (or individual tools) to set up triggers.

This follows the emerging x402 service discovery pattern where AI agents can autonomously discover, evaluate, and pay for infrastructure services.

---

## 17. Full System Flow

```
                                  ┌──────────────────────────────────┐
                                  │         AI Agent                 │
                                  │  (Claude, GPT, custom agent)     │
                                  └──────────┬───────────────────────┘
                                             │
                          ┌──────────────────┼──────────────────┐
                          │  1. Discover      │  5. Receive      │
                          │  /.well-known/    │  webhooks        │
                          │  x402-manifest    │                  │
                          ▼                  │                  ▼
              ┌───────────────────┐          │     ┌──────────────────┐
              │  MCP Server       │          │     │  Agent's API     │
              │  POST /mcp        │          │     │  (webhook URL)   │
              │                   │          │     └──────────────────┘
              │  Tools:           │          │              ▲
              │  register_middleware          │              │
              │  create_trigger   │          │     ┌────────┴─────────┐
              │  list_templates   │          │     │  Convoy          │
              │  activate_template│          │     │  (retry + DLQ)   │
              │  ...              │          │     └────────┬─────────┘
              └───────┬───────────┘          │              │
                      │ 2. Create            │     6. Dispatch
                      │ endpoint + triggers   │              │
                      ▼                      │     ┌────────┴─────────┐
              ┌───────────────────┐          │     │  EventProcessor  │
              │  Supabase         │          │     │                  │
              │                   │◄─────────┘     │  detect → decode │
              │  endpoints        │                │  → filter → dedup│
              │  triggers         │◄───────────────│  → identity      │
              │  trigger_templates│  3. Query       │  → dispatch      │
              │  events           │  triggers       └────────┬─────────┘
              │  nonces           │                          │
              └───────────────────┘                 4. Ingest│
                                                            │
                                              ┌─────────────┴──────────┐
                                              │                        │
                                    ┌─────────┴───────┐    ┌───────────┴──────┐
                                    │ Goldsky Turbo    │    │ x402 Facilitator │
                                    │ (2-4s latency)   │    │ (~100ms latency) │
                                    └─────────┬───────┘    └───────────┬──────┘
                                              │                        │
                                    ┌─────────┴────────────────────────┴──────┐
                                    │          EVM Chains                     │
                                    │    Base / Ethereum / Arbitrum           │
                                    └────────────────────────────────────────┘
```

### End-to-End Sequence

1. **Discover:** Agent fetches `/.well-known/x402-manifest.json` to find the MCP endpoint and available tools.
2. **Register:** Agent calls `register_middleware` via MCP, creating an endpoint and triggers (from templates or custom definitions). Receives `endpoint_id`, `webhook_secret`, and `trigger_ids`.
3. **Index:** Goldsky Turbo indexes EVM chains and delivers raw logs to `POST /api/v1/ingest`. The x402 facilitator delivers pre-confirmed payments to `POST /api/v1/ingest/facilitator`.
4. **Process:** `EventProcessor._detect_event_type` checks topic0 against hardcoded signatures, then falls back to the trigger registry. Matched triggers route to `_process_dynamic_event`; ERC-3009 events route to `_process_erc3009_event`.
5. **Decode + Filter:** Dynamic events are decoded with the trigger's ABI via `decode_event_with_abi()`, then filtered with `evaluate_filters()`. ERC-3009 events use the dedicated decoder.
6. **Dispatch:** Approved events are sent to the agent's webhook URL via Convoy (with retries, HMAC signing, and DLQ) or pushed via Supabase Realtime for Notify-mode endpoints.
