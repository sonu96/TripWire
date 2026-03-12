"""Shared async Redis connection for the TripWire API layer."""

import redis.asyncio as aioredis

from tripwire.config.settings import settings

_redis: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Lazily create a shared async Redis connection."""
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis
