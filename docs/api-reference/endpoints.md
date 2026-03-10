# TripWire REST API Reference

Base URL: `https://api.tripwire.dev` (production) or `http://localhost:8000` (local)

All endpoints under `/v1/` require authentication via API key.

## Authentication

Include your API key in the `Authorization` header:

```
Authorization: Bearer {api_key}
```

---

## Health Check

### `GET /health`

Returns service health status. No authentication required.

**Response** `200 OK`

```json
{
  "status": "ok",
  "service": "tripwire"
}
```

**cURL**

```bash
curl https://api.tripwire.dev/health
```

---

## Endpoints

### Register Endpoint

`POST /v1/endpoints`

Register a new webhook endpoint to receive payment notifications.

**Headers**

| Header          | Value                  | Required |
|-----------------|------------------------|----------|
| Authorization   | `Bearer {api_key}`     | Yes      |
| Content-Type    | `application/json`     | Yes      |

**Request Body**

| Field       | Type             | Required | Description                                                                 |
|-------------|------------------|----------|-----------------------------------------------------------------------------|
| `url`       | string           | Yes      | The URL where webhooks will be delivered                                    |
| `mode`      | string           | Yes      | `"notify"` (Supabase Realtime push) or `"execute"` (Svix webhook delivery) |
| `chains`    | array of int     | Yes      | Chain IDs to monitor. At least one required. Values: `1` (Ethereum), `8453` (Base), `42161` (Arbitrum) |
| `recipient` | string           | Yes      | Ethereum address (0x-prefixed, 40 hex chars) to match incoming transfers   |
| `policies`  | object or null   | No       | Optional policy rules (see [Policies](#endpoint-policies) below)           |

**Request Example**

```json
{
  "url": "https://myapp.com/webhooks/payments",
  "mode": "notify",
  "chains": [8453],
  "recipient": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18",
  "policies": {
    "min_amount": "1000000",
    "max_amount": "100000000",
    "allowed_senders": ["0xAbC1230000000000000000000000000000000001"],
    "finality_depth": 3
  }
}
```

**Response** `201 Created`

```json
{
  "id": "ep_a1b2c3d4e5f6g7h8i9j0k",
  "url": "https://myapp.com/webhooks/payments",
  "mode": "notify",
  "chains": [8453],
  "recipient": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18",
  "policies": {
    "min_amount": "1000000",
    "max_amount": "100000000",
    "allowed_senders": ["0xAbC1230000000000000000000000000000000001"],
    "blocked_senders": null,
    "required_agent_class": null,
    "min_reputation_score": null,
    "finality_depth": 3
  },
  "active": true,
  "created_at": "2026-03-10T12:00:00+00:00",
  "updated_at": "2026-03-10T12:00:00+00:00"
}
```

**Error Responses**

| Status | Description                                           |
|--------|-------------------------------------------------------|
| 400    | Invalid request body (missing fields, bad format)     |
| 422    | Validation error (invalid chain ID, bad address, etc) |

**cURL**

```bash
curl -X POST https://api.tripwire.dev/v1/endpoints \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://myapp.com/webhooks/payments",
    "mode": "notify",
    "chains": [8453],
    "recipient": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
  }'
```

---

### List Endpoints

`GET /v1/endpoints`

List all active registered endpoints.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |

**Response** `200 OK`

```json
{
  "data": [
    {
      "id": "ep_a1b2c3d4e5f6g7h8i9j0k",
      "url": "https://myapp.com/webhooks/payments",
      "mode": "notify",
      "chains": [8453],
      "recipient": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18",
      "policies": {
        "min_amount": null,
        "max_amount": null,
        "allowed_senders": null,
        "blocked_senders": null,
        "required_agent_class": null,
        "min_reputation_score": null,
        "finality_depth": 3
      },
      "active": true,
      "created_at": "2026-03-10T12:00:00+00:00",
      "updated_at": "2026-03-10T12:00:00+00:00"
    }
  ],
  "count": 1
}
```

**cURL**

```bash
curl https://api.tripwire.dev/v1/endpoints \
  -H "Authorization: Bearer $API_KEY"
```

---

### Get Endpoint

`GET /v1/endpoints/{endpoint_id}`

Retrieve details for a single endpoint.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |

**Path Parameters**

| Parameter     | Type   | Description            |
|---------------|--------|------------------------|
| `endpoint_id` | string | The endpoint's unique ID |

**Response** `200 OK`

```json
{
  "id": "ep_a1b2c3d4e5f6g7h8i9j0k",
  "url": "https://myapp.com/webhooks/payments",
  "mode": "notify",
  "chains": [8453],
  "recipient": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18",
  "policies": {
    "min_amount": null,
    "max_amount": null,
    "allowed_senders": null,
    "blocked_senders": null,
    "required_agent_class": null,
    "min_reputation_score": null,
    "finality_depth": 3
  },
  "active": true,
  "created_at": "2026-03-10T12:00:00+00:00",
  "updated_at": "2026-03-10T12:00:00+00:00"
}
```

**Error Responses**

| Status | Description        |
|--------|--------------------|
| 404    | Endpoint not found |

**cURL**

```bash
curl https://api.tripwire.dev/v1/endpoints/ep_a1b2c3d4e5f6g7h8i9j0k \
  -H "Authorization: Bearer $API_KEY"
```

---

### Update Endpoint

`PATCH /v1/endpoints/{endpoint_id}`

Update one or more fields on an existing endpoint. Only include the fields you want to change.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |
| Content-Type  | `application/json` | Yes      |

**Path Parameters**

| Parameter     | Type   | Description            |
|---------------|--------|------------------------|
| `endpoint_id` | string | The endpoint's unique ID |

**Request Body**

All fields are optional. Only provided fields are updated.

| Field      | Type           | Description                                               |
|------------|----------------|-----------------------------------------------------------|
| `url`      | string         | New webhook delivery URL                                  |
| `mode`     | string         | `"notify"` or `"execute"`                                 |
| `chains`   | array of int   | New chain IDs to monitor                                  |
| `policies` | object         | New policy rules (replaces entire policies object)        |
| `active`   | boolean        | Set to `false` to deactivate, `true` to reactivate        |

**Request Example**

```json
{
  "url": "https://myapp.com/webhooks/v2/payments",
  "policies": {
    "min_amount": "5000000",
    "finality_depth": 6
  }
}
```

**Response** `200 OK`

Returns the full updated endpoint object (same shape as [Get Endpoint](#get-endpoint)).

**Error Responses**

| Status | Description                    |
|--------|--------------------------------|
| 400    | No fields to update (empty body) |
| 404    | Endpoint not found             |
| 422    | Validation error               |

**cURL**

```bash
curl -X PATCH https://api.tripwire.dev/v1/endpoints/ep_a1b2c3d4e5f6g7h8i9j0k \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://myapp.com/webhooks/v2/payments"
  }'
```

---

### Deactivate Endpoint

`DELETE /v1/endpoints/{endpoint_id}`

Soft-delete an endpoint by setting `active` to `false`. The endpoint data is retained but it will no longer receive webhooks or appear in list results.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |

**Path Parameters**

| Parameter     | Type   | Description            |
|---------------|--------|------------------------|
| `endpoint_id` | string | The endpoint's unique ID |

**Response** `204 No Content`

No response body.

**Error Responses**

| Status | Description        |
|--------|--------------------|
| 404    | Endpoint not found |

**cURL**

```bash
curl -X DELETE https://api.tripwire.dev/v1/endpoints/ep_a1b2c3d4e5f6g7h8i9j0k \
  -H "Authorization: Bearer $API_KEY"
```

---

## Subscriptions

Subscriptions allow notify-mode endpoints to filter which payments they receive. Only endpoints with `mode: "notify"` can have subscriptions.

### Create Subscription

`POST /v1/endpoints/{endpoint_id}/subscriptions`

Create a subscription with filters for a notify-mode endpoint.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |
| Content-Type  | `application/json` | Yes      |

**Path Parameters**

| Parameter     | Type   | Description            |
|---------------|--------|------------------------|
| `endpoint_id` | string | The endpoint's unique ID |

**Request Body**

| Field     | Type   | Required | Description         |
|-----------|--------|----------|---------------------|
| `filters` | object | Yes      | Subscription filters |

**Subscription Filters**

All filter fields are optional. When multiple filters are specified, all must match (AND logic).

| Field         | Type           | Description                                                    |
|---------------|----------------|----------------------------------------------------------------|
| `chains`      | array of int   | Only deliver events for these chain IDs                        |
| `senders`     | array of string| Only deliver events from these sender addresses (case-insensitive) |
| `recipients`  | array of string| Only deliver events to these recipient addresses (case-insensitive) |
| `min_amount`  | string         | Minimum transfer amount in smallest unit (USDC 6 decimals)    |
| `agent_class` | string         | Only deliver events from agents with this ERC-8004 agent class |

**Request Example**

```json
{
  "filters": {
    "chains": [8453],
    "senders": ["0xAbC1230000000000000000000000000000000001"],
    "min_amount": "1000000"
  }
}
```

**Response** `201 Created`

```json
{
  "id": "sub_x1y2z3a4b5c6d7e8f9g0h",
  "endpoint_id": "ep_a1b2c3d4e5f6g7h8i9j0k",
  "filters": {
    "chains": [8453],
    "senders": ["0xAbC1230000000000000000000000000000000001"],
    "recipients": null,
    "min_amount": "1000000",
    "agent_class": null
  },
  "active": true,
  "created_at": "2026-03-10T12:30:00+00:00"
}
```

**Error Responses**

| Status | Description                                               |
|--------|-----------------------------------------------------------|
| 400    | Endpoint is not in notify mode                            |
| 404    | Endpoint not found or inactive                            |
| 422    | Validation error                                          |

**cURL**

```bash
curl -X POST https://api.tripwire.dev/v1/endpoints/ep_a1b2c3d4e5f6g7h8i9j0k/subscriptions \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "filters": {
      "chains": [8453],
      "min_amount": "1000000"
    }
  }'
```

---

### List Subscriptions

`GET /v1/endpoints/{endpoint_id}/subscriptions`

List all active subscriptions for an endpoint.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |

**Path Parameters**

| Parameter     | Type   | Description            |
|---------------|--------|------------------------|
| `endpoint_id` | string | The endpoint's unique ID |

**Response** `200 OK`

```json
[
  {
    "id": "sub_x1y2z3a4b5c6d7e8f9g0h",
    "endpoint_id": "ep_a1b2c3d4e5f6g7h8i9j0k",
    "filters": {
      "chains": [8453],
      "senders": null,
      "recipients": null,
      "min_amount": "1000000",
      "agent_class": null
    },
    "active": true,
    "created_at": "2026-03-10T12:30:00+00:00"
  }
]
```

**Error Responses**

| Status | Description        |
|--------|--------------------|
| 404    | Endpoint not found |

**cURL**

```bash
curl https://api.tripwire.dev/v1/endpoints/ep_a1b2c3d4e5f6g7h8i9j0k/subscriptions \
  -H "Authorization: Bearer $API_KEY"
```

---

### Delete Subscription

`DELETE /v1/subscriptions/{subscription_id}`

Deactivate a subscription. The subscription data is retained but no longer used for filtering.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |

**Path Parameters**

| Parameter         | Type   | Description                |
|-------------------|--------|----------------------------|
| `subscription_id` | string | The subscription's unique ID |

**Response** `204 No Content`

No response body.

**Error Responses**

| Status | Description            |
|--------|------------------------|
| 404    | Subscription not found |

**cURL**

```bash
curl -X DELETE https://api.tripwire.dev/v1/subscriptions/sub_x1y2z3a4b5c6d7e8f9g0h \
  -H "Authorization: Bearer $API_KEY"
```

---

## Events

### List Events

`GET /v1/events`

List events with cursor-based pagination and optional filters. Events are returned in reverse chronological order (newest first).

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |

**Query Parameters**

| Parameter    | Type   | Default | Description                                                            |
|--------------|--------|---------|------------------------------------------------------------------------|
| `cursor`     | string | null    | Event ID to use as pagination cursor. Returns events created before this event. |
| `limit`      | int    | 50      | Number of events to return. Min: 1, Max: 200.                         |
| `event_type` | string | null    | Filter by event type: `payment.confirmed`, `payment.pending`, `payment.failed`, `payment.reorged` |
| `chain_id`   | int    | null    | Filter by chain ID (1, 8453, 42161)                                   |

**Response** `200 OK`

```json
{
  "data": [
    {
      "id": "evt_m1n2o3p4q5r6s7t8u9v0w",
      "endpoint_id": "ep_a1b2c3d4e5f6g7h8i9j0k",
      "type": "payment.confirmed",
      "data": {
        "transfer": {
          "chain_id": 8453,
          "tx_hash": "0xabc123...",
          "block_number": 12345678,
          "from_address": "0xSender...",
          "to_address": "0xRecipient...",
          "amount": "5000000",
          "nonce": "0xnonce...",
          "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
        },
        "finality": {
          "confirmations": 3,
          "required_confirmations": 3,
          "is_finalized": true
        }
      },
      "created_at": "2026-03-10T12:15:00+00:00"
    }
  ],
  "cursor": "evt_x1y2z3a4b5c6d7e8f9g0h",
  "has_more": true
}
```

**Pagination**

To paginate through results, pass the `cursor` value from the response as the `cursor` query parameter in your next request. When `has_more` is `false`, you have reached the end of the results.

```bash
# First page
curl "https://api.tripwire.dev/v1/events?limit=10" \
  -H "Authorization: Bearer $API_KEY"

# Next page
curl "https://api.tripwire.dev/v1/events?limit=10&cursor=evt_x1y2z3a4b5c6d7e8f9g0h" \
  -H "Authorization: Bearer $API_KEY"
```

**cURL**

```bash
curl "https://api.tripwire.dev/v1/events?event_type=payment.confirmed&chain_id=8453&limit=20" \
  -H "Authorization: Bearer $API_KEY"
```

---

### Get Event

`GET /v1/events/{event_id}`

Retrieve details for a single event.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |

**Path Parameters**

| Parameter  | Type   | Description          |
|------------|--------|----------------------|
| `event_id` | string | The event's unique ID |

**Response** `200 OK`

```json
{
  "id": "evt_m1n2o3p4q5r6s7t8u9v0w",
  "endpoint_id": "ep_a1b2c3d4e5f6g7h8i9j0k",
  "type": "payment.confirmed",
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0xabc123def456789...",
      "block_number": 12345678,
      "from_address": "0x1234567890abcdef1234567890abcdef12345678",
      "to_address": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18",
      "amount": "5000000",
      "nonce": "0xnonce123...",
      "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
    },
    "finality": {
      "confirmations": 3,
      "required_confirmations": 3,
      "is_finalized": true
    },
    "identity": {
      "address": "0x1234567890abcdef1234567890abcdef12345678",
      "agent_class": "payment-agent",
      "deployer": "0xDeployer...",
      "capabilities": ["pay", "transfer"],
      "reputation_score": 85.5,
      "registered_at": 1710000000,
      "metadata": {}
    }
  },
  "created_at": "2026-03-10T12:15:00+00:00"
}
```

**Error Responses**

| Status | Description     |
|--------|-----------------|
| 404    | Event not found |

**cURL**

```bash
curl https://api.tripwire.dev/v1/events/evt_m1n2o3p4q5r6s7t8u9v0w \
  -H "Authorization: Bearer $API_KEY"
```

---

### List Endpoint Events

`GET /v1/endpoints/{endpoint_id}/events`

List events for a specific endpoint. Uses the same cursor-based pagination as the global events list.

**Headers**

| Header        | Value              | Required |
|---------------|--------------------|----------|
| Authorization | `Bearer {api_key}` | Yes      |

**Path Parameters**

| Parameter     | Type   | Description            |
|---------------|--------|------------------------|
| `endpoint_id` | string | The endpoint's unique ID |

**Query Parameters**

| Parameter | Type   | Default | Description                                                            |
|-----------|--------|---------|------------------------------------------------------------------------|
| `cursor`  | string | null    | Event ID to use as pagination cursor                                   |
| `limit`   | int    | 50      | Number of events to return. Min: 1, Max: 200.                         |

**Response** `200 OK`

Same shape as [List Events](#list-events).

**Error Responses**

| Status | Description        |
|--------|--------------------|
| 404    | Endpoint not found |

**cURL**

```bash
curl "https://api.tripwire.dev/v1/endpoints/ep_a1b2c3d4e5f6g7h8i9j0k/events?limit=25" \
  -H "Authorization: Bearer $API_KEY"
```

---

## Endpoint Policies

Policies control which payments an endpoint will accept. They are evaluated during the dispatch pipeline before a webhook is sent.

| Field                   | Type             | Default | Description                                                     |
|-------------------------|------------------|---------|-----------------------------------------------------------------|
| `min_amount`            | string or null   | null    | Minimum transfer amount in smallest unit (USDC 6 decimals). `"1000000"` = 1 USDC. |
| `max_amount`            | string or null   | null    | Maximum transfer amount in smallest unit                        |
| `allowed_senders`       | array or null    | null    | Whitelist of sender addresses. If set, only these senders trigger webhooks. |
| `blocked_senders`       | array or null    | null    | Blacklist of sender addresses. If set, these senders are rejected. |
| `required_agent_class`  | string or null   | null    | Require the sender to have this ERC-8004 agent class            |
| `min_reputation_score`  | float or null    | null    | Minimum ERC-8004 reputation score (0-100) required              |
| `finality_depth`        | int              | 3       | Number of block confirmations required before dispatching (1-64) |

**Amount Format**

All amounts use USDC's 6-decimal representation as strings:

| Human-readable | Raw value    |
|----------------|-------------|
| 0.01 USDC      | `"10000"`   |
| 1 USDC         | `"1000000"` |
| 100 USDC       | `"100000000"` |

---

## Supported Chains

| Chain     | Chain ID | Finality Depth (default) | USDC Contract                                |
|-----------|----------|--------------------------|----------------------------------------------|
| Ethereum  | 1        | 12 blocks                | `0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48` |
| Base      | 8453     | 3 blocks                 | `0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913` |
| Arbitrum  | 42161    | 1 block                  | `0xaf88d065e77c8cC2239327C5EDb3A432268e5831` |

---

## Error Format

All error responses follow this structure:

```json
{
  "detail": "Human-readable error message"
}
```

For validation errors (422), FastAPI returns detailed field-level errors:

```json
{
  "detail": [
    {
      "loc": ["body", "recipient"],
      "msg": "String should match pattern '^0x[a-fA-F0-9]{40}$'",
      "type": "string_pattern_mismatch"
    }
  ]
}
```

## Rate Limits

Rate limits are enforced per API key. Contact the TripWire team for rate limit details and enterprise tier options.
