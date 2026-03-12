"""TripWire x402 Payment Flow — how endpoint registration payments work.

Registering a TripWire endpoint now costs $1.00 USDC, paid automatically via
the x402 protocol.  This example walks through the full flow and explains
what happens under the hood.

Prerequisites:
    pip install tripwire-sdk[x402]

Your wallet (the private key you provide) must hold USDC on Base (chain 8453).
The SDK handles the rest — you never manually construct a payment transaction.

How x402 works (what the SDK does for you):
    1. You call client.register_endpoint(...).
    2. The SDK sends a POST to /api/v1/endpoints.
    3. The server replies with HTTP 402 Payment Required, including a
       payment header that says "send $1.00 USDC to this address on Base."
    4. The x402 interceptor (x402Client, a drop-in httpx wrapper) sees the
       402, constructs an ERC-3009 `transferWithAuthorization` signature
       using your private key — no on-chain transaction yet.
    5. The interceptor retries the original request with the signed payment
       authorization attached in headers.
    6. The server validates the authorization, submits it on-chain, and
       returns the endpoint along with a `registration_tx_hash` proving
       the USDC was transferred.

    All of this is invisible to your code.  register_endpoint() just returns
    an Endpoint with the payment receipt baked in.
"""

import asyncio
import os
import sys

from tripwire_sdk import TripwireClient


# ── Optional: check USDC balance before registering ─────────────────

async def check_usdc_balance(address: str) -> float | None:
    """Read USDC balance on Base via a public RPC (optional convenience).

    Returns the balance in human-readable USDC (6 decimals), or None if
    the check fails.  This is purely informational — the SDK does not
    require you to check your balance before registering.
    """
    import httpx

    # USDC on Base mainnet
    USDC_ADDRESS = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"

    # ERC-20 balanceOf(address) selector
    calldata = (
        "0x70a08231"
        + address.lower().replace("0x", "").zfill(64)
    )

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [
            {"to": USDC_ADDRESS, "data": calldata},
            "latest",
        ],
    }

    try:
        async with httpx.AsyncClient() as http:
            resp = await http.post("https://mainnet.base.org", json=payload)
            raw = resp.json()["result"]
            return int(raw, 16) / 1_000_000
    except Exception as exc:
        print(f"  (Could not fetch balance: {exc})")
        return None


# ── Main example ─────────────────────────────────────────────────────

async def main():
    # Load your private key.  This wallet will:
    #   - Authenticate via SIWE (EIP-191 signature)
    #   - Pay the $1.00 USDC registration fee via x402 (ERC-3009 signature)
    #
    # The same key is used for both — no separate "payment key" needed.
    private_key = os.environ.get("TRIPWIRE_PRIVATE_KEY")
    if not private_key:
        print("Set TRIPWIRE_PRIVATE_KEY to an Ethereum private key with USDC on Base.")
        sys.exit(1)

    # ── Step 1: Create the client (x402 is enabled by default) ───────
    #
    # Under the hood, TripwireClient wraps httpx.AsyncClient with the
    # x402Client transport.  This intercepts any HTTP 402 response and
    # handles payment automatically.
    #
    # If you installed `tripwire-sdk[x402]`, this just works.
    # If x402 is not installed, the client warns and falls back to plain
    # httpx — but register_endpoint() will fail with a 402 error.

    async with TripwireClient(private_key=private_key) as client:
        print(f"Wallet address: {client.wallet_address}")

        # ── Optional: check your USDC balance ────────────────────────
        balance = await check_usdc_balance(client.wallet_address)
        if balance is not None:
            print(f"USDC balance on Base: ${balance:.2f}")
            if balance < 1.0:
                print(
                    "WARNING: You need at least $1.00 USDC on Base to "
                    "register an endpoint.  Fund your wallet first."
                )
                sys.exit(1)

        # ── Step 2: Register an endpoint (payment happens here) ──────
        #
        # This single call triggers the full x402 flow:
        #   POST /api/v1/endpoints
        #     -> server returns 402 Payment Required
        #     -> x402Client signs an ERC-3009 transferWithAuthorization
        #     -> x402Client retries POST with payment proof in headers
        #     -> server submits the USDC transfer on-chain
        #     -> server returns 201 Created with the endpoint + tx hash
        #
        # From your perspective, it is just an await that returns an Endpoint.

        print("\nRegistering endpoint (this will pay $1.00 USDC via x402)...")

        endpoint = await client.register_endpoint(
            url="https://your-api.com/webhook",
            mode="execute",
            chains=[8453],           # Listen for payments on Base
            recipient="0xYourRecipientAddress",
            policies={
                "min_amount": "1000000",       # 1 USDC minimum payment
                "min_reputation_score": 70,    # Only trusted agents
            },
        )

        # ── Step 3: Inspect the result ───────────────────────────────

        print("\nEndpoint registered successfully!")
        print(f"  Endpoint ID:  {endpoint.id}")
        print(f"  Owner:        {endpoint.owner_address}")
        print(f"  URL:          {endpoint.url}")
        print(f"  Mode:         {endpoint.mode.value}")
        print(f"  Chains:       {endpoint.chains}")
        print(f"  Active:       {endpoint.active}")

        # The registration_tx_hash proves the $1.00 USDC payment was
        # submitted on-chain.  You can look it up on BaseScan:
        #   https://basescan.org/tx/<registration_tx_hash>
        tx_hash = getattr(endpoint, "registration_tx_hash", None)
        if tx_hash:
            print(f"\n  Payment tx:   {tx_hash}")
            print(f"  BaseScan:     https://basescan.org/tx/{tx_hash}")
        else:
            # If the server does not yet include the tx_hash in the
            # Endpoint model, the payment still happened — check the
            # x402 response headers or server logs.
            print("\n  (registration_tx_hash not in response — payment was still made)")


asyncio.run(main())
