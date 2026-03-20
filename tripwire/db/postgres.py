"""Direct asyncpg connection pool for coordination primitives.

Supabase PostgREST uses connection pooling (PgBouncer), which means
advisory locks are released as soon as the HTTP request completes —
breaking leader election for background tasks.

This module provides a thin asyncpg pool used *only* for:
  - Advisory locks (leader election for finality poller / TTL sweeper)
  - Queries that must execute on the same connection as the lock holder

All CRUD operations continue to use the Supabase client via PostgREST.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator

import structlog

logger = structlog.get_logger(__name__)

# Lazy import — asyncpg is optional for dev environments without a database_url
_pool: Any = None


class CoordinationLockNotAcquired(Exception):
    """Raised when pg_try_advisory_lock returns false (another session holds it)."""


async def init_pool(dsn: str, *, min_size: int = 2, max_size: int = 5) -> Any:
    """Create and cache an asyncpg connection pool.

    Parameters
    ----------
    dsn:
        PostgreSQL connection string (``postgresql://...``).
    min_size:
        Minimum number of connections kept open.
    max_size:
        Maximum number of connections in the pool.

    Returns the pool object.
    """
    global _pool
    import asyncpg  # noqa: F811

    _pool = await asyncpg.create_pool(dsn, min_size=min_size, max_size=max_size)
    logger.info("asyncpg_pool_created", min_size=min_size, max_size=max_size)
    return _pool


async def close_pool() -> None:
    """Gracefully close the asyncpg pool."""
    global _pool
    if _pool is not None:
        await _pool.close()
        logger.info("asyncpg_pool_closed")
        _pool = None


def get_pool() -> Any:
    """Return the cached asyncpg pool.

    Raises RuntimeError if the pool has not been initialised.
    """
    if _pool is None:
        raise RuntimeError(
            "asyncpg pool not initialised — call init_pool() first or set DATABASE_URL"
        )
    return _pool


# ---------------------------------------------------------------------------
# Advisory lock context manager
# ---------------------------------------------------------------------------


@asynccontextmanager
async def advisory_lock(lock_id: int) -> AsyncIterator[Any]:
    """Acquire a Postgres session-level advisory lock, yielding the connection.

    The lock is held for the entire duration of the ``async with`` block
    because the same connection is used throughout.  On exit the lock is
    explicitly released and the connection returned to the pool.

    Raises :class:`CoordinationLockNotAcquired` if the lock is already
    held by another session (uses ``pg_try_advisory_lock`` — non-blocking).
    """
    pool = get_pool()
    conn = await pool.acquire()
    try:
        acquired = await conn.fetchval("SELECT pg_try_advisory_lock($1)", lock_id)
        if not acquired:
            raise CoordinationLockNotAcquired(
                f"Advisory lock {lock_id} is held by another session"
            )
        try:
            yield conn
        finally:
            # Always release the lock before returning the connection
            try:
                await conn.fetchval("SELECT pg_advisory_unlock($1)", lock_id)
            except Exception:
                logger.warning("advisory_unlock_failed", lock_id=lock_id)
    finally:
        await pool.release(conn)


# ---------------------------------------------------------------------------
# Helper queries — run on the lock-holder connection
# ---------------------------------------------------------------------------


async def fetch_pending_events(
    conn: Any,
    chain_id: int,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch events with status IN ('pending','confirmed') for *chain_id*.

    Only returns events with a real block_number (> 0), excluding
    pre_confirmed events that have no onchain tx yet.
    """
    rows = await conn.fetch(
        """
        SELECT *
          FROM events
         WHERE status IN ('pending', 'confirmed')
           AND block_number > 0
           AND chain_id = $1
         ORDER BY block_number ASC
         LIMIT $2
        """,
        chain_id,
        limit,
    )
    return [dict(r) for r in rows]


async def fetch_stale_preconfirmed(
    conn: Any,
    cutoff_iso: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Fetch pre_confirmed events created before *cutoff_iso*.

    Returns events that have been stuck in ``pre_confirmed`` status
    longer than the TTL threshold.
    """
    rows = await conn.fetch(
        """
        SELECT *
          FROM events
         WHERE status = 'pre_confirmed'
           AND created_at < $1
         ORDER BY created_at ASC
         LIMIT $2
        """,
        cutoff_iso,
        limit,
    )
    return [dict(r) for r in rows]


async def update_event_status(
    conn: Any,
    event_id: str,
    new_status: str,
    confirmed_at: datetime | None = None,
) -> None:
    """Update the status (and optionally confirmed_at) of an event."""
    if confirmed_at is not None:
        await conn.execute(
            """
            UPDATE events
               SET status = $1, confirmed_at = $2
             WHERE id = $3
            """,
            new_status,
            confirmed_at.isoformat(),
            event_id,
        )
    elif new_status == "confirmed":
        await conn.execute(
            """
            UPDATE events
               SET status = $1, confirmed_at = $2
             WHERE id = $3
            """,
            new_status,
            datetime.now(timezone.utc).isoformat(),
            event_id,
        )
    else:
        await conn.execute(
            "UPDATE events SET status = $1 WHERE id = $2",
            new_status,
            event_id,
        )


async def update_event_finality(
    conn: Any,
    event_id: str,
    depth: int,
) -> None:
    """Update the finality_depth for an event."""
    await conn.execute(
        "UPDATE events SET finality_depth = $1 WHERE id = $2",
        depth,
        event_id,
    )


async def fetch_event_endpoint_ids(
    conn: Any,
    event_id: str,
) -> list[str]:
    """Return all endpoint IDs linked to an event via the join table."""
    rows = await conn.fetch(
        "SELECT endpoint_id FROM event_endpoints WHERE event_id = $1",
        event_id,
    )
    return [row["endpoint_id"] for row in rows]
