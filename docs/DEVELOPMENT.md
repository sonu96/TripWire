# TripWire Development Guide

## 1. Quick Start

```bash
# Clone the repository
git clone https://github.com/your-org/tripwire.git
cd tripwire

# Create a virtual environment and install dependencies
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Copy the environment template and fill in your values
cp .env.example .env

# Start the dev server (auth bypassed, uses Hardhat account #0)
python dev_server.py
```

The dev server starts on port **3402** by default (`http://localhost:3402`). It gives you:

- Full FastAPI app with all routes mounted under `/api/v1`
- MCP server at `/mcp`
- Wallet auth bypassed (all requests authenticated as Hardhat account #0)
- Hot reload enabled
- x402 payment gating disabled (treasury address is empty by default)
- Prometheus metrics at `/metrics`
- Health checks at `/health`, `/health/detailed`, and `/ready`

**Goldsky data plane note:** Full end-to-end event flow requires Goldsky Turbo pipelines deployed and streaming live chain data to TripWire's `/api/v1/ingest/goldsky` endpoint. For local development you do not need Goldsky running — use `POST /api/v1/ingest/event` to submit a single raw event dict directly. This bypasses Goldsky entirely and runs the event through `EventProcessor` (or publishes to the Redis event bus when `EVENT_BUS_ENABLED=true`), giving you the full processing pipeline without a live chain connection.

**Unified processor:** Set `UNIFIED_PROCESSOR=true` to route both ERC-3009 and dynamic triggers through the single-path `_process_unified()` pipeline. This gives dynamic triggers finality checking, full policy evaluation, and execution state metadata. Default is `false` (legacy split paths). See [TWSS-1 Skill Spec](SKILL-SPEC.md) for the execution semantics.

Minimum `.env` for local dev (Supabase required):

```
APP_ENV=development
SUPABASE_URL=https://your-project.supabase.co
SUPABASE_ANON_KEY=your_anon_key
SUPABASE_SERVICE_ROLE_KEY=your_service_role_key
REDIS_URL=redis://localhost:6379
```

Goldsky-related env vars (both are optional in dev):

```
GOLDSKY_WEBHOOK_SECRET=   # validates inbound Goldsky Turbo webhooks at /ingest/goldsky;
                          # if empty, ingest auth is skipped (safe for local dev only)
GOLDSKY_EDGE_API_KEY=     # authenticated Goldsky Edge RPC; if empty, falls back to the
                          # public RPC URLs (BASE_RPC_URL, ETHEREUM_RPC_URL, ARBITRUM_RPC_URL)
```

---

## 2. Project Structure

```
tripwire/
  api/             FastAPI routes, auth middleware, rate limiting
  config/          Settings via pydantic-settings (.env loading)
  db/              Supabase client, repositories, SQL migrations
    migrations/    Numbered SQL migration files (001..023)
    repositories/  Data access: endpoints, events, nonces, triggers, webhooks
  identity/        ERC-8004 identity resolution
    resolver.py    ERC8004Resolver (prod, makes eth_call to onchain registry)
                   and MockResolver (dev, returns hardcoded identities)
    reputation.py  ReputationRegistry RPC client with 300s in-memory cache
  ingestion/       Goldsky pipeline processing, finality tracking,
                   event_bus.py (Redis Streams), trigger_worker.py,
                   dlq_consumer.py (Redis Streams DLQ consumer)
    pipeline.py    Goldsky Turbo pipeline config builder and CLI management
                   (deploy_pipeline, start_pipeline, stop_pipeline, get_pipeline_status)
  mcp/             MCP server, tool handlers, 3-tier auth (PUBLIC/SIWX/X402)
  notify/          Supabase Realtime notifier (notify-mode delivery)
  observability/   Tracing (OTel), metrics (Prometheus), audit logging, Sentry
  rpc.py           Goldsky Edge RPC client: eth_call, eth_blockNumber, lazy singleton
                   httpx.AsyncClient; used by finality poller and identity resolver
  types/           Shared Pydantic v2 models
  utils/           Helpers: keccak256 topic computation, etc.
  webhook/         Convoy integration (with circuit breaker), direct httpx fast path, DLQ handler
  main.py          App factory, lifespan, middleware stack, route mounting

sdk/
  tripwire_sdk/    Python SDK package for API consumers
    client.py      TripwireClient (async context manager, SIWE auth)
    verify.py      Webhook signature verification (HMAC-SHA256)
    signer.py      SIWE message construction and EIP-191 signing
    types.py       Pydantic models: Endpoint, Subscription, Event, etc.
    errors.py      TripWireError exception hierarchy

tests/
  conftest.py      Shared fixtures (MockRedis, test wallets, sample models)
  test_routes.py   Unit tests for API routes
  integration/
    test_pipeline.py  End-to-end pipeline integration tests

dev_server.py      Dev mode entry point (auth bypass, hot reload)
```

---

## 3. Goldsky Data Plane

TripWire's chain data comes from two Goldsky services:

### Goldsky Turbo (ingest path)

Goldsky Turbo pipelines stream decoded on-chain events to TripWire's ingest endpoint. `tripwire/ingestion/pipeline.py` owns the full lifecycle:

- `build_pipeline_config(chain_id)` / `build_pipeline_yaml(chain_id)` — generate the Goldsky Turbo YAML config for a chain. The pipeline joins `AuthorizationUsed` and `Transfer` logs from the same transaction so TripWire receives both payment and transfer metadata in one row.
- `deploy_pipeline(chain_id)` — writes the config to a temp file and calls `goldsky turbo apply`.
- `start_pipeline` / `stop_pipeline` / `get_pipeline_status` — manage running pipelines via the Goldsky CLI.

The deployed pipeline POSTs to `{APP_BASE_URL}/api/v1/ingest/goldsky` with an `Authorization: Bearer {GOLDSKY_WEBHOOK_SECRET}` header. The secret is validated by the `_verify_goldsky_request` dependency on that route.

Required env vars for pipeline deployment (not needed for local dev):

```
GOLDSKY_API_KEY=          # Goldsky platform API key (used by the goldsky CLI)
GOLDSKY_PROJECT_ID=       # Goldsky project identifier
GOLDSKY_WEBHOOK_SECRET=   # HMAC secret sent in the Authorization header on every
                          # inbound webhook; if empty, auth check is skipped
```

### Goldsky Edge RPC (`tripwire/rpc.py`)

`tripwire/rpc.py` is the single shared JSON-RPC client used by the finality poller (`finality_poller.py`) and the identity resolver. It exposes two primitives:

- `eth_block_number(chain_id)` — returns the latest block number as an integer.
- `eth_call(chain_id, to, data)` — sends an `eth_call` and returns the hex result, or `None` on failure.

Both use a lazily-created module-level `httpx.AsyncClient`. If `GOLDSKY_EDGE_API_KEY` is set, the client adds an `Authorization: Bearer` header to every request, routing through Goldsky's authenticated Edge RPC endpoints. If the key is empty (default in dev), requests go to the public RPC URLs:

```
BASE_RPC_URL=         # defaults to public Base RPC if empty
ETHEREUM_RPC_URL=     # defaults to public Ethereum RPC if empty
ARBITRUM_RPC_URL=     # defaults to public Arbitrum RPC if empty
GOLDSKY_EDGE_API_KEY= # optional; enables Goldsky Edge for higher rate limits
```

The `close_rpc_client()` coroutine is called during app shutdown (in `main.py` lifespan) to cleanly drain the client connection pool.

---

## 4. Dev Server

`dev_server.py` is the local development entry point. It does two things the production entry point (`tripwire/main.py`) does not:

1. **Forces `APP_ENV=development`** before any settings are imported.
2. **Overrides `require_wallet_auth`** with a stub that returns a fixed `WalletAuthContext`, removing the need to sign every request with an Ethereum private key during development.

The default dev wallet is **Hardhat account #0**:

```
0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266
```

To use a different address:

```bash
DEV_WALLET_ADDRESS=0xYourAddress python dev_server.py
```

The dev server runs with `reload=True` so file changes restart the process automatically.

**Identity resolution in dev mode:** When `APP_ENV=development`, the identity layer automatically uses `MockResolver` instead of `ERC8004Resolver`. The mock returns predictable identities for three hardcoded agent addresses without making any RPC calls:

| Agent slug | Address |
|---|---|
| `trading-bot` | `0x70997970C51812dc3A010C7d01b50e0d17dc79C8` |
| `data-oracle` | `0x3C44CdDdB6a900fa2b585dd299e03d12FA4293BC` |
| `payment-agent` | `0x90F79bf6EB2c4f870365E785982E1f101E93b906` |

Any address not in this list resolves to `None` (no registered identity). No RPC node is required for identity lookups during local development.

**Important:** `dev_server.py` must never be deployed to production. It is the only file where the auth bypass exists.

---

## 5. SDK

The `tripwire_sdk` package (`sdk/tripwire_sdk/`) is meant for consumers who receive webhooks from TripWire or interact with the API programmatically.

### TripwireClient

Async context manager that authenticates every request by signing a SIWE message with the caller's Ethereum private key:

```python
from tripwire_sdk import TripwireClient

async with TripwireClient(private_key="0x...") as client:
    print(client.wallet_address)

    ep = await client.register_endpoint(
        url="https://example.com/webhook",
        mode="execute",
        chains=[8453],
        recipient="0xAbC...",
    )

    events = await client.list_events(limit=10)
```

The client fetches a fresh nonce from the server before each request (`GET /auth/nonce`). If `enable_x402=True` (the default), it wraps `httpx.AsyncClient` with `x402Client` so HTTP 402 responses are auto-handled with on-chain payments.

Install the x402 integration with: `pip install tripwire-sdk[x402]`

### Webhook Verification

Two functions for verifying inbound webhook signatures:

```python
from tripwire_sdk.verify import verify_webhook_signature, verify_webhook_signature_safe

# Raises WebhookVerificationError on failure
verify_webhook_signature(payload_bytes, headers_dict, secret)

# Returns False instead of raising
verify_webhook_signature_safe(payload_bytes, headers_dict, secret)
```

Signature header scheme: `X-TripWire-Signature: t={unix_timestamp},v1={hex_hmac_sha256}`
Algorithm: HMAC-SHA256 over `{timestamp}.{raw_body}`
Tolerance: 300 seconds (5 minutes)

For testing, `sign_payload(payload, secret)` generates valid TripWire signature headers without a running server.

### Auth Helpers

`signer.py` provides low-level SIWE auth primitives:

- `sign_auth_message(key_or_account, address, nonce, method, path, body_bytes)` -- returns `(signature_hex, issued_at, expiration_time)`
- `make_auth_headers(key_or_account, address, path, nonce=..., method=...)` -- returns the five auth headers dict: `X-TripWire-Address`, `X-TripWire-Signature`, `X-TripWire-Nonce`, `X-TripWire-Issued-At`, `X-TripWire-Expiration`
- `build_auth_message(address, nonce, method, path, body_bytes)` -- returns `(message_text, issued_at, expiration_time)` without signing

The SIWE statement embeds a body hash: `{METHOD} {path} {sha256(body_bytes)}`.

### Types

Key types exported from `tripwire_sdk.types`:

| Type | Description |
|---|---|
| `ChainId` | Enum: ETHEREUM (1), BASE (8453), ARBITRUM (42161) |
| `EndpointMode` | Enum: NOTIFY, EXECUTE |
| `WebhookEventType` | Enum: payment.confirmed, payment.pending, payment.pre_confirmed, payment.failed, payment.reorged, payment.finalized |
| `Endpoint` | Registered webhook endpoint (immutable, frozen Pydantic model) |
| `Subscription` | Notify-mode subscription with filter rules |
| `EndpointPolicies` | Policy configuration: min/max amount, allowed/blocked senders, reputation, finality depth (`int | None`, default `None`) |
| `WebhookPayload` | Full webhook delivery envelope containing transfer, finality, and identity data |
| `ExecutionState` | Enum: lifecycle states for event execution (e.g., pre_confirmed, confirmed, finalized, failed, reorged) |
| `TrustSource` | Enum: trust provenance (goldsky, facilitator, rpc) |
| `PaginatedResponse` | Cursor-paginated event list |

The `derive_execution_metadata()` helper function (in `tripwire/types/models.py`) computes execution state and trust metadata from an event's nonce source and finality status, used by the processor to enrich webhook payloads.

All SDK models inherit from `TripWireBaseModel` which is configured with `extra="ignore"` (silently drops unknown fields from the server) and `frozen=True` (immutable instances).

### Errors

Exception hierarchy:

```
TripWireError (base, carries status_code + detail)
  TripWireAuthError         401/403
  TripWireNotFoundError     404
  TripWireRateLimitError    429  (has retry_after attribute)
  TripWireServerError       5xx
  TripWireValidationError   response parsing failures (status_code=0)
```

### Known Bug: Chain ID Mismatch in signer.py

`signer.py` hardcodes `Chain ID: 1` (Ethereum mainnet) in the SIWE message at line 40:

```python
f"Chain ID: 1\n"
```

The server's `settings.py` defaults to `x402_network = "eip155:8453"` (Base mainnet, chain ID 8453). This mismatch means the SIWE chain ID in signed messages does not match the server's expected chain. Because the chain ID is embedded in the SIWE message text, a mismatch causes the server to reconstruct a different message than what the client signed. The recovered signer address will not match the claimed address, so authentication fails. The SDK and server are currently incompatible on this field.

---

## 6. Database Migrations

Migrations live in `tripwire/db/migrations/` as numbered SQL files. Apply them manually via `psql` or the Supabase SQL Editor (Dashboard > SQL Editor > paste and run).

There is no automated migration runner. Run them in order against your Supabase project database.

### Full Migration List

| # | File | Description |
|---|---|---|
| 001 | `001_initial.sql` | Initial schema: endpoints, subscriptions, events, nonces, webhook_deliveries tables |
| 002 | `002_add_event_type_data.sql` | Add type and data columns to events table for webhook payload format |
| 003 | `003_realtime_events.sql` | Realtime events table for Supabase Realtime notify-mode delivery |
| 004 | `004_add_endpoint_id_to_events.sql` | Add endpoint_id column to link events to matching endpoints |
| 005 | `005_api_key_index.sql` | Index on api_key_hash for fast authentication lookups |
| 006 | `006_svix_ids.sql` | Add Svix provider IDs to endpoints table (pre-Convoy era) |
| 007 | `007_key_rotation.sql` | API key rotation with grace period columns |
| 008 | `008_svix_to_convoy.sql` | Rename Svix columns to Convoy equivalents |
| 009 | `009_performance_indexes.sql` | Composite indexes for webhook_deliveries and common query patterns |
| 010 | `010_wallet_auth.sql` | Replace API key auth with wallet-based auth (owner_address column) |
| 011 | `011_rls_policies.sql` | Row Level Security policies for multi-tenant wallet isolation |
| 012 | `012_audit_logging.sql` | Audit log table for tracking administrative actions |
| 013 | `013_trigger_registry.sql` | Trigger registry tables: trigger_templates and triggers for MCP-driven agent triggers |
| 014 | `014_event_endpoints_join.sql` | Many-to-many join table for events to endpoints (fixes single endpoint_id limitation) |
| 015 | `015_install_count_fix.sql` | Fix gameable install_count on trigger_templates (prevent inflation via create/delete) |
| 016 | `016_webhook_secret_drop.sql` | Drop plaintext webhook_secret from endpoints (deploy code first, then run migration) |
| 017 | `017_topic0_column.sql` | Precomputed topic0 (keccak256) column on triggers and templates for fast matching |
| 018 | `018_nonce_reorg_support.sql` | Reorg-aware nonce deduplication (reorged_at column, status tracking) |
| 019 | `019_nonce_archival.sql` | Nonce archival table and function for unbounded table growth |
| 020 | `020_unified_event_lifecycle.sql` | Unified event lifecycle: `source` column on nonces, `record_nonce_or_correlate` function with SELECT FOR UPDATE (fixes nonce TOCTOU race), enables Goldsky to promote facilitator pre_confirmed events |
| 021 | `021_dlq_retry_count.sql` | Persistent `dlq_retry_count` column on webhook_deliveries (replaces in-memory retry counter in DLQHandler) |
| 022 | `022_audit_latency.sql` | Add `execution_latency_ms` column to `audit_log` for tracking MCP tool execution latency |
| 023 | `023_agent_metrics_view.sql` | Create `agent_metrics` materialized view for per-agent metrics; powers `GET /stats/agent-metrics` endpoint |

---

## 7. Testing

### Setup

Tests use `pytest` with `pytest-asyncio` for async support. The test environment is configured in `tests/conftest.py`, which sets `APP_ENV=testing` and provides dummy environment variables before any settings import.

```bash
# Run all unit tests
pytest tests/ -v

# Run integration tests only
pytest tests/ -m integration -v

# Run with coverage
pytest tests/ --cov=tripwire --cov-report=term-missing
```

### Testing with Identity

The `MockResolver` makes identity-dependent code testable without any external dependencies:

- **Predictable outputs:** `MockResolver` returns a fixed `AgentIdentity` for the three known agent addresses (trading-bot, data-oracle, payment-agent) and `None` for all others, so assertions are deterministic.
- **Dependency injection override:** Any route or service that receives the resolver via FastAPI `Depends()` can be overridden in tests by passing a `MockResolver` instance directly. For example:

  ```python
  app.dependency_overrides[get_identity_resolver] = lambda: MockResolver()
  ```

- **`sample_identity` fixture:** `conftest.py` provides a `sample_identity` fixture that returns a pre-built `AgentIdentity` model matching the `trading-bot` mock agent. Use it in tests that need a non-`None` identity without constructing one manually.

### Key Fixtures (from `tests/conftest.py` and `tests/_wallet_helpers.py`)

| Fixture | Description |
|---|---|
| `test_wallet` | Primary test wallet derived from Hardhat account #0 private key |
| `other_wallet` | Secondary wallet from Hardhat account #1 |
| `mock_redis` | In-memory Redis mock with `seed_nonce()` method for SIWE nonce management |
| `auth_headers` | Pre-built auth headers for the primary test wallet |
| `sample_transfer` | Pre-built `ERC3009Transfer` Pydantic model |
| `sample_endpoint` | Pre-built `Endpoint` model |
| `sample_identity` | Pre-built `AgentIdentity` model |
| `sample_raw_log` | Raw Goldsky-decoded log dict for ingestion |

### Test Structure

- **Unit tests** (`tests/test_routes.py`): Use `MockSupabase` with lightweight chain-able query builder. Override `require_wallet_auth` and `_verify_goldsky_request` dependencies. Use `httpx.ASGITransport` to call the app without a running server.

- **Integration tests** (`tests/integration/test_pipeline.py`): Use `StatefulMockSupabase` that tracks inserts/selects/upserts across tables. Wire the real `EventProcessor` with all repositories. Cover scenarios: register then ingest, nonce deduplication, wallet auth round-trip, policy rejection, notify-mode with subscriptions.

- **Execution state and decoder tests** (`tests/test_execution_state.py`): 15 tests covering `execution_state_from_status()` mapping, `Decoder` protocol compliance, and both `ERC3009Decoder` and `AbiGenericDecoder` wrappers.

- **x402 middleware tests** (`tests/test_routes.py::TestX402PaymentMiddleware`): Require the `x402` package; skipped when not installed.

### Coverage Gap

Test coverage is approximately 60%. Notable gaps:

- No tests for MCP tool handlers
- No tests for event bus (Redis Streams) or trigger workers
- No tests for finality poller, DLQ handler, or DLQ consumer
- No CI/CD pipeline exists to run tests automatically

---

## 8. Code Conventions

These conventions are defined in `CLAUDE.md` and apply project-wide:

- **Pydantic v2** for all input/output validation. Use `model_dump()`, not `.dict()`.
- **async/await throughout** -- FastAPI routes, httpx calls, repository methods that hit external services.
- **structlog** for structured JSON logging. Use `structlog.get_logger(__name__)`.
- **No web3.py** -- use `httpx` for raw JSON-RPC calls and `eth-abi` for ABI decoding.
- **All amounts in smallest unit** -- USDC uses 6 decimals, so `$5.00` = `5000000`.
- **httpx** as the sole async HTTP client.
- **MCP tools** follow the Model Context Protocol spec and are mounted at `/mcp`.

---

## 9. Adding a New Trigger Template

Trigger templates are predefined event patterns that agents can instantiate via MCP. To add a new one:

### Step 1: Compute the topic0

Use `tripwire/utils/topic.py` to compute the keccak256 hash of the Solidity event signature:

```python
from tripwire.utils.topic import compute_topic0

topic0 = compute_topic0("Transfer(address,address,uint256)")
# '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
```

The function accepts either a Solidity event signature string or a pre-computed `0x`-prefixed hex hash (pass-through).

### Step 2: Write a SQL migration

Create a new migration file (e.g., `022_my_template.sql`) that inserts into `trigger_templates`:

```sql
-- Migration 022: Add MyEvent trigger template

INSERT INTO trigger_templates (
    id, slug, name, description, category,
    event_signature, topic0, abi,
    default_chains, default_filters, parameter_schema,
    webhook_event_type, reputation_threshold, install_count,
    created_at, updated_at
) VALUES (
    gen_random_uuid(),
    'my-event',                                    -- unique slug for MCP lookup
    'My Event Monitor',                            -- human-readable name
    'Monitors MyEvent(address,uint256) emissions', -- template description
    'defi',                                        -- category for filtering
    'MyEvent(address,uint256)',                     -- Solidity event signature
    '0x<computed_topic0_hash>',                     -- keccak256 of the signature
    '[{"type":"event","name":"MyEvent","inputs":[{"name":"account","type":"address","indexed":true},{"name":"amount","type":"uint256","indexed":false}]}]',
    '[8453, 1]'::jsonb,                              -- default chain IDs
    '[]'::jsonb,                                   -- default JMESPath filter rules
    '{}'::jsonb,                                   -- JSON Schema for customizable params
    'payment.confirmed',                           -- webhook event type string
    0.0,                                           -- minimum reputation score
    0,                                             -- install count (starts at 0)
    NOW(), NOW()
);
```

### Step 3: Apply the migration

Run the SQL against your Supabase project database via `psql` or the SQL Editor.

### Step 4: Verify via MCP

Agents can now discover the template via the `list_templates` MCP tool and instantiate it via `activate_template`.

---

## 10. Adding a New MCP Tool

MCP tools are registered in `tripwire/mcp/server.py` and implemented in `tripwire/mcp/tools.py`.

### Step 1: Write the handler

Add a new async function in `tripwire/mcp/tools.py`:

```python
async def my_new_tool(
    params: dict, ctx: MCPAuthContext, repos: dict
) -> dict:
    """Description of what this tool does."""
    endpoint_repo, trigger_repo, template_repo, event_repo = _repos(repos)

    # Tool logic here...

    return {"result": "..."}
```

Every handler receives:
- `params` -- the input parameters from the MCP call
- `ctx` -- `MCPAuthContext` with `agent_address` and `auth_tier`
- `repos` -- dict containing `endpoint_repo`, `trigger_repo`, `template_repo`, `event_repo`, and `supabase`

### Step 2: Register in server.py

Add a `_register()` call in `tripwire/mcp/server.py` alongside the existing 8 tools:

```python
_register(
    name="my_new_tool",
    description="One-line description for agent discovery.",
    input_schema={
        "type": "object",
        "properties": {
            "param1": {"type": "string", "description": "..."},
        },
        "required": ["param1"],
    },
    handler=tool_handlers.my_new_tool,
    auth_tier=AuthTier.SIWX,   # PUBLIC, SIWX, or X402
    price=None,                 # Set a dollar amount string for X402 tools, e.g. "$0.10"
    min_reputation=0.0,         # Minimum ERC-8004 reputation score to call this tool
)
```

### Step 3: Choose the auth tier

| Tier | When to use | Example |
|---|---|---|
| `PUBLIC` | No auth needed, read-only discovery | `tools/list`, `initialize` |
| `SIWX` | Free but requires wallet signature | `list_triggers`, `search_events` |
| `X402` | Per-call micropayment via x402 protocol | `register_middleware` (paid endpoint creation) |

If `price` is set (e.g., `"$0.10"`), the MCP server collects an x402 payment before executing the handler.

---

## 11. Known Issues for Contributors

### Fixed: Nonce TOCTOU Race Condition

**Resolved in:** Migration 020 (`020_unified_event_lifecycle.sql`)

Previously, concurrent facilitator and Goldsky events could race on nonce insertion, causing one to silently drop. Migration 020 introduces a `record_nonce_or_correlate` PostgreSQL function that uses `SELECT FOR UPDATE` to atomically claim or correlate nonces, eliminating the TOCTOU window. Facilitator events now pre-claim nonces with `source = 'facilitator'`; when Goldsky delivers the same nonce, it correlates and promotes the event to `payment.confirmed` rather than rejecting as a duplicate.

### Note: Redis Streams DLQ Is Now Consumed

The `tripwire:dlq` stream was previously write-only (trigger workers wrote permanently-failed events but nothing read them). The new `tripwire/ingestion/dlq_consumer.py` runs as a background task when `EVENT_BUS_ENABLED=true`, reading failed events, logging them, firing an alert webhook if configured, and incrementing a Prometheus counter.

### Bug: Chain ID Mismatch (SDK signer vs. server)

**Location:** `sdk/tripwire_sdk/signer.py` line 40

The SIWE message hardcodes `Chain ID: 1` (Ethereum mainnet), but the server defaults to Base mainnet (`chain_id=8453`, configured as `x402_network = "eip155:8453"` in `settings.py`). This is currently harmless because the server does not validate the chain ID field in SIWE messages. If chain ID validation is added, all SDK clients will fail authentication.

### Bug: MCP `register_middleware` Does Not Register with Convoy

**Location:** `tripwire/mcp/tools.py`, `register_middleware` function

The handler inserts the endpoint row directly into Supabase but never calls `webhook_provider.create_app()` or `webhook_provider.create_endpoint()`. This means endpoints created via MCP exist in the database but Convoy has no corresponding application or endpoint registered. Webhook delivery will fail for MCP-created execute-mode endpoints. The REST API route (`tripwire/api/routes/endpoints.py`) does call the webhook provider correctly.

### Bug: RLS Policies Not Wired at the Application Layer

**Location:** `tripwire/db/migrations/011_rls_policies.sql`

Migration 011 defines Row Level Security policies that depend on a PostgreSQL session variable `app.current_wallet`. The application layer never sets this session variable -- all database access uses the `service_role` key which bypasses RLS entirely. The RLS policies exist in the schema but are effectively inactive. Multi-tenant isolation relies on application-level `WHERE owner_address = ...` filtering in repositories, not database-enforced RLS.

### Test Coverage Gap (~60%)

No tests exist for: MCP tool handlers, event bus / trigger workers, finality poller, DLQ handler, nonce archiver, or the webhook provider abstraction. The integration test suite covers the core pipeline (register, ingest, deduplicate, policy evaluation, notify mode) but not the operational subsystems.

### No CI/CD Pipeline

There is no GitHub Actions workflow, no automated test runner, and no deployment pipeline. Tests run locally only via `pytest`.
