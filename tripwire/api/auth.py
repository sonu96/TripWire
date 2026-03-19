"""SIWE (EIP-4361) wallet authentication with replay prevention for TripWire."""

from dataclasses import dataclass

import structlog
from fastapi import HTTPException, Request

from tripwire.api.redis import get_redis
from tripwire.auth.siwe import build_siwe_message, build_request_statement, verify_siwe_signature, validate_timestamps
from tripwire.config.settings import settings
from tripwire.observability.audit import fire_and_forget

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class WalletAuthContext:
    """Authenticated caller context carrying the verified wallet address."""

    wallet_address: str



def _get_audit_logger(request: Request):
    """Return the audit logger if available, else None."""
    return getattr(getattr(request, "app", None), "state", None) and getattr(request.app.state, "audit_logger", None)


async def require_wallet_auth(request: Request) -> WalletAuthContext:
    """FastAPI dependency that enforces SIWE wallet signature authentication.

    Expected headers:
        X-TripWire-Address        -- caller's Ethereum address (0x...)
        X-TripWire-Signature      -- EIP-191 personal_sign hex signature (0x...)
        X-TripWire-Nonce          -- nonce previously obtained from GET /auth/nonce
        X-TripWire-Issued-At      -- ISO-8601 timestamp when the message was signed
        X-TripWire-Expiration     -- ISO-8601 expiration timestamp

    Verification steps:
        1. Read the request body and compute its SHA-256 hash.
        2. Reconstruct the SIWE message with method + path + body hash as the statement.
        3. Recover the signer address from the EIP-191 signature.
        4. Compare recovered address to the claimed address (case-insensitive).
        5. Atomically consume the nonce from Redis (reject if missing / already used).
        6. Validate the expiration time has not passed.
    """
    address = request.headers.get("X-TripWire-Address")
    signature = request.headers.get("X-TripWire-Signature")
    nonce = request.headers.get("X-TripWire-Nonce")
    issued_at = request.headers.get("X-TripWire-Issued-At")
    expiration_time = request.headers.get("X-TripWire-Expiration")

    if not all([address, signature, nonce, issued_at, expiration_time]):
        _audit = _get_audit_logger(request)
        if _audit:
            fire_and_forget(_audit.log(
                action="auth.failed",
                actor=address or "unknown",
                resource_type="auth",
                resource_id="missing_headers",
                details={"reason": "missing_headers"},
                ip_address=request.client.host if request.client else None,
            ))
        raise HTTPException(
            status_code=401,
            detail="Missing authentication headers; "
            "X-TripWire-Address, X-TripWire-Signature, X-TripWire-Nonce, "
            "X-TripWire-Issued-At, and X-TripWire-Expiration are all required",
        )

    # --- Expiration validation ---
    try:
        validate_timestamps(issued_at, expiration_time, check_issued_at_tolerance=False)
    except ValueError as exc:
        _audit = _get_audit_logger(request)
        if _audit:
            fire_and_forget(_audit.log(
                action="auth.failed",
                actor=address,
                resource_type="auth",
                resource_id="expired",
                details={"reason": "signature_expired"},
                ip_address=request.client.host if request.client else None,
            ))
        raise HTTPException(status_code=401, detail=str(exc))

    # --- Body hash + statement ---
    body_bytes = await request.body()
    method = request.method
    path = request.url.path
    statement = build_request_statement(method, path, body_bytes)

    # --- Reconstruct SIWE message ---
    message_text = build_siwe_message(
        domain=settings.siwe_domain,
        address=address,
        statement=statement,
        nonce=nonce,
        issued_at=issued_at,
        expiration_time=expiration_time,
        chain_id=settings.siwe_chain_id,
    )

    # --- Signature recovery + address comparison ---
    try:
        recovered = verify_siwe_signature(message_text, signature, address)
    except ValueError as exc:
        logger.warning("wallet_auth_recovery_failed", error=str(exc))
        _audit = _get_audit_logger(request)
        if _audit:
            fire_and_forget(_audit.log(
                action="auth.failed",
                actor=address,
                resource_type="auth",
                resource_id="invalid_signature",
                details={"reason": "signature_recovery_or_mismatch"},
                ip_address=request.client.host if request.client else None,
            ))
        raise HTTPException(status_code=401, detail="Invalid signature or address mismatch")
    except Exception as exc:
        logger.warning("wallet_auth_recovery_failed", error=str(exc))
        _audit = _get_audit_logger(request)
        if _audit:
            fire_and_forget(_audit.log(
                action="auth.failed",
                actor=address,
                resource_type="auth",
                resource_id="invalid_signature",
                details={"reason": "signature_recovery_failed"},
                ip_address=request.client.host if request.client else None,
            ))
        raise HTTPException(status_code=401, detail="Invalid signature")

    # --- Nonce consumption (atomic: delete returns 1 if key existed, 0 if not) ---
    r = get_redis()
    consumed = await r.delete(f"siwe:nonce:{nonce}")
    if consumed == 0:
        logger.warning("wallet_auth_nonce_invalid", nonce=nonce)
        _audit = _get_audit_logger(request)
        if _audit:
            fire_and_forget(_audit.log(
                action="auth.failed",
                actor=address,
                resource_type="auth",
                resource_id="invalid_nonce",
                details={"reason": "nonce_invalid_or_reused"},
                ip_address=request.client.host if request.client else None,
            ))
        raise HTTPException(status_code=401, detail="Invalid or already-used nonce")

    logger.debug("wallet_auth_ok", wallet_address=recovered)
    _audit = _get_audit_logger(request)
    if _audit:
        fire_and_forget(_audit.log(
            action="auth.success",
            actor=recovered,
            resource_type="auth",
            resource_id=recovered,
            details={"method": method, "path": path},
            ip_address=request.client.host if request.client else None,
        ))
    return WalletAuthContext(wallet_address=recovered)
