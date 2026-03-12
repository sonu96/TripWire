"""SIWE (EIP-4361) authentication helpers for TripWire API requests.

Constructs and signs SIWE messages so the server can verify the
caller's Ethereum address with replay prevention via server-issued nonces.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

# Default SIWE domain — must match the server's siwe_domain setting
_DEFAULT_DOMAIN = "tripwire.dev"

# Signature validity window
_EXPIRATION_MINUTES = 5


def _build_siwe_message(
    domain: str,
    address: str,
    statement: str,
    nonce: str,
    issued_at: str,
    expiration_time: str,
) -> str:
    """Construct an EIP-4361 SIWE message string."""
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{address}\n"
        f"\n"
        f"{statement}\n"
        f"\n"
        f"URI: https://{domain}\n"
        f"Version: 1\n"
        f"Chain ID: 1\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}\n"
        f"Expiration Time: {expiration_time}"
    )


def build_auth_message(
    address: str,
    nonce: str,
    method: str,
    path: str,
    body_bytes: bytes = b"",
    domain: str = _DEFAULT_DOMAIN,
    issued_at: str | None = None,
    expiration_time: str | None = None,
) -> tuple[str, str, str]:
    """Build the canonical SIWE message for a request.

    Returns ``(message_text, issued_at, expiration_time)``.
    """
    now = datetime.now(timezone.utc)
    if issued_at is None:
        issued_at = now.isoformat()
    if expiration_time is None:
        expiration_time = (now + timedelta(minutes=_EXPIRATION_MINUTES)).isoformat()

    body_hash = hashlib.sha256(body_bytes).hexdigest()
    statement = f"{method} {path} {body_hash}"

    message_text = _build_siwe_message(
        domain=domain,
        address=address,
        statement=statement,
        nonce=nonce,
        issued_at=issued_at,
        expiration_time=expiration_time,
    )
    return message_text, issued_at, expiration_time


def sign_auth_message(
    key_or_account: str | LocalAccount,
    address: str,
    nonce: str,
    method: str,
    path: str,
    body_bytes: bytes = b"",
    domain: str = _DEFAULT_DOMAIN,
) -> tuple[str, str, str]:
    """Sign the TripWire SIWE auth message with a private key (EIP-191).

    Returns ``(signature_hex, issued_at, expiration_time)``.
    """
    message_text, issued_at, expiration_time = build_auth_message(
        address=address,
        nonce=nonce,
        method=method,
        path=path,
        body_bytes=body_bytes,
        domain=domain,
    )
    signable = encode_defunct(text=message_text)
    if isinstance(key_or_account, LocalAccount):
        account = key_or_account
    else:
        account = Account.from_key(key_or_account)
    signed = account.sign_message(signable)
    return signed.signature.hex(), issued_at, expiration_time


def make_auth_headers(
    key_or_account: str | LocalAccount,
    address: str,
    path: str,
    *,
    nonce: str,
    method: str = "GET",
    body_bytes: bytes = b"",
    domain: str = _DEFAULT_DOMAIN,
) -> dict[str, str]:
    """Return a dict of authentication headers for a single API request.

    Headers:
        X-TripWire-Address    -- the checksummed wallet address
        X-TripWire-Signature  -- hex EIP-191 signature of the SIWE message
        X-TripWire-Nonce      -- server-issued nonce (consumed on use)
        X-TripWire-Issued-At  -- ISO-8601 timestamp
        X-TripWire-Expiration -- ISO-8601 expiration timestamp
    """
    signature, issued_at, expiration_time = sign_auth_message(
        key_or_account=key_or_account,
        address=address,
        nonce=nonce,
        method=method,
        path=path,
        body_bytes=body_bytes,
        domain=domain,
    )
    return {
        "X-TripWire-Address": address,
        "X-TripWire-Signature": signature,
        "X-TripWire-Nonce": nonce,
        "X-TripWire-Issued-At": issued_at,
        "X-TripWire-Expiration": expiration_time,
    }
