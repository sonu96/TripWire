"""Wallet-based authentication helpers for TripWire API requests.

Constructs and signs EIP-191 messages so the server can verify the
caller's Ethereum address without relying on static API keys.
"""

from __future__ import annotations

import time

from eth_account import Account
from eth_account.messages import encode_defunct


def build_auth_message(address: str, timestamp: int, path: str) -> str:
    """Return the canonical string that must be signed for a request.

    Format: ``TripWire:{address}:{timestamp}:{path}``
    """
    return f"TripWire:{address}:{timestamp}:{path}"


def sign_auth_message(private_key: str, address: str, timestamp: int, path: str) -> str:
    """Sign the TripWire auth message with a private key (EIP-191).

    Returns the hex-encoded signature (with ``0x`` prefix).
    """
    message_text = build_auth_message(address, timestamp, path)
    signable = encode_defunct(text=message_text)
    account = Account.from_key(private_key)
    signed = account.sign_message(signable)
    return signed.signature.hex()


def make_auth_headers(private_key: str, address: str, path: str) -> dict[str, str]:
    """Return a dict of authentication headers for a single API request.

    Headers:
        X-TripWire-Address   -- the checksummed wallet address
        X-TripWire-Signature -- hex EIP-191 signature of the auth message
        X-TripWire-Timestamp -- unix timestamp (seconds) when the signature was created
    """
    timestamp = int(time.time())
    signature = sign_auth_message(private_key, address, timestamp, path)
    return {
        "X-TripWire-Address": address,
        "X-TripWire-Signature": signature,
        "X-TripWire-Timestamp": str(timestamp),
    }
