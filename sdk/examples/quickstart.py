"""TripWire Quickstart — 3 steps to verified payment webhooks.

1. Register an endpoint (costs $1.00 USDC, paid automatically via x402)
2. Receive webhooks
3. Verify signatures

pip install tripwire-sdk[x402]

NOTE: Endpoint registration requires a $1.00 USDC payment on Base.  The SDK
handles this transparently using the x402 protocol — your wallet just needs
USDC on Base (chain 8453).  See x402_payment.py for a detailed walkthrough
of the payment flow and what happens under the hood.
"""

import asyncio
import os

from tripwire_sdk import TripwireClient


async def main():
    # Load your Ethereum private key from the environment (never hard-code it!)
    private_key = os.environ["TRIPWIRE_PRIVATE_KEY"]

    # Step 1: Register your webhook endpoint
    # If x402 is installed, any 402 Payment Required responses are
    # automatically handled — USDC payments are signed and retried
    # transparently. No code changes needed.
    async with TripwireClient(private_key=private_key) as client:
        print(f"Wallet address: {client.wallet_address}")

        endpoint = await client.register_endpoint(
            url="https://your-api.com/webhook",
            mode="execute",
            chains=[8453],  # Base
            recipient="0xYourAddress",
            policies={
                "min_amount": "1000000",  # 1 USDC minimum
                "min_reputation_score": 70,  # Only trusted agents
            },
        )

        print(f"Endpoint ID: {endpoint.id}")
        print(f"Owner: {endpoint.owner_address}")


asyncio.run(main())
