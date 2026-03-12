"""TripWire webhook handler — verify and process payment events.

This is what runs on YOUR server to receive webhooks from TripWire.

pip install tripwire-sdk fastapi uvicorn
"""

import os

from fastapi import FastAPI, HTTPException, Request

from tripwire_sdk import WebhookPayload, verify_webhook_signature

app = FastAPI()

WEBHOOK_SECRET = os.environ["TRIPWIRE_WEBHOOK_SECRET"]


@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.body()
    headers = dict(request.headers)

    # Step 2: Verify the signature
    try:
        verify_webhook_signature(body, headers, WEBHOOK_SECRET)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Step 3: Process the event
    payload = WebhookPayload.model_validate_json(body)

    if payload.type == "payment.confirmed":
        transfer = payload.data["transfer"]
        identity = payload.data.get("identity")

        print(f"Payment confirmed!")
        print(f"  From: {transfer['from_address']}")
        print(f"  Amount: {int(transfer['amount']) / 1_000_000} USDC")
        print(f"  Chain: {transfer['chain_id']}")

        if identity:
            print(f"  Agent class: {identity['agent_class']}")
            print(f"  Reputation: {identity['reputation_score']}")

        # Your business logic here:
        # - Fulfill the API request
        # - Credit the user's account
        # - Trigger a workflow

    return {"status": "ok"}
