"""Redis-backed shared cache for multi-instance consistency."""

import json
import time
from typing import Any

import structlog

logger = structlog.get_logger(__name__)


class RedisCache:
    """Shared cache backed by Redis. Falls back to in-memory on Redis failure."""

    def __init__(self, redis_client, prefix: str = "cache", default_ttl: int = 30):
        self._redis = redis_client
        self._prefix = prefix
        self._default_ttl = default_ttl
        # In-memory fallback for when Redis is unavailable
        self._local: dict[str, tuple[Any, float]] = {}

    def _key(self, name: str) -> str:
        return f"{self._prefix}:{name}"

    async def get(self, name: str) -> Any | None:
        """Get cached value. Tries Redis first, falls back to local."""
        key = self._key(name)
        try:
            if self._redis:
                raw = await self._redis.get(key)
                if raw:
                    return json.loads(raw)
        except Exception:
            logger.debug("redis_cache_get_failed", key=key)

        # Fallback to local
        entry = self._local.get(key)
        if entry:
            value, expires_at = entry
            if time.time() < expires_at:
                return value
            del self._local[key]
        return None

    async def set(self, name: str, value: Any, ttl: int | None = None) -> None:
        """Set cached value in Redis + local fallback."""
        key = self._key(name)
        ttl = ttl or self._default_ttl
        serialized = json.dumps(value, default=str)

        try:
            if self._redis:
                await self._redis.set(key, serialized, ex=ttl)
        except Exception:
            logger.debug("redis_cache_set_failed", key=key)

        # Always update local fallback
        self._local[key] = (value, time.time() + ttl)

    async def delete(self, name: str) -> None:
        """Delete from both Redis and local."""
        key = self._key(name)
        try:
            if self._redis:
                await self._redis.delete(key)
        except Exception:
            pass
        self._local.pop(key, None)

    async def invalidate_pattern(self, pattern: str) -> None:
        """Invalidate all keys matching pattern."""
        full_pattern = self._key(pattern)
        try:
            if self._redis:
                keys = []
                async for key in self._redis.scan_iter(match=full_pattern):
                    keys.append(key)
                if keys:
                    await self._redis.delete(*keys)
        except Exception:
            pass
        # Clear matching local entries
        prefix_match = self._prefix + ":" + pattern.replace("*", "")
        to_delete = [k for k in self._local if k.startswith(prefix_match)]
        for k in to_delete:
            del self._local[k]
