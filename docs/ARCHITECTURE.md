# TripWire Architecture

## 1. System Overview

TripWire is execution middleware for onchain payments, described as "Stripe Webhooks for x402." It detects ERC-3009 `TransferWithAuthorization` payments on Ethereum, Base, and Arbitrum, then delivers structured webhook notifications to registered endpoints.

TripWire is built for **AI agent developers** who need reliable, low-latency payment event detection without running their own indexing infrastructure. An agent registers a webhook endpoint (paying via the x402 protocol), subscribes to payment events for a specific recipient address, and receives webhook callbacks whenever a matching payment lands onchain.

Key design goals:

- **Multi-path ingestion** with different latency/reliability tradeoffs
- **Sub-second detection** via the x402 facilitator fast path
- **Finality awareness** with per-chain confirmation depths and reorg detection
- **ERC-8004 agent identity** enrichment and policy-based filtering
- **Wallet-native authentication** using SIWE (EIP-4361) with no API keys
- **Reliable delivery** through Convoy with exponential retry and dead-letter queues

The stack is Python (FastAPI + uvicorn), Supabase (PostgreSQL + Realtime), Redis (nonce replay prevention), Convoy (webhook delivery), and direct JSON-RPC calls for finality and identity resolution.

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

Every ingestion path feeds into the `EventProcessor`, which orchestrates the full pipeline. The steps are:

### Step 1: Event Type Detection

The processor inspects `topic[0]` of the raw log against `_EVENT_SIGNATURES` to determine the event type. Currently only `erc3009_transfer` is registered (matching both `Transfer` and `AuthorizationUsed` topic signatures).

### Step 2: Decode

`decoder.py` extracts structured data from the raw log:

- **Goldsky path:** Parses the pre-decoded row with `decoded` (authorizer, nonce) and `transfer` (from, to, value) fields.
- **Raw path:** Uses `decode_erc3009_from_logs()` to decode both `Transfer(address,address,uint256)` and `AuthorizationUsed(address,bytes32)` from raw EVM log topics and data.
- **Facilitator path:** Skipped entirely; data arrives pre-structured.

Chain ID is resolved from the raw log's `chain_id` field or by reverse-mapping the USDC contract address.

### Step 3: Nonce Deduplication

The `nonce` (bytes32) from the `AuthorizationUsed` event is checked against the `nonces` table, keyed by `(chain_id, nonce, authorizer)`. If already seen, the event is short-circuited as a duplicate. This runs first because it is the cheapest rejection path.

### Step 4: Finality Check + Identity Resolution (Parallel)

These two stages have zero data dependencies on each other and run concurrently via `asyncio.gather()`:

- **Finality:** `check_finality()` fetches the current block number via JSON-RPC and computes `confirmations = current_block - event.block_number`. Each chain has a configured finality depth (Ethereum: 12, Base: 3, Arbitrum: 1). Returns a `FinalityStatus` with `is_finalized` flag.
- **Identity:** The `IdentityResolver` looks up the sender's ERC-8004 agent identity (see section 9).

For the facilitator fast path, finality is skipped (returns `None`).

### Step 5: Endpoint Matching

Active endpoints are fetched by `recipient` address (with a 30-second in-memory TTL cache) and filtered by:

- Recipient address match (case-insensitive)
- Chain ID membership in the endpoint's `chains` list
- Active status

### Step 6: Policy Evaluation

Each matched endpoint's `policies` configuration is evaluated against the transfer data and identity:

- `min_amount` / `max_amount` — transfer value bounds
- `allowed_senders` / `blocked_senders` — sender address allowlist/blocklist
- `required_agent_class` — ERC-8004 agent class filter
- `min_reputation_score` — minimum reputation threshold
- `finality_depth` — minimum confirmations required

Endpoints that fail policy are logged and excluded from dispatch.

### Step 7: Event Recording

An event row is inserted into the `events` table with all structured columns (chain_id, tx_hash, block_number, from_address, to_address, amount, authorizer, nonce, token, status, finality_depth) plus JSONB `data` and optional `identity_data`.

### Step 8: Dispatch

Approved endpoints are split by delivery mode:

- **Execute mode:** Webhooks dispatched via Convoy (see section 4). Each delivery is recorded in `webhook_deliveries` with the Convoy message ID.
- **Notify mode:** Events pushed via Supabase Realtime. Subscription filters are applied first (chains, senders, recipients, min_amount, agent_class).

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
