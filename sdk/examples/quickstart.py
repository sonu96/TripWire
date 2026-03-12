"""TripWire Quickstart — 3 steps to verified payment webhooks.

1. Register an endpoint
2. Receive webhooks
3. Verify signatures

pip install tripwire-sdk
"""

import asyncio
import os

from tripwire_sdk import TripwireClient


async def main():
    # Load your Ethereum private key from the environment (never hard-code it!)
    private_key = os.environ["TRIPWIRE_PRIVATE_KEY"]

    # Step 1: Register your webhook endpoint
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
