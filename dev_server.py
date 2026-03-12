"""TripWire development server with auth bypass.

Run this file directly to start TripWire with wallet authentication disabled.
This file is the ONLY place the dev auth bypass exists and must NEVER be
deployed to production.

Usage:
    python dev_server.py
    # or with a custom dev wallet:
    DEV_WALLET_ADDRESS=0xYourAddress python dev_server.py
"""

from __future__ import annotations

import os
import sys

# Force development mode BEFORE importing settings (which validates on import)
os.environ.setdefault("APP_ENV", "development")

import uvicorn

from tripwire.api.auth import WalletAuthContext, require_wallet_auth
from tripwire.config.settings import settings
from tripwire.main import app

# Configurable dev wallet address (never use the zero address)
DEV_WALLET_ADDRESS = os.environ.get(
    "DEV_WALLET_ADDRESS",
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266",  # Hardhat account #0
)


async def _dev_require_wallet_auth() -> WalletAuthContext:
    """Bypass wallet authentication and return a dev context."""
    return WalletAuthContext(wallet_address=DEV_WALLET_ADDRESS)


# Override the auth dependency globally
app.dependency_overrides[require_wallet_auth] = _dev_require_wallet_auth

if __name__ == "__main__":
    print(
        "\n"
        "============================================================\n"
        "  DEV MODE - Auth bypassed\n"
        f"  Dev wallet: {DEV_WALLET_ADDRESS}\n"
        "  DO NOT USE IN PRODUCTION\n"
        "============================================================\n"
    )

    uvicorn.run(
        "dev_server:app",
        host="0.0.0.0",
        port=settings.app_port,
        log_level=settings.log_level,
        reload=True,
    )
