"""Single source of truth for SIWE (EIP-4361) message construction and verification."""

import hashlib
import time
from datetime import datetime

from eth_account import Account
from eth_account.messages import encode_defunct


def build_siwe_message(
    domain: str,
    address: str,
    statement: str,
    nonce: str,
    issued_at: str,
    expiration_time: str,
    chain_id: int = 8453,
) -> str:
    """Construct an EIP-4361 SIWE message string."""
    return (
        f"{domain} wants you to sign in with your Ethereum account:\n"
        f"{address}\n\n"
        f"{statement}\n\n"
        f"URI: https://{domain}\n"
        f"Version: 1\n"
        f"Chain ID: {chain_id}\n"
        f"Nonce: {nonce}\n"
        f"Issued At: {issued_at}\n"
        f"Expiration Time: {expiration_time}"
    )


def build_request_statement(method: str, path: str, body_bytes: bytes = b"") -> str:
    """Construct the SIWE statement: '{METHOD} {PATH} {SHA256(body)}'."""
    body_hash = hashlib.sha256(body_bytes).hexdigest()
    return f"{method} {path} {body_hash}"


def verify_siwe_signature(
    message_text: str,
    signature: str,
    expected_address: str,
) -> str:
    """Verify an EIP-191 signature over a SIWE message. Returns recovered address.
    Raises ValueError on mismatch or invalid signature.
    """
    msg = encode_defunct(text=message_text)
    recovered = Account.recover_message(msg, signature=signature)
    if recovered.lower() != expected_address.lower():
        raise ValueError(f"Signature mismatch: recovered {recovered}, expected {expected_address}")
    return recovered


def validate_timestamps(
    issued_at: str,
    expiration_time: str,
    *,
    check_issued_at_tolerance: bool = False,
    tolerance_seconds: int = 300,
) -> None:
    """Validate SIWE timestamp fields. Raises ValueError on failure."""
    now = time.time()
    try:
        exp_dt = datetime.fromisoformat(expiration_time.replace("Z", "+00:00"))
    except (ValueError, AttributeError) as e:
        raise ValueError(f"Invalid expiration_time format: {expiration_time}") from e

    if exp_dt.timestamp() < now:
        raise ValueError("SIWE message has expired")

    if check_issued_at_tolerance:
        try:
            iat_dt = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        except (ValueError, AttributeError) as e:
            raise ValueError(f"Invalid issued_at format: {issued_at}") from e
        if abs(now - iat_dt.timestamp()) > tolerance_seconds:
            raise ValueError(f"issued_at is outside tolerance of {tolerance_seconds}s")
