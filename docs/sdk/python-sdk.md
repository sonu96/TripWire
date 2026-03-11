# Python SDK Reference

The `tripwire-sdk` package provides an async Python client for the TripWire API and a webhook signature verification utility.

## Installation

```bash
# Core SDK (API client)
pip install tripwire-sdk

# With webhook verification support
pip install tripwire-sdk[webhook]
```

Dependencies:
- `httpx >= 0.28.0` -- async HTTP client
- `pydantic >= 2.10.0` -- data validation
- `httpx >= 0.28.0` -- used for Convoy REST API webhook verification (optional, installed with `[webhook]`)

## Initialization

```python
from tripwire_sdk import TripwireClient

# Using async context manager (recommended)
async with TripwireClient(api_key="tw_your_api_key") as client:
    endpoints = await client.list_endpoints()

# Custom base URL (e.g. local development)
async with TripwireClient(
    api_key="tw_your_api_key",
    base_url="http://localhost:3402",
) as client:
    ...
```

The client can also be used without a context manager. In that case, call `await client.close()` when done:

```python
client = TripwireClient(api_key="tw_your_api_key")
try:
    endpoints = await client.list_endpoints()
finally:
    await client.close()
```

## Endpoints

### Register an Endpoint

```python
from tripwire_sdk import TripwireClient
from tripwire_sdk.types import EndpointPolicies

async with TripwireClient(api_key="tw_...") as client:
    endpoint = await client.register_endpoint(
        url="https://my-app.example.com/webhook",
        mode="execute",
        chains=[8453],          # Base
        recipient="0xAbCdEf...",
    )
    print(endpoint.id)          # "ep_abc123..."
    print(endpoint.mode)        # EndpointMode.EXECUTE
    print(endpoint.active)      # True
```

With policies:

```python
    endpoint = await client.register_endpoint(
        url="https://my-app.example.com/webhook",
        mode="execute",
        chains=[8453, 42161],   # Base + Arbitrum
        recipient="0xAbCdEf...",
        policies=EndpointPolicies(
            min_amount="1000000",       # 1 USDC (6 decimals)
            finality_depth=5,
            min_reputation_score=50.0,
            allowed_senders=["0x1234...", "0x5678..."],
        ),
    )
```

You can also pass policies as a plain dict:

```python
    endpoint = await client.register_endpoint(
        url="https://my-app.example.com/webhook",
        mode="execute",
        chains=[8453],
        recipient="0xAbCdEf...",
        policies={
            "min_amount": "1000000",
            "finality_depth": 5,
        },
    )
```

### List Endpoints

```python
async with TripwireClient(api_key="tw_...") as client:
    endpoints = await client.list_endpoints()
    for ep in endpoints:
        print(f"{ep.id}: {ep.url} ({ep.mode.value})")
```

### Get Endpoint

```python
async with TripwireClient(api_key="tw_...") as client:
    endpoint = await client.get_endpoint("ep_abc123...")
    print(endpoint.url)
    print(endpoint.chains)
    print(endpoint.policies.finality_depth)
```

### Update Endpoint

Pass only the fields you want to change as keyword arguments:

```python
async with TripwireClient(api_key="tw_...") as client:
    # Update the URL
    updated = await client.update_endpoint(
        "ep_abc123...",
        url="https://new-url.example.com/webhook",
    )

    # Update chains and policies
    updated = await client.update_endpoint(
        "ep_abc123...",
        chains=[8453, 1, 42161],
        policies=EndpointPolicies(
            min_amount="5000000",   # 5 USDC
            finality_depth=10,
        ),
    )
```

### Delete Endpoint

Performs a soft-delete (sets `active=false`):

```python
async with TripwireClient(api_key="tw_...") as client:
    await client.delete_endpoint("ep_abc123...")
```

## Subscriptions

Subscriptions are for **notify-mode** endpoints. They define which events the endpoint should be notified about via Supabase Realtime.

### Create a Subscription

```python
from tripwire_sdk.types import SubscriptionFilter

async with TripwireClient(api_key="tw_...") as client:
    subscription = await client.create_subscription(
        endpoint_id="ep_abc123...",
        filters=SubscriptionFilter(
            chains=[8453],
            min_amount="500000",    # 0.5 USDC
        ),
    )
    print(subscription.id)
    print(subscription.filters.chains)
```

With a dict instead of a typed filter:

```python
    subscription = await client.create_subscription(
        endpoint_id="ep_abc123...",
        filters={
            "chains": [8453, 42161],
            "senders": ["0x1234..."],
        },
    )
```

### List Subscriptions

```python
async with TripwireClient(api_key="tw_...") as client:
    subs = await client.list_subscriptions("ep_abc123...")
    for sub in subs:
        print(f"{sub.id}: chains={sub.filters.chains}")
```

### Delete a Subscription

```python
async with TripwireClient(api_key="tw_...") as client:
    await client.delete_subscription("sub_xyz789...")
```

## Events

### List Events

Supports cursor-based pagination and filters:

```python
async with TripwireClient(api_key="tw_...") as client:
    # First page
    page = await client.list_events(limit=20)
    for event in page.data:
        print(f"{event.id}: {event.type.value} -- {event.data}")

    # Next page (if available)
    if page.has_more:
        next_page = await client.list_events(
            cursor=page.cursor,
            limit=20,
        )
```

With filters:

```python
    page = await client.list_events(
        limit=50,
        event_type="payment.confirmed",
        chain_id=8453,
    )
```

### Get Event

```python
async with TripwireClient(api_key="tw_...") as client:
    event = await client.get_event("evt_abc123...")
    print(event.type)       # WebhookEventType.PAYMENT_CONFIRMED
    print(event.data)       # {"chain_id": 8453, "tx_hash": "0x...", ...}
```

## Error Handling

All API errors raise `TripwireAPIError`:

```python
from tripwire_sdk import TripwireClient, TripwireAPIError

async with TripwireClient(api_key="tw_...") as client:
    try:
        endpoint = await client.get_endpoint("nonexistent_id")
    except TripwireAPIError as e:
        print(e.status_code)    # 404
        print(e.detail)         # "Endpoint not found"
```

Common error codes:

| Status Code | Meaning |
|-------------|---------|
| 400 | Bad request (invalid input, no fields to update) |
| 401 | Unauthorized (invalid or missing API key) |
| 404 | Resource not found |
| 422 | Validation error (Pydantic rejected the input) |
| 500 | Server error |

## Webhook Verification

See the dedicated [Webhook Verification Guide](../guides/webhook-verification.md) for full details. Quick example:

```python
from tripwire_sdk import verify_webhook_signature

is_valid = verify_webhook_signature(
    payload=request_body,
    headers={
        "X-TripWire-ID": headers["X-TripWire-ID"],
        "X-TripWire-Timestamp": headers["X-TripWire-Timestamp"],
        "X-TripWire-Signature": headers["X-TripWire-Signature"],
    },
    secret="your_hex_signing_secret",
)
```

## Types Reference

All types are importable from `tripwire_sdk.types`:

```python
from tripwire_sdk.types import (
    ChainId,                # Enum: ETHEREUM=1, BASE=8453, ARBITRUM=42161
    EndpointMode,           # Enum: NOTIFY="notify", EXECUTE="execute"
    WebhookEventType,       # Enum: PAYMENT_CONFIRMED, PAYMENT_PENDING, PAYMENT_FAILED, PAYMENT_REORGED
    EndpointPolicies,       # Pydantic model for endpoint policies
    Endpoint,               # Pydantic model for endpoint responses
    SubscriptionFilter,     # Pydantic model for subscription filters
    Subscription,           # Pydantic model for subscription responses
    Event,                  # Pydantic model for event responses
    PaginatedResponse,      # Pydantic model for paginated event lists
)
```

### ChainId

```python
from tripwire_sdk.types import ChainId

ChainId.ETHEREUM     # 1
ChainId.BASE         # 8453
ChainId.ARBITRUM     # 42161
```

### EndpointPolicies

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `min_amount` | `str \| None` | `None` | Minimum payment amount (in smallest unit, e.g. USDC = 6 decimals) |
| `max_amount` | `str \| None` | `None` | Maximum payment amount |
| `allowed_senders` | `list[str] \| None` | `None` | Allowlist of sender addresses |
| `blocked_senders` | `list[str] \| None` | `None` | Blocklist of sender addresses |
| `required_agent_class` | `str \| None` | `None` | Required ERC-8004 agent class |
| `min_reputation_score` | `float \| None` | `None` | Minimum ERC-8004 reputation score (0-100) |
| `finality_depth` | `int` | `3` | Number of block confirmations required (1-64) |

### SubscriptionFilter

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `chains` | `list[int] \| None` | `None` | Filter by chain IDs |
| `senders` | `list[str] \| None` | `None` | Filter by sender addresses |
| `recipients` | `list[str] \| None` | `None` | Filter by recipient addresses |
| `min_amount` | `str \| None` | `None` | Minimum payment amount |
| `agent_class` | `str \| None` | `None` | Filter by ERC-8004 agent class |

### WebhookEventType

| Value | Description |
|-------|-------------|
| `payment.confirmed` | Payment reached required finality depth |
| `payment.pending` | Payment detected but not yet confirmed |
| `payment.failed` | Payment failed validation or policy checks |
| `payment.reorged` | Previously confirmed payment was reorged out |
