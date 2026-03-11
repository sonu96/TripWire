"""Webhook signature verification for TripWire SDK consumers.

Standalone module — uses only the Python standard library (``hmac``,
``hashlib``, ``time``).  No extra dependencies required beyond what ships
with Python 3.8+.

Header scheme (produced by Convoy / TripWire server):
    X-TripWire-Signature : t={unix_timestamp},v1={hex_hmac_sha256}
    X-TripWire-ID        : unique message ID
    X-TripWire-Timestamp : unix timestamp (same value as in signature header)

Signed content: ``{timestamp}.{raw_payload_body}``
Algorithm     : HMAC-SHA256
Tolerance     : 300 seconds (5 minutes)

Quick-start example::

    from tripwire_sdk.verify import verify_webhook_signature

    @app.post("/webhook")
    async def handle_webhook(request: Request):
        payload = await request.body()
        headers = dict(request.headers)
        secret  = os.environ["TRIPWIRE_WEBHOOK_SECRET"]

        try:
            verify_webhook_signature(payload, headers, secret)
        except WebhookVerificationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        # ... process event
"""

from __future__ import annotations

import hashlib
import hmac
import time

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOLERANCE_SECONDS = 300  # 5 minutes
_SIG_HEADER = "x-tripwire-signature"
_ID_HEADER = "x-tripwire-id"
_TS_HEADER = "x-tripwire-timestamp"


# ---------------------------------------------------------------------------
# Exception (exported for backwards-compatible except clauses)
# ---------------------------------------------------------------------------


class WebhookVerificationError(Exception):
    """Raised when a TripWire webhook signature cannot be verified.

    Attributes:
        reason: Short machine-readable reason code, e.g.
            ``"missing_signature_header"``, ``"timestamp_too_old"``,
            ``"signature_mismatch"``.
    """

    def __init__(self, message: str, reason: str = "verification_failed") -> None:
        super().__init__(message)
        self.reason = reason


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
        WebhookVerificationError: if the header cannot be parsed or contains
            no v1 signatures.
    """
    parts = [part.strip() for part in header_value.split(",")]
    timestamp: int | None = None
    signatures: list[str] = []

    for part in parts:
        if part.startswith("t="):
            try:
                timestamp = int(part[2:])
            except ValueError:
                raise WebhookVerificationError(
                    f"Invalid timestamp in X-TripWire-Signature header: {part!r}",
                    reason="malformed_signature_header",
                )
        elif part.startswith("v1="):
            sig = part[3:]
            if sig:
                signatures.append(sig)

    if timestamp is None:
        raise WebhookVerificationError(
            "Missing timestamp (t=) in X-TripWire-Signature header",
            reason="malformed_signature_header",
        )
    if not signatures:
        raise WebhookVerificationError(
            "No v1 signatures found in X-TripWire-Signature header",
            reason="malformed_signature_header",
        )

    return timestamp, signatures


def _verify_internal(
    payload: bytes,
    norm_headers: dict[str, str],
    secret: str,
) -> None:
    """Core verification logic.  Raises ``WebhookVerificationError`` on failure."""

    # --- extract signature header -------------------------------------------
    sig_header = norm_headers.get(_SIG_HEADER)
    if not sig_header:
        raise WebhookVerificationError(
            "Missing X-TripWire-Signature header",
            reason="missing_signature_header",
        )

    # --- parse signature header ---------------------------------------------
    timestamp, signatures = _parse_signature_header(sig_header)

    # --- timestamp tolerance check ------------------------------------------
    now = int(time.time())
    age = abs(now - timestamp)
    if age > _TOLERANCE_SECONDS:
        raise WebhookVerificationError(
            f"Webhook timestamp is too old: age={age}s, tolerance={_TOLERANCE_SECONDS}s",
            reason="timestamp_too_old",
        )

    # --- compute expected signature -----------------------------------------
    signed_content = f"{timestamp}.".encode("utf-8") + payload
    expected = _compute_hmac(secret, signed_content)

    # --- constant-time comparison against all provided v1 sigs --------------
    verified = any(
        hmac.compare_digest(expected, provided) for provided in signatures
    )

    if not verified:
        raise WebhookVerificationError(
            "Webhook signature does not match",
            reason="signature_mismatch",
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def sign_payload(payload: str | bytes, secret: str) -> dict[str, str]:
    """Generate TripWire webhook headers for *payload* signed with *secret*.

    Primarily intended for testing and local development — lets you produce
    valid signed requests without a running TripWire / Convoy instance.

    Args:
        payload: The raw request body to sign (string or bytes).
        secret:  The endpoint webhook secret.

    Returns:
        A dict containing the three TripWire signature headers::

            {
                "X-TripWire-ID":        "<uuid>",
                "X-TripWire-Timestamp": "<unix_ts>",
                "X-TripWire-Signature": "t=<unix_ts>,v1=<hex_hmac>",
            }
    """
    import uuid

    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    ts = str(int(time.time()))
    msg_id = str(uuid.uuid4())
    signed_content = f"{ts}.".encode("utf-8") + payload
    sig = _compute_hmac(secret, signed_content)

    return {
        "X-TripWire-ID": msg_id,
        "X-TripWire-Timestamp": ts,
        "X-TripWire-Signature": f"t={ts},v1={sig}",
    }


def verify_webhook_signature(
    payload: str | bytes,
    headers: dict[str, str],
    secret: str,
) -> bool:
    """Verify an incoming TripWire webhook signature.

    Raises ``WebhookVerificationError`` on any failure so callers can catch
    it and return an appropriate HTTP 4xx response.

    Args:
        payload: The raw request body (string or bytes). Must be the
            unmodified body exactly as received on the wire.
        headers: The request headers dict (case-insensitive comparison is
            applied internally). Must contain:
            - ``X-TripWire-Signature``  (``t={ts},v1={hex_hmac}``)
            - ``X-TripWire-ID``
            - ``X-TripWire-Timestamp``
        secret: The endpoint signing secret.

    Returns:
        ``True`` if verification succeeds.

    Raises:
        WebhookVerificationError: if signature verification fails for any
            reason (missing headers, malformed header, timestamp out of
            tolerance, or HMAC mismatch).
    """
    if isinstance(payload, str):
        payload = payload.encode("utf-8")

    norm = _normalise_headers(headers)
    _verify_internal(payload, norm, secret)
    return True


def verify_webhook_signature_safe(
    payload: str | bytes,
    headers: dict[str, str],
    secret: str,
) -> bool:
    """Verify an incoming TripWire webhook signature without raising exceptions.

    Identical to :func:`verify_webhook_signature` but returns ``False``
    instead of raising :exc:`WebhookVerificationError` on failure.  Use this
    variant when you prefer a simple boolean check.

    Args:
        payload: The raw request body (string or bytes).
        headers: The request headers dict.
        secret:  The endpoint signing secret.

    Returns:
        ``True`` if the signature is valid and the timestamp is within the
        5-minute tolerance window; ``False`` on any failure.
    """
    try:
        return verify_webhook_signature(payload, headers, secret)
    except WebhookVerificationError:
        return False
