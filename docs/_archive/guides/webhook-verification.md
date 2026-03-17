# Webhook Verification

When TripWire delivers a webhook to your endpoint (execute mode), the payload is signed using HMAC via [Convoy self-hosted](https://getconvoy.io). You should always verify the signature before processing the payload to ensure it was sent by TripWire and has not been tampered with.

## Why Verification Matters

Without signature verification, an attacker could send forged webhook payloads to your endpoint, potentially triggering unauthorized business logic (e.g. granting access, delivering goods, or crediting accounts for payments that never happened).

Every webhook from TripWire includes three headers:

| Header | Description |
|--------|-------------|
| `X-TripWire-ID` | Unique message identifier |
| `X-TripWire-Timestamp` | Unix timestamp (seconds) when the message was sent |
| `X-TripWire-Signature` | HMAC-SHA256 signature of the payload |

## Using the TripWire SDK

The simplest way to verify webhooks. Install with the `[webhook]` extra:

```bash
pip install tripwire-sdk[webhook]
```

### Basic Verification

```python
from tripwire_sdk import verify_webhook_signature

is_valid = verify_webhook_signature(
    payload=raw_body,       # Raw request body (str or bytes)
    headers={
        "X-TripWire-ID": "msg_abc123...",
        "X-TripWire-Timestamp": "1700000000",
        "X-TripWire-Signature": "v1,base64signature...",
    },
    secret="your_hex_signing_secret",
)

if not is_valid:
    # Reject the request
    ...
```

### FastAPI Webhook Handler

```python
import json

from fastapi import FastAPI, Request, HTTPException
from tripwire_sdk import verify_webhook_signature

app = FastAPI()

WEBHOOK_SECRET = "your_hex_signing_secret"


@app.post("/webhook")
async def handle_webhook(request: Request):
    # 1. Read the raw body
    body = await request.body()

    # 2. Extract Convoy headers
    headers = {
        "X-TripWire-ID": request.headers.get("X-TripWire-ID", ""),
        "X-TripWire-Timestamp": request.headers.get("X-TripWire-Timestamp", ""),
        "X-TripWire-Signature": request.headers.get("X-TripWire-Signature", ""),
    }

    # 3. Verify signature
    if not verify_webhook_signature(body, headers, WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # 4. Parse and process the event
    event = json.loads(body)
    event_type = event.get("type")

    if event_type == "payment.confirmed":
        tx_hash = event["data"]["tx_hash"]
        amount = event["data"]["amount"]
        sender = event["data"]["from_address"]
        print(f"Payment confirmed: {amount} from {sender} (tx: {tx_hash})")
        # Execute your business logic here

    elif event_type == "payment.pending":
        print(f"Payment pending: {event['data']['tx_hash']}")

    elif event_type == "payment.failed":
        print(f"Payment failed: {event['data']}")

    elif event_type == "payment.reorged":
        print(f"Payment reorged -- roll back: {event['data']['tx_hash']}")
        # Reverse any actions taken for this payment

    return {"status": "ok"}
```

### Flask Webhook Handler

```python
import json

from flask import Flask, request, abort
from tripwire_sdk import verify_webhook_signature

app = Flask(__name__)

WEBHOOK_SECRET = "your_hex_signing_secret"


@app.route("/webhook", methods=["POST"])
def handle_webhook():
    # 1. Read the raw body
    body = request.get_data(as_text=True)

    # 2. Extract Convoy headers
    headers = {
        "X-TripWire-ID": request.headers.get("X-TripWire-ID", ""),
        "X-TripWire-Timestamp": request.headers.get("X-TripWire-Timestamp", ""),
        "X-TripWire-Signature": request.headers.get("X-TripWire-Signature", ""),
    }

    # 3. Verify signature
    if not verify_webhook_signature(body, headers, WEBHOOK_SECRET):
        abort(401, description="Invalid webhook signature")

    # 4. Parse and process the event
    event = json.loads(body)
    event_type = event.get("type")

    if event_type == "payment.confirmed":
        tx_hash = event["data"]["tx_hash"]
        amount = event["data"]["amount"]
        sender = event["data"]["from_address"]
        print(f"Payment confirmed: {amount} from {sender} (tx: {tx_hash})")

    elif event_type == "payment.reorged":
        print(f"Payment reorged -- roll back: {event['data']['tx_hash']}")

    return {"status": "ok"}, 200
```

## Manual Verification (Without SDK)

If you cannot use the `tripwire-sdk` package, you can verify the signature manually. TripWire uses the [Convoy standard webhook signing scheme](https://docs.getconvoy.io/product-manual/signatures):

```python
import base64
import hashlib
import hmac
import time


def verify_webhook_manual(
    payload: str,
    headers: dict[str, str],
    secret: str,
    tolerance_seconds: int = 300,
) -> bool:
    """Verify a TripWire/Convoy webhook signature manually.

    Args:
        payload: Raw request body as string.
        headers: Dict with X-TripWire-ID, X-TripWire-Timestamp, X-TripWire-Signature.
        secret: Signing secret (hex string).
        tolerance_seconds: Max age of the timestamp (default 5 minutes).

    Returns:
        True if valid, False otherwise.
    """
    msg_id = headers.get("X-TripWire-ID", "")
    timestamp = headers.get("X-TripWire-Timestamp", "")
    signature = headers.get("X-TripWire-Signature", "")

    if not msg_id or not timestamp or not signature:
        return False

    # Check timestamp tolerance (replay protection)
    try:
        ts = int(timestamp)
    except ValueError:
        return False

    now = int(time.time())
    if abs(now - ts) > tolerance_seconds:
        return False

    # Decode the secret (hex-encoded)
    secret_bytes = bytes.fromhex(secret)

    # Build the signed content: "{msg_id}.{timestamp}.{payload}"
    to_sign = f"{msg_id}.{timestamp}.{payload}"

    # Compute HMAC-SHA256
    expected = hmac.new(
        secret_bytes,
        to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    expected_b64 = base64.b64encode(expected).decode("utf-8")

    # The signature header can contain multiple signatures separated by spaces
    # Each is prefixed with "v1,"
    for sig in signature.split(" "):
        sig_value = sig.removeprefix("v1,")
        if hmac.compare_digest(expected_b64, sig_value):
            return True

    return False
```

## Timestamp Tolerance and Replay Protection

The `X-TripWire-Timestamp` header contains the Unix timestamp (in seconds) when the webhook was sent. You should reject webhooks with timestamps that are too old or too far in the future.

**Default tolerance**: 5 minutes (300 seconds)

The SDK's `verify_webhook_signature` function handles this automatically via the Convoy REST API via httpx. If you verify manually, enforce the tolerance yourself as shown in the manual verification example above.

### Why Replay Protection Matters

Without timestamp checking, an attacker who captures a valid webhook payload could replay it later. Even though the HMAC signature would still be valid, the timestamp check prevents this by rejecting stale messages.

**Best practices:**

- Always verify the timestamp is within tolerance before accepting a webhook
- Use a tolerance no larger than 5 minutes
- Log and alert on rejected webhooks for monitoring
- Store processed event IDs (the `X-TripWire-ID` header) to detect exact duplicates

## Webhook Payload Format

The webhook body is JSON with this structure:

```json
{
  "type": "payment.confirmed",
  "data": {
    "event_id": "evt_abc123...",
    "chain_id": 8453,
    "tx_hash": "0x1234...",
    "block_number": 12345678,
    "from_address": "0xSender...",
    "to_address": "0xRecipient...",
    "amount": "1000000",
    "token": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    "authorizer": "0xAuth...",
    "nonce": "0xNonce...",
    "finality_depth": 5,
    "identity_data": {
      "agent_class": "payment-bot",
      "reputation_score": 85.5
    }
  }
}
```

### Event Types

| Type | Description | Action |
|------|-------------|--------|
| `payment.confirmed` | Payment has reached required finality depth | Execute business logic (grant access, deliver goods, etc.) |
| `payment.pending` | Payment detected on-chain but not yet finalized | Show pending status to user |
| `payment.failed` | Payment failed policy checks or validation | Log for debugging, do not execute |
| `payment.reorged` | Previously confirmed payment was reorged out of the chain | **Reverse** any actions taken for this payment |

### Handling `payment.reorged`

This is the most important event to handle correctly. If you receive a `payment.reorged` event, it means a previously confirmed payment is no longer valid due to a blockchain reorganization. You must reverse any side effects (revoke access, cancel orders, etc.).
