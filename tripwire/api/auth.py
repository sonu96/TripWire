"""API key authentication for TripWire endpoints."""

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import structlog
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

# auto_error=False so we can handle dev-mode skip ourselves
_bearer_scheme = HTTPBearer(auto_error=False)


def generate_api_key() -> str:
    """Generate a new API key with the 'tw_' prefix."""
    return "tw_" + secrets.token_hex(32)


def hash_api_key(key: str) -> str:
    """Return the SHA-256 hex digest of an API key."""
    return hashlib.sha256(key.encode()).hexdigest()


async def require_api_key(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str | None:
    """FastAPI dependency that enforces Bearer token authentication.

    Uses FastAPI's built-in HTTPBearer to extract the Bearer token from the
    Authorization header, hashes it, and looks it up in the endpoints table.

    Returns the endpoint_id associated with the key.
    Raises 401 if the key is invalid or missing.

    In development (APP_ENV=development), skips auth if no header is present.

    Supports a grace period after key rotation: if key_rotated_at is within
    the configured window, both the current and old key hashes are accepted.
    """
    # In development, skip if no Authorization header
    if credentials is None:
        if settings.app_env == "development":
            return None
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")

    token = credentials.credentials
    if not token:
        raise HTTPException(status_code=401, detail="Missing API key")

    key_hash = hash_api_key(token)

    sb = request.app.state.supabase

    # First, try the current api_key_hash (fast path)
    result = (
        sb.table("endpoints")
        .select("id")
        .eq("api_key_hash", key_hash)
        .eq("active", True)
        .execute()
    )

    if result.data:
        endpoint_id = result.data[0]["id"]
        logger.debug("api_key_authenticated", endpoint_id=endpoint_id)
        return endpoint_id

    # Fall back: check old_api_key_hash within grace period
    result = (
        sb.table("endpoints")
        .select("id, key_rotated_at")
        .eq("old_api_key_hash", key_hash)
        .eq("active", True)
        .execute()
    )

    if result.data:
        row = result.data[0]
        rotated_at = row.get("key_rotated_at")
        if rotated_at:
            if isinstance(rotated_at, str):
                rotated_at = datetime.fromisoformat(rotated_at)
            grace_cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.key_rotation_grace_hours)
            if rotated_at >= grace_cutoff:
                endpoint_id = row["id"]
                logger.debug(
                    "api_key_authenticated_via_old_key",
                    endpoint_id=endpoint_id,
                    key_rotated_at=rotated_at.isoformat(),
                )
                return endpoint_id

    raise HTTPException(status_code=401, detail="Invalid API key")
