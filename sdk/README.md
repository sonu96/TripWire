# tripwire-sdk

Python SDK for TripWire -- programmable onchain event triggers for AI agents.

Authenticate with your Ethereum wallet, register webhook endpoints, subscribe to onchain payment events, and verify incoming webhook signatures.

## Installation

```bash
pip install tripwire-sdk
```

For automatic x402 payment handling (required for endpoint registration):

```bash
pip install tripwire-sdk[x402]
```

Requires Python 3.11+.

## Quick Start

```python
import asyncio
import os
from tripwire_sdk import TripwireClient, verify_webhook_signature

async def main():
    private_key = os.environ["TRIPWIRE_PRIVATE_KEY"]

    async with TripwireClient(private_key=private_key) as client:
        # Register a webhook endpoint
        endpoint = await client.register_endpoint(
            url="https://your-api.com/webhook",
            mode="execute",
            chains=[8453],  # Base
            recipient="0xYourAddress",
        )
        print(f"Endpoint ID: {endpoint.id}")

        # List your endpoints
        endpoints = await client.list_endpoints()
        for ep in endpoints:
            print(f"  {ep.id} -> {ep.url}")

asyncio.run(main())
```

The client **must** be used as an async context manager (`async with`).

## Authentication

Every API request is authenticated using SIWE (Sign-In with Ethereum, EIP-4361). The SDK handles this automatically:

1. **Nonce** -- A fresh one-time nonce is fetched from the server before each request to prevent replay attacks.
2. **SIWE message** -- A message is constructed containing the HTTP method, request path, and a SHA-256 hash of the request body.
3. **EIP-191 signature** -- The message is signed with your Ethereum private key.
4. **Auth headers** -- Five headers are attached to every request:
   - `X-TripWire-Address` -- your checksummed wallet address
   - `X-TripWire-Signature` -- hex-encoded EIP-191 signature
   - `X-TripWire-Nonce` -- the server-issued nonce
   - `X-TripWire-Issued-At` -- ISO-8601 timestamp
   - `X-TripWire-Expiration` -- ISO-8601 expiration (5 minutes from issuance)

You do not need to call any signing functions yourself. The `TripwireClient` handles it internally.

## x402 Payment Support

Registering a TripWire endpoint costs $1.00 USDC on Base. When you install the `x402` extra (`pip install tripwire-sdk[x402]`), the SDK transparently handles HTTP 402 Payment Required responses:

1. Your code calls `client.register_endpoint(...)`.
2. The server responds with 402 Payment Required.
3. The x402 interceptor constructs an ERC-3009 `transferWithAuthorization` signature using your private key (no on-chain transaction from your side).
4. The request is retried with the signed payment authorization in headers.
5. The server validates the authorization, submits the USDC transfer on-chain, and returns your new endpoint.

All of this is invisible to your code. If x402 is not installed, the client emits a warning and falls back to plain httpx -- but calls that require payment will fail with a 402 error.

You can disable x402 handling explicitly:

```python
TripwireClient(private_key="0x...", enable_x402=False)
```

## Client API Reference

### Constructor

```python
TripwireClient(
    private_key: str,
    base_url: str = "https://tripwire-production.up.railway.app",
    enable_x402: bool = True,
)
```

| Parameter | Description |
|---|---|
| `private_key` | Ethereum private key (hex string with `0x` prefix) |
| `base_url` | TripWire API base URL |
| `enable_x402` | Enable automatic x402 payment handling (requires the `x402` extra) |

**Property:** `client.wallet_address -> str` -- the checksummed Ethereum address derived from the private key.

---

### Endpoints

#### `register_endpoint`

```python
await client.register_endpoint(
    url: str,
    mode: str | EndpointMode,
    chains: list[int],
    recipient: str,
    policies: EndpointPolicies | dict | None = None,
) -> Endpoint
```

Register a new webhook endpoint. The `mode` is either `"execute"` or `"notify"`. The `chains` parameter is a list of chain IDs to monitor (e.g., `[8453]` for Base). The `recipient` is the onchain address to watch for incoming payments. Optional `policies` set filtering rules such as minimum amounts or sender allowlists.

#### `list_endpoints`

```python
await client.list_endpoints() -> list[Endpoint]
```

List all active endpoints owned by the authenticated wallet.

#### `get_endpoint`

```python
await client.get_endpoint(endpoint_id: str) -> Endpoint
```

Get a single endpoint by its ID.

#### `update_endpoint`

```python
await client.update_endpoint(endpoint_id: str, **kwargs) -> Endpoint
```

Update an endpoint. Pass keyword arguments for any fields to change (e.g., `url=`, `mode=`, `policies=`).

#### `delete_endpoint`

```python
await client.delete_endpoint(endpoint_id: str) -> None
```

Deactivate (soft-delete) an endpoint.

---

### Subscriptions

#### `create_subscription`

```python
await client.create_subscription(
    endpoint_id: str,
    filters: SubscriptionFilter | dict,
) -> Subscription
```

Create a subscription for a notify-mode endpoint. Filters control which events are delivered.

#### `list_subscriptions`

```python
await client.list_subscriptions(endpoint_id: str) -> list[Subscription]
```

List active subscriptions for an endpoint.

#### `delete_subscription`

```python
await client.delete_subscription(subscription_id: str) -> None
```

Deactivate a subscription.

---

### Events

#### `list_events`

```python
await client.list_events(
    cursor: str | None = None,
    limit: int = 50,
    **filters,
) -> PaginatedResponse
```

List events with cursor-based pagination. Pass additional keyword arguments as query filters. The returned `PaginatedResponse` contains a `data` list of `Event` objects, a `cursor` for the next page, and a `has_more` flag.

#### `get_event`

```python
await client.get_event(event_id: str) -> Event
```

Get a single event by its ID.

#### `get_endpoint_events`

```python
await client.get_endpoint_events(
    endpoint_id: str,
    cursor: str | None = None,
    limit: int = 50,
) -> PaginatedResponse
```

List events scoped to a specific endpoint, with cursor-based pagination.

---

### Utility

#### `get_nonce`

```python
await client.get_nonce() -> str
```

Fetch a fresh one-time nonce from the server. Called automatically before each authenticated request; you rarely need to call this directly.

### Sessions (Keeper)

#### `open_session`

```python
await client.open_session(
    budget: int | None = None,
    ttl_seconds: int | None = None,
    chain_id: int = 8453,
) -> Session
```

Open a pre-funded Keeper session. After opening, all subsequent API and MCP calls automatically include the `X-TripWire-Session` header, replacing per-call x402 payments. Budget is in smallest USDC units (6 decimals). Both `budget` and `ttl_seconds` are clamped to server-configured maximums.

#### `get_session`

```python
await client.get_session(session_id: str | None = None) -> Session
```

Get the current state of a session. If `session_id` is not provided, uses the currently active session. Raises `SessionError` if no session is active.

#### `close_session`

```python
await client.close_session(session_id: str | None = None) -> Session
```

Close a session early and return its final budget state. If `session_id` is not provided, closes the currently active session. Clears the internal session token so subsequent calls revert to per-call auth.

#### Session Usage Example

```python
from tripwire_sdk import TripwireClient

async with TripwireClient(private_key="0x...") as client:
    # Open a session with 5 USDC budget, 10-minute TTL
    session = await client.open_session(budget=5_000_000, ttl_seconds=600)
    print(f"Session {session.session_id}: {session.budget_remaining} remaining")

    # Subsequent calls automatically use the session token
    endpoints = await client.list_endpoints()
    events = await client.list_events(limit=10)

    # Check remaining budget
    status = await client.get_session()
    print(f"Budget remaining: {status.budget_remaining}")

    # Close when done (returns unspent budget info)
    final = await client.close_session()
    print(f"Closed. Unspent: {final.budget_remaining}")
```

---

## Types

All models inherit from `TripWireBaseModel` (a Pydantic `BaseModel` with `extra="ignore"`, `frozen=True`, and `str_strip_whitespace=True`).

### `Endpoint`

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique endpoint identifier |
| `url` | `str` | Webhook delivery URL |
| `mode` | `EndpointMode` | `"execute"` or `"notify"` |
| `chains` | `list[int]` | Chain IDs being monitored |
| `recipient` | `str` | Onchain address watched for payments |
| `owner_address` | `str` | Wallet address that owns this endpoint |
| `policies` | `EndpointPolicies` | Filtering and policy configuration |
| `active` | `bool` | Whether the endpoint is active (default `True`) |
| `created_at` | `datetime` | Creation timestamp |
| `updated_at` | `datetime` | Last update timestamp |

### `EndpointPolicies`

| Field | Type | Default | Description |
|---|---|---|---|
| `min_amount` | `str \| None` | `None` | Minimum payment amount (raw token units) |
| `max_amount` | `str \| None` | `None` | Maximum payment amount (raw token units) |
| `allowed_senders` | `list[str] \| None` | `None` | Allowlist of sender addresses |
| `blocked_senders` | `list[str] \| None` | `None` | Blocklist of sender addresses |
| `required_agent_class` | `str \| None` | `None` | Required agent class identifier |
| `min_reputation_score` | `float \| None` | `None` | Minimum reputation score (0--100) |
| `finality_depth` | `int` | `3` | Required block confirmations (1--64) |

### `Subscription`

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique subscription identifier |
| `endpoint_id` | `str` | Parent endpoint ID |
| `filters` | `SubscriptionFilter` | Event filter criteria |
| `active` | `bool` | Whether active (default `True`) |
| `created_at` | `datetime` | Creation timestamp |

### `SubscriptionFilter`

| Field | Type | Description |
|---|---|---|
| `chains` | `list[int] \| None` | Filter by chain IDs |
| `senders` | `list[str] \| None` | Filter by sender addresses |
| `recipients` | `list[str] \| None` | Filter by recipient addresses |
| `min_amount` | `str \| None` | Minimum amount filter |
| `agent_class` | `str \| None` | Filter by agent class |

### `Event`

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique event identifier |
| `type` | `WebhookEventType` | Event type (see enum values below) |
| `data` | `dict[str, Any]` | Event payload data |
| `created_at` | `str` | Creation timestamp |

### `WebhookPayload`

The full payload delivered to your webhook endpoint.

| Field | Type | Description |
|---|---|---|
| `id` | `str` | Unique delivery identifier |
| `idempotency_key` | `str` | Key for deduplication |
| `type` | `WebhookEventType` | Event type |
| `mode` | `EndpointMode` | Endpoint mode (`"execute"` or `"notify"`) |
| `timestamp` | `int` | Unix timestamp of delivery |
| `data` | `WebhookData` | Structured event data |

### `WebhookData`

| Field | Type | Description |
|---|---|---|
| `transfer` | `TransferData` | Onchain transfer details |
| `finality` | `FinalityData` | Block confirmation status |
| `identity` | `dict[str, Any] \| None` | Sender identity/reputation metadata |

### `TransferData`

| Field | Type | Description |
|---|---|---|
| `chain_id` | `int` | Chain ID of the transfer |
| `tx_hash` | `str` | Transaction hash |
| `block_number` | `int` | Block number |
| `from_address` | `str` | Sender address |
| `to_address` | `str` | Recipient address |
| `amount` | `str` | Amount in raw token units |
| `nonce` | `str` | Transaction nonce |
| `token` | `str` | Token contract address |

### `FinalityData`

| Field | Type | Description |
|---|---|---|
| `confirmations` | `int` | Current number of confirmations |
| `required_confirmations` | `int` | Required confirmations for finality |
| `is_finalized` | `bool` | Whether the transfer is finalized |

### Enums

**`EndpointMode`** -- `"execute"` | `"notify"`

**`WebhookEventType`** -- `"payment.confirmed"` | `"payment.pending"` | `"payment.pre_confirmed"` | `"payment.failed"` | `"payment.reorged"`

**`ChainId`** -- `ETHEREUM = 1` | `BASE = 8453` | `ARBITRUM = 42161`

### `PaginatedResponse`

| Field | Type | Description |
|---|---|---|
| `data` | `list[Event]` | List of events |
| `cursor` | `str \| None` | Cursor for the next page (`None` if no more pages) |
| `has_more` | `bool` | Whether more results are available |

### `Session`

| Field | Type | Description |
|---|---|---|
| `session_id` | `str` | Unique session identifier |
| `wallet_address` | `str` | Wallet that owns the session |
| `budget_total` | `int` | Total budget in smallest USDC units |
| `budget_remaining` | `int` | Remaining budget |
| `budget_currency` | `str` | Currency identifier (default `"USDC"`) |
| `expires_at` | `str` | ISO-8601 expiration timestamp |
| `ttl_seconds` | `int` | Session lifetime in seconds |
| `chain_id` | `int` | Chain ID context (default 8453) |
| `status` | `str` | `"active"`, `"expired"`, or `"closed"` |
| `reputation_score` | `float` | Cached reputation score from identity resolution |
| `agent_class` | `str` | Cached agent class from identity resolution |

## Errors

All SDK exceptions inherit from `TripWireError`, which carries `status_code` and `detail` attributes.

```
TripWireError                  # Base exception (any HTTP error)
  TripWireAuthError            # 401 / 403 -- authentication or authorization failure
  TripWireNotFoundError        # 404 -- resource not found
  TripWireRateLimitError       # 429 -- rate limit exceeded (has retry_after: float | None)
  TripWireServerError          # 5xx -- server-side error
  TripWireValidationError      # Response parsing failure (status_code=0)
  SessionError                 # Session operation failed (has session_id: str | None)
    SessionExpiredError        # Session has expired
    BudgetExhaustedError       # Session budget is exhausted
```

Additionally, `WebhookVerificationError` (not part of the `TripWireError` hierarchy) is raised by the webhook verification functions. It has a `reason` attribute with values like `"missing_signature_header"`, `"timestamp_too_old"`, or `"signature_mismatch"`.

### Catching errors

```python
from tripwire_sdk import TripWireError, TripWireRateLimitError
import asyncio

try:
    endpoint = await client.register_endpoint(...)
except TripWireRateLimitError as exc:
    if exc.retry_after:
        await asyncio.sleep(exc.retry_after)
except TripWireError as exc:
    print(f"API error {exc.status_code}: {exc.detail}")
```

## Webhook Verification

Verify incoming webhook signatures using HMAC-SHA256. The verification function checks the `X-TripWire-Signature` header, validates the timestamp is within 5 minutes, and performs constant-time signature comparison.

```python
import os
from fastapi import FastAPI, HTTPException, Request
from tripwire_sdk import (
    WebhookPayload,
    WebhookVerificationError,
    verify_webhook_signature,
)

app = FastAPI()
WEBHOOK_SECRET = os.environ["TRIPWIRE_WEBHOOK_SECRET"]

@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.body()
    headers = dict(request.headers)

    # Verify signature -- raises WebhookVerificationError on failure
    try:
        verify_webhook_signature(body, headers, WEBHOOK_SECRET)
    except WebhookVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Parse and process the event
    payload = WebhookPayload.model_validate_json(body)
    print(f"Event: {payload.type.value}, Amount: {payload.data.transfer.amount}")
    return {"status": "ok"}
```

There is also a non-raising variant:

```python
from tripwire_sdk import verify_webhook_signature_safe

if verify_webhook_signature_safe(body, headers, secret):
    # signature valid
    ...
```

For testing, you can generate signed headers locally with `sign_payload`:

```python
from tripwire_sdk import sign_payload

headers = sign_payload(b'{"test": true}', secret="your-secret")
```

## Examples

The [`examples/`](examples/) directory contains complete runnable scripts:

- **[`quickstart.py`](examples/quickstart.py)** -- Minimal 3-step example: register an endpoint, receive webhooks, verify signatures.
- **[`x402_payment.py`](examples/x402_payment.py)** -- Detailed walkthrough of the x402 payment flow, including an optional USDC balance check.
- **[`webhook_handler.py`](examples/webhook_handler.py)** -- FastAPI webhook server that verifies signatures and processes payment events.
