"""Shared wallet test helpers and constants.

Importable from any test module via `from tests._wallet_helpers import ...`.
"""

import hashlib
import secrets
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock

from eth_account import Account
from eth_account.messages import encode_defunct

from tripwire.auth.siwe import build_siwe_message
from tripwire.config.settings import settings

# ── Deterministic test wallets (Hardhat default accounts) ─────

TEST_PRIVATE_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
OTHER_PRIVATE_KEY = "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d"


def make_auth_headers(
    account,
    *,
    method="GET",
    path="/",
    body=b"",
    nonce=None,
    issued_at=None,
    expiration_time=None,
):
    """Generate real SIWE auth headers for the given account.

    Returns a dict with all five X-TripWire-* headers ready to pass to
    an HTTP client.

    The ``nonce`` defaults to a fresh random string.  In tests that exercise
    the real ``require_wallet_auth`` dependency you must also pre-seed the
    nonce in a mock Redis via ``seed_nonce()``.
    """
    if nonce is None:
        nonce = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    if issued_at is None:
        issued_at = now.isoformat()
    if expiration_time is None:
        expiration_time = (now + timedelta(minutes=5)).isoformat()

    address = account.address
    body_hash = hashlib.sha256(body if isinstance(body, bytes) else body.encode()).hexdigest()
    statement = f"{method} {path} {body_hash}"

    message_text = build_siwe_message(
        domain=settings.siwe_domain,
        address=address,
        statement=statement,
        nonce=nonce,
        issued_at=issued_at,
        expiration_time=expiration_time,
        chain_id=settings.siwe_chain_id,
    )
    signable = encode_defunct(text=message_text)
    signed = account.sign_message(signable)

    sig_hex = signed.signature.hex() if isinstance(signed.signature, bytes) else str(signed.signature)

    return {
        "X-TripWire-Address": address,
        "X-TripWire-Signature": sig_hex,
        "X-TripWire-Nonce": nonce,
        "X-TripWire-Issued-At": issued_at,
        "X-TripWire-Expiration": expiration_time,
    }


class MockRedis:
    """In-memory mock for redis.asyncio.Redis used by SIWE nonce consumption."""

    def __init__(self):
        self._store: dict[str, str] = {}

    async def setex(self, key: str, ttl: int, value: str):
        self._store[key] = value

    async def delete(self, key: str) -> int:
        if key in self._store:
            del self._store[key]
            return 1
        return 0

    async def get(self, key: str):
        return self._store.get(key)

    def seed_nonce(self, nonce: str):
        """Pre-seed a nonce so require_wallet_auth can consume it."""
        self._store[f"siwe:nonce:{nonce}"] = "1"
