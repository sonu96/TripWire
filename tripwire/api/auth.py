"""Wallet-based EIP-191 signature authentication for TripWire endpoints."""

import time
from dataclasses import dataclass

import structlog
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import HTTPException, Request

from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

# Maximum age (in seconds) of a signed timestamp before it's rejected
_TIMESTAMP_TOLERANCE = 300


@dataclass(frozen=True)
class WalletAuthContext:
    """Authenticated caller context carrying the verified wallet address."""

    wallet_address: str


async def require_wallet_auth(request: Request) -> WalletAuthContext:
    """FastAPI dependency that enforces EIP-191 wallet signature authentication.

    Expected headers:
        X-TripWire-Address   – caller's Ethereum address (0x…)
        X-TripWire-Signature – EIP-191 personal_sign hex signature (0x…)
        X-TripWire-Timestamp – Unix epoch seconds when the message was signed

    The signed message format is:
        TripWire:{address}:{timestamp}:{request_path}

    Verification steps:
        1. Reconstruct the canonical message from the headers and request path.
        2. Recover the signer address from the EIP-191 signature.
        3. Compare recovered address to the claimed address (case-insensitive).
        4. Ensure the timestamp is within the allowed tolerance window.

    In development mode (APP_ENV=development), authentication is skipped when
    none of the required headers are present.
    """
    address = request.headers.get("X-TripWire-Address")
    signature = request.headers.get("X-TripWire-Signature")
    timestamp = request.headers.get("X-TripWire-Timestamp")

    # Dev-mode bypass: skip auth when no headers are supplied at all
    if address is None and signature is None and timestamp is None:
        if settings.app_env == "development":
            logger.debug("wallet_auth_skipped", reason="development mode, no headers")
            return WalletAuthContext(wallet_address="0x0000000000000000000000000000000000000000")
        raise HTTPException(status_code=401, detail="Missing authentication headers")

    # If some but not all headers are present, reject immediately
    if not all([address, signature, timestamp]):
        raise HTTPException(
            status_code=401,
            detail="Incomplete authentication headers; "
            "X-TripWire-Address, X-TripWire-Signature, and X-TripWire-Timestamp are all required",
        )

    # --- Timestamp validation ---
    try:
        ts = int(timestamp)
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid timestamp format")

    now = int(time.time())
    if abs(now - ts) > _TIMESTAMP_TOLERANCE:
        raise HTTPException(status_code=401, detail="Timestamp expired or too far in the future")

    # --- Signature recovery ---
    request_path = request.url.path
    message_text = f"TripWire:{address}:{timestamp}:{request_path}"
    signable = encode_defunct(text=message_text)

    try:
        recovered = Account.recover_message(signable, signature=signature)
    except Exception as exc:
        logger.warning("wallet_auth_recovery_failed", error=str(exc))
        raise HTTPException(status_code=401, detail="Invalid signature")

    # --- Address comparison (EIP-55 checksum-safe) ---
    if recovered.lower() != address.lower():
        logger.warning(
            "wallet_auth_address_mismatch",
            claimed=address,
            recovered=recovered,
        )
        raise HTTPException(status_code=401, detail="Signature does not match claimed address")

    logger.debug("wallet_auth_ok", wallet_address=recovered)
    return WalletAuthContext(wallet_address=recovered)
