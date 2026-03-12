"""Webhook signature verification for TripWire server-side use.

Implements HMAC-SHA256 verification for the TripWire native signing scheme.
No third-party dependencies — uses only the Python standard library for
crypto, plus structlog for server-side observability.

Header scheme (produced by convoy_client.py / Agent 1):
    X-TripWire-Signature : t={unix_timestamp},v1={hex_hmac_sha256}
    X-TripWire-ID        : unique message ID
    X-TripWire-Timestamp : unix timestamp (same value as in signature header)

Signed content: ``{timestamp}.{raw_payload_body}``
Algorithm     : HMAC-SHA256
Tolerance     : 300 seconds (5 minutes)
"""

from __future__ import annotations

import hashlib
import hmac
import time

import structlog

logger = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOLERANCE_SECONDS = 300  # 5 minutes
_SIG_HEADER = "x-tripwire-signature"
_ID_HEADER = "x-tripwire-id"
_TS_HEADER = "x-tripwire-timestamp"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _normalise_headers(headers: dict[str, str]) -> dict[str, str]:
    """Return a copy of *headers* with all keys lowercased."""
    return {k.lower(): v for k, v in headers.items()}


def _compute_hmac(secret: str, signed_content: bytes) -> str:
    """Return the hex-encoded HMAC-SHA256 of *signed_content* keyed by *secret*."""
    return hmac.new(
        secret.encode("utf-8"),
        signed_content,
        hashlib.sha256,
    ).hexdigest()


def _parse_signature_header(header_value: str) -> tuple[int, list[str]]:
    """Parse ``t={ts},v1={sig}[,v1={sig2}...]`` into ``(timestamp, [sig, ...])``.

    Multiple ``v1=`` entries are supported so keys can be rotated without
    dropping deliveries mid-rotation.

    Raises:
        ValueError: if the header cannot be parsed or contains no v1 signatures.
    """
    parts = [part.strip() for part in header_value.split(",")]
    timestamp: int | None = None
    signatures: list[str] = []

    for part in parts:
        if part.startswith("t="):
            try:
                timestamp = int(part[2:])
            except ValueError as exc:
                raise ValueError(
                    f"Invalid timestamp in signature header: {part!r}"
                ) from exc
        elif part.startswith("v1="):
            sig = part[3:]
            if sig:
                signatures.append(sig)

    if timestamp is None:
        raise ValueError("Missing timestamp (t=) in X-TripWire-Signature header")
    if not signatures:
        raise ValueError("No v1 signatures found in X-TripWire-Signature header")

    return timestamp, signatures


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def verify_webhook(
    payload: str | bytes,
    headers: dict[str, str],
    secret: str,
) -> bool:
    """Verify an incoming webhook's TripWire HMAC-SHA256 signature.

    Args:
        payload: The raw request body (string or bytes). Must be the
            unmodified body exactly as received on the wire.
        headers: The request headers dict (case-insensitive comparison is
            applied internally). Must contain:
            - ``X-TripWire-Signature``  (``t={ts},v1={hex_hmac}``)
            - ``X-TripWire-ID``
            - ``X-TripWire-Timestamp``
        secret: The endpoint signing secret configured in Convoy.

    Returns:
        ``True`` if the signature is valid and the timestamp is within the
        5-minute tolerance window.  ``False`` on any verification failure.
    """
    log = logger.bind(
        headers_present=sorted(headers.keys()),
    )

    # --- normalise inputs ---------------------------------------------------
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    norm = _normalise_headers(headers)

    # --- extract headers ----------------------------------------------------
    sig_header = norm.get(_SIG_HEADER)
    if not sig_header:
        log.warning(
            "webhook_verification_failed",
            reason="missing_signature_header",
            header=_SIG_HEADER,
        )
        return False

    msg_id = norm.get(_ID_HEADER, "<unknown>")
    log = log.bind(msg_id=msg_id)

    # --- parse signature header ---------------------------------------------
    try:
        timestamp, signatures = _parse_signature_header(sig_header)
    except ValueError as exc:
        log.warning(
            "webhook_verification_failed",
            reason="malformed_signature_header",
            detail=str(exc),
        )
        return False

    # --- timestamp tolerance check ------------------------------------------
    now = int(time.time())
    age = abs(now - timestamp)
    if age > _TOLERANCE_SECONDS:
        log.warning(
            "webhook_verification_failed",
            reason="timestamp_too_old",
            age_seconds=age,
            tolerance_seconds=_TOLERANCE_SECONDS,
        )
        return False

    # --- compute expected signature -----------------------------------------
    signed_content = f"{timestamp}.".encode("utf-8") + payload
    expected = _compute_hmac(secret, signed_content)

    # --- constant-time comparison against all provided v1 sigs --------------
    verified = any(
        hmac.compare_digest(expected, provided) for provided in signatures
    )

    if not verified:
        log.warning(
            "webhook_verification_failed",
            reason="signature_mismatch",
            signatures_checked=len(signatures),
        )
        return False

    log.info("webhook_verified", msg_id=msg_id)
    return True
