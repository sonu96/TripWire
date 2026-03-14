"""Authentication routes — nonce issuance for SIWE replay prevention."""

import secrets

import structlog
from fastapi import APIRouter, Request

from tripwire.api.redis import get_redis
from tripwire.api.ratelimit import limiter

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/auth", tags=["auth"])

_NONCE_TTL_SECONDS = 300  # 5 minutes


@router.get("/nonce")
@limiter.limit("30/minute")
@limiter.limit("1000/minute", key_func=lambda *args, **kwargs: "global")
async def get_nonce(request: Request) -> dict[str, str]:
    """Generate a cryptographically random nonce, store it in Redis with a 5-min TTL."""
    nonce = secrets.token_urlsafe(32)
    r = get_redis()
    await r.setex(f"siwe:nonce:{nonce}", _NONCE_TTL_SECONDS, "1")
    logger.debug("nonce_issued", nonce=nonce)
    return {"nonce": nonce}
