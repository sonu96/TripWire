# Webhook Payload Reference

When a payment event is detected and passes policy evaluation, TripWire delivers a webhook to your registered endpoint URL. Webhooks are delivered via [Convoy self-hosted](https://getconvoy.io/) which handles HMAC signing, retries, and delivery tracking.

---

## Payload Format

Every webhook delivery contains a JSON body with the following structure:

```json
{
  "id": "a3f8c1d2-4e5b-6789-abcd-ef0123456789",
  "type": "payment.confirmed",
  "mode": "execute",
  "timestamp": 1710072900,
  "data": {
    "transfer": {
      "chain_id": 8453,
      "tx_hash": "0x1a2b3c4d5e6f7890abcdef1234567890abcdef1234567890abcdef1234567890",
      "block_number": 12345678,
      "from_address": "0x1234567890abcdef1234567890abcdef12345678",
      "to_address": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18",
      "amount": "5000000",
      "nonce": "0xabcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890",
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
      "deployer": "0xDeployerAddress1234567890abcdef12345678",
      "capabilities": ["pay", "transfer"],
      "reputation_score": 85.5,
      "registered_at": 1709900000,
      "metadata": {}
    }
  }
}
```

### Top-Level Fields

| Field       | Type   | Description                                                             |
|-------------|--------|-------------------------------------------------------------------------|
| `id`        | string | Unique event ID (UUID v4)                                               |
| `type`      | string | Event type (see [Event Types](#event-types) below)                      |
| `mode`      | string | Endpoint mode: `"notify"` or `"execute"`                                |
| `timestamp` | int    | Unix timestamp (seconds) when the event was created                     |
| `data`      | object | Event data containing `transfer`, and optionally `finality` and `identity` |

### Transfer Data

Present in every webhook payload. Contains the ERC-3009 `transferWithAuthorization` details.

| Field          | Type   | Description                                                        |
|----------------|--------|--------------------------------------------------------------------|
| `chain_id`     | int    | Chain ID where the transfer occurred (1, 8453, 42161)              |
| `tx_hash`      | string | Transaction hash                                                   |
| `block_number` | int    | Block number containing the transaction                            |
| `from_address` | string | Sender address (the authorizer of the ERC-3009 transfer)           |
| `to_address`   | string | Recipient address                                                  |
| `amount`       | string | Transfer amount in smallest unit (USDC 6 decimals). `"5000000"` = 5 USDC |
| `nonce`        | string | ERC-3009 authorization nonce (bytes32 hex)                         |
| `token`        | string | Token contract address (USDC)                                      |

### Finality Data

Included when finality tracking information is available.

| Field                     | Type    | Description                                        |
|---------------------------|---------|----------------------------------------------------|
| `confirmations`           | int     | Current number of block confirmations              |
| `required_confirmations`  | int     | Required confirmations for this chain/endpoint     |
| `is_finalized`            | boolean | Whether the transaction has reached finality       |

### Identity Data

Included when the sender has a registered ERC-8004 onchain agent identity.

| Field              | Type           | Description                                          |
|--------------------|----------------|------------------------------------------------------|
| `address`          | string         | Agent's Ethereum address                             |
| `agent_class`      | string         | Agent classification (e.g., `"payment-agent"`)       |
| `deployer`         | string         | Address that deployed/registered the agent           |
| `capabilities`     | array of string| Agent's declared capabilities                        |
| `reputation_score` | float          | Reputation score (0-100)                             |
| `registered_at`    | int            | Unix timestamp when the agent was registered         |
| `metadata`         | object         | Additional agent metadata                            |

---

## Event Types

| Event Type          | Description                                                                                      |
|---------------------|--------------------------------------------------------------------------------------------------|
| `payment.pending`   | Transfer detected on-chain but has not yet reached the required finality depth                    |
| `payment.confirmed` | Transfer has reached the required number of block confirmations and is considered final           |
| `payment.failed`    | Transfer failed validation (policy rejection, invalid authorization, etc.)                        |
| `payment.reorged`   | A previously confirmed transfer was removed due to a chain reorganization                         |

### Event Lifecycle

A typical successful payment flows through these events:

1. **`payment.pending`** -- Transfer detected in a new block, confirmations still accumulating
2. **`payment.confirmed`** -- Required confirmations reached, safe to fulfill the order

If something goes wrong:

- **`payment.failed`** -- The transfer did not pass policy checks or was otherwise invalid
- **`payment.reorged`** -- A previously confirmed block was reorganized out of the canonical chain (rare but critical to handle)

---

## Webhook Headers

Every webhook delivery includes these HTTP headers:

| Header                | Description                                                      |
|-----------------------|------------------------------------------------------------------|
| `Content-Type`        | `application/json`                                               |
| `Tripwire-Event-Id`   | Unique event ID (matches the `id` field in the payload)          |
| `Tripwire-Timestamp`  | Unix timestamp of the delivery attempt                           |
| `Tripwire-Signature`  | HMAC signature for verifying authenticity (Convoy format)        |
| `X-TripWire-ID`       | Convoy message ID                                                |
| `X-TripWire-Timestamp` | Convoy delivery timestamp                                       |
| `X-TripWire-Signature` | Convoy HMAC-SHA256 signature (used for verification)            |

---

## Signature Verification

TripWire uses Convoy self-hosted for webhook delivery, which signs every payload with HMAC-SHA256. You should always verify webhook signatures to ensure payloads are authentic and have not been tampered with.

### Signature Format

Convoy uses a hex signing secret that is assigned to each endpoint. The signature is computed as:

```
HMAC-SHA256(
  key = hex_decode(secret),
  message = "{X-TripWire-ID}.{X-TripWire-Timestamp}.{body}"
)
```

The `X-TripWire-Signature` header contains one or more signatures in the format:

```
v1,{base64_encoded_signature}
```

Multiple signatures may be present (comma-separated) during key rotation.

### Verification with the Convoy REST API via httpx (Recommended)

The simplest way to verify webhooks is using the Convoy REST API via httpx, which TripWire wraps for convenience.

**Using `tripwire-sdk`**

```python
from tripwire_sdk.verify import verify_webhook

# In your webhook handler (e.g., FastAPI, Flask, Django)
is_valid = verify_webhook(
    payload=request.body,       # raw request body (str or bytes)
    headers=dict(request.headers),
    secret="your_hex_endpoint_secret",
)

if not is_valid:
    return Response(status_code=401)
```

**Using Convoy REST API via httpx directly**

```python
import hashlib
import hmac

def handle_webhook(request):
    payload = request.body
    headers = dict(request.headers)
    secret = "your_hex_endpoint_secret"

    msg_id = headers.get("X-TripWire-ID", "")
    timestamp = headers.get("X-TripWire-Timestamp", "")
    signature = headers.get("X-TripWire-Signature", "")

    signed_content = f"{msg_id}.{timestamp}.{payload}"
    expected = hmac.new(
        bytes.fromhex(secret),
        signed_content.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    import base64
    expected_b64 = base64.b64encode(expected).decode("utf-8")

    if not any(
        hmac.compare_digest(expected_b64, sig.removeprefix("v1,"))
        for sig in signature.split(" ")
    ):
        return Response(status_code=401)

    # Signature is valid -- process the event
    event = json.loads(payload)
    print(f"Received {event['type']} event: {event['id']}")
```

### Manual Verification (Without Library)

If you cannot use the Convoy REST API via httpx, you can verify manually:

```python
import base64
import hashlib
import hmac
import time

def verify_webhook_manual(payload: str, headers: dict, secret: str) -> bool:
    """Manually verify a TripWire webhook signature.

    Args:
        payload: Raw request body as string.
        headers: Request headers dict.
        secret: Endpoint signing secret (hex string).
    """
    msg_id = headers.get("X-TripWire-ID")
    timestamp = headers.get("X-TripWire-Timestamp")
    signature = headers.get("X-TripWire-Signature")

    if not all([msg_id, timestamp, signature]):
        return False

    # Reject timestamps older than 5 minutes to prevent replay attacks
    try:
        ts = int(timestamp)
        if abs(time.time() - ts) > 300:
            return False
    except ValueError:
        return False

    # Decode the signing secret (hex-encoded)
    secret_bytes = bytes.fromhex(secret)

    # Construct the signed content
    signed_content = f"{msg_id}.{timestamp}.{payload}"

    # Compute HMAC-SHA256
    expected = hmac.new(
        secret_bytes,
        signed_content.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_b64 = base64.b64encode(expected).decode("utf-8")

    # Compare against each signature in the header (supports key rotation)
    for sig in signature.split(" "):
        sig_value = sig.removeprefix("v1,")
        if hmac.compare_digest(expected_b64, sig_value):
            return True

    return False
```

### Framework Examples

**FastAPI**

```python
from fastapi import FastAPI, Request, HTTPException
import hashlib, hmac, base64, time, json

app = FastAPI()
WEBHOOK_SECRET = "your_hex_endpoint_secret"

@app.post("/webhooks/tripwire")
async def handle_tripwire_webhook(request: Request):
    payload = await request.body()
    headers = dict(request.headers)

    msg_id = headers.get("x-tripwire-id", "")
    timestamp = headers.get("x-tripwire-timestamp", "")
    signature = headers.get("x-tripwire-signature", "")
    signed = f"{msg_id}.{timestamp}.{payload.decode()}"
    expected = base64.b64encode(
        hmac.new(bytes.fromhex(WEBHOOK_SECRET), signed.encode(), hashlib.sha256).digest()
    ).decode()
    if not any(hmac.compare_digest(expected, s.removeprefix("v1,")) for s in signature.split(" ")):
        raise HTTPException(status_code=401, detail="Invalid signature")

    event = json.loads(payload)

    match event["type"]:
        case "payment.confirmed":
            transfer = event["data"]["transfer"]
            print(f"Payment confirmed: {transfer['amount']} from {transfer['from_address']}")
            # Fulfill the order
        case "payment.pending":
            print(f"Payment pending, waiting for confirmations...")
        case "payment.failed":
            print(f"Payment failed")
        case "payment.reorged":
            print(f"Payment reorged -- reverse fulfillment!")

    return {"status": "ok"}
```

**Flask**

```python
from flask import Flask, request, jsonify
import hashlib, hmac, base64, json

app = Flask(__name__)
WEBHOOK_SECRET = "your_hex_endpoint_secret"

@app.route("/webhooks/tripwire", methods=["POST"])
def handle_tripwire_webhook():
    payload = request.get_data(as_text=True)
    headers = dict(request.headers)

    msg_id = headers.get("X-TripWire-ID", "")
    timestamp = headers.get("X-TripWire-Timestamp", "")
    signature = headers.get("X-TripWire-Signature", "")
    signed = f"{msg_id}.{timestamp}.{payload}"
    expected = base64.b64encode(
        hmac.new(bytes.fromhex(WEBHOOK_SECRET), signed.encode(), hashlib.sha256).digest()
    ).decode()
    if not any(hmac.compare_digest(expected, s.removeprefix("v1,")) for s in signature.split(" ")):
        return jsonify({"error": "Invalid signature"}), 401

    event = json.loads(payload)

    if event["type"] == "payment.confirmed":
        transfer = event["data"]["transfer"]
        print(f"Payment confirmed: {transfer['amount']} from {transfer['from_address']}")

    return jsonify({"status": "ok"}), 200
```

---

## Retry Schedule

When your endpoint returns a non-2xx status code (or is unreachable), Convoy automatically retries delivery with exponential backoff. You do not need to configure retries -- they happen automatically.

| Attempt | Delay After Previous |
|---------|---------------------|
| 1       | Immediately         |
| 2       | 5 seconds           |
| 3       | 5 minutes           |
| 4       | 30 minutes          |
| 5       | 2 hours             |
| 6       | 5 hours             |
| 7       | 10 hours            |
| 8       | 10 hours            |

After 8 failed attempts, the message is sent to the dead letter queue (DLQ). Failed messages can be manually retried via the Convoy dashboard or API.

### Best Practices for Webhook Handlers

- **Return 200 quickly.** Process the webhook asynchronously if your handler takes more than a few seconds. Convoy handles retries if your endpoint times out.
- **Handle duplicates.** Use the event `id` field for idempotency. The same event may be delivered more than once due to retries.
- **Verify signatures.** Always verify the `X-TripWire-Signature` header before processing. Never trust unverified payloads.
- **Handle `payment.reorged`.** If you fulfill orders on `payment.confirmed`, you must reverse them on `payment.reorged`. Chain reorganizations are rare but do happen.
- **Log the event ID.** Store the `id` from each webhook for debugging and support requests.

---

## Testing Webhooks Locally

Use a tunneling service to expose your local server for development:

```bash
# Using ngrok
ngrok http 8000

# Register the tunnel URL as your endpoint
curl -X POST https://api.tripwire.dev/v1/endpoints \
  -H "Authorization: Bearer $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://abc123.ngrok.io/webhooks/tripwire",
    "mode": "execute",
    "chains": [8453],
    "recipient": "0x742d35Cc6634C0532925a3b844Bc9e7595f2bD18"
  }'
```
