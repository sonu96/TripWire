"""Session manager — Redis-backed session lifecycle with atomic budget operations.

Sessions provide a pre-authorized spending limit so that agents can make
multiple MCP tool calls without per-call x402 payment negotiation.  All state
lives in Redis (hash per session) and budget decrements use a Lua script for
atomicity.

For v1 sessions are free to create (gated only by SIWE auth).  The budget is a
server-side spending limit, not a prepayment.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

import structlog

from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

# Redis key prefix for session hashes
_SESSION_PREFIX = "session:"

# Lua script: atomic validate-and-decrement
# Returns:
#   -1  → session not found
#   -2  → session expired
#   -3  → insufficient budget
#   >=0 → new budget_remaining after decrement
_DECREMENT_LUA = """
local key = KEYS[1]
local cost = tonumber(ARGV[1])
local now  = tonumber(ARGV[2])

-- Check existence
if redis.call("EXISTS", key) == 0 then
    return -1
end

-- Check expiry
local expires_at = tonumber(redis.call("HGET", key, "expires_at"))
if expires_at and now > expires_at then
    return -2
end

-- Check budget
local remaining = tonumber(redis.call("HGET", key, "budget_remaining"))
if remaining < cost then
    return -3
end

-- Decrement
local new_remaining = remaining - cost
redis.call("HSET", key, "budget_remaining", new_remaining)
return new_remaining
"""


@dataclass
class SessionData:
    """Snapshot of a session's current state."""

    session_id: str
    wallet_address: str
    budget_total: int
    budget_remaining: int
    expires_at: float
    ttl_seconds: int
    chain_id: int
    reputation_score: float
    agent_class: str


class SessionManager:
    """Manages session lifecycle in Redis.

    Each session is a Redis hash at ``session:{session_id}`` with the fields:
    wallet_address, budget_total, budget_remaining, expires_at, ttl_seconds,
    chain_id, reputation_score, agent_class, created_at.
    """

    def __init__(self, redis) -> None:
        self._redis = redis
        self._decrement_sha: str | None = None

    async def register_lua_scripts(self) -> None:
        """Pre-load the Lua decrement script into Redis."""
        self._decrement_sha = await self._redis.script_load(_DECREMENT_LUA)
        logger.info("session_lua_scripts_registered")

    async def create(
        self,
        wallet_address: str,
        budget: int | None = None,
        ttl_seconds: int | None = None,
        chain_id: int | None = None,
        reputation_score: float = 0.0,
        agent_class: str = "unknown",
    ) -> SessionData:
        """Create a new session and store it in Redis.

        Parameters
        ----------
        wallet_address:
            Verified wallet address (from SIWE auth).
        budget:
            Budget in smallest USDC units (6 decimals).  Clamped to
            ``session_max_budget_usdc``.  Defaults to ``session_default_budget_usdc``.
        ttl_seconds:
            Session lifetime.  Clamped to ``session_max_ttl_seconds``.
            Defaults to ``session_default_ttl_seconds``.
        chain_id:
            Optional chain ID context.  Defaults to ``siwe_chain_id``.
        reputation_score:
            Cached reputation score from identity resolution.
        agent_class:
            Cached agent class from identity resolution.
        """
        session_id = secrets.token_urlsafe(24)

        effective_budget = max(
            1,
            min(
                budget if budget is not None else settings.session_default_budget_usdc,
                settings.session_max_budget_usdc,
            ),
        )
        effective_ttl = max(
            60,
            min(
                ttl_seconds if ttl_seconds is not None else settings.session_default_ttl_seconds,
                settings.session_max_ttl_seconds,
            ),
        )
        effective_chain_id = chain_id if chain_id is not None else settings.siwe_chain_id

        now = time.time()
        expires_at = now + effective_ttl

        key = f"{_SESSION_PREFIX}{session_id}"
        mapping = {
            "wallet_address": wallet_address,
            "budget_total": str(effective_budget),
            "budget_remaining": str(effective_budget),
            "expires_at": str(expires_at),
            "ttl_seconds": str(effective_ttl),
            "chain_id": str(effective_chain_id),
            "reputation_score": str(reputation_score),
            "agent_class": agent_class,
            "created_at": str(now),
        }

        pipe = self._redis.pipeline()
        pipe.hset(key, mapping=mapping)
        # Set Redis-level TTL slightly beyond session expiry for cleanup
        pipe.expire(key, effective_ttl + 60)
        await pipe.execute()

        logger.info(
            "session_created",
            session_id=session_id,
            wallet_address=wallet_address,
            budget=effective_budget,
            ttl_seconds=effective_ttl,
        )

        return SessionData(
            session_id=session_id,
            wallet_address=wallet_address,
            budget_total=effective_budget,
            budget_remaining=effective_budget,
            expires_at=expires_at,
            ttl_seconds=effective_ttl,
            chain_id=effective_chain_id,
            reputation_score=reputation_score,
            agent_class=agent_class,
        )

    async def get(self, session_id: str) -> SessionData | None:
        """Retrieve current session state.  Returns ``None`` if not found."""
        key = f"{_SESSION_PREFIX}{session_id}"
        data = await self._redis.hgetall(key)
        if not data:
            return None

        return SessionData(
            session_id=session_id,
            wallet_address=data["wallet_address"],
            budget_total=int(data["budget_total"]),
            budget_remaining=int(data["budget_remaining"]),
            expires_at=float(data["expires_at"]),
            ttl_seconds=int(data["ttl_seconds"]),
            chain_id=int(data["chain_id"]),
            reputation_score=float(data.get("reputation_score", "0.0")),
            agent_class=data.get("agent_class", "unknown"),
        )

    async def validate_and_decrement(self, session_id: str, cost: int) -> SessionData:
        """Atomically validate session and decrement budget.

        Raises
        ------
        SessionNotFound
            If the session does not exist in Redis.
        SessionExpired
            If the session's ``expires_at`` is in the past.
        InsufficientBudget
            If ``budget_remaining < cost``.
        """
        key = f"{_SESSION_PREFIX}{session_id}"
        now = time.time()

        if self._decrement_sha:
            result = await self._redis.evalsha(
                self._decrement_sha, 1, key, str(cost), str(now)
            )
        else:
            result = await self._redis.eval(
                _DECREMENT_LUA, 1, key, str(cost), str(now)
            )

        result = int(result)

        if result == -1:
            raise SessionNotFound(session_id)
        if result == -2:
            raise SessionExpired(session_id)
        if result == -3:
            raise InsufficientBudget(session_id, cost)

        # Fetch full session data after successful decrement
        session = await self.get(session_id)
        if session is None:
            raise SessionNotFound(session_id)

        return session

    async def refund(self, session_id: str, amount: int) -> int:
        """Refund *amount* back to the session's budget (e.g. on reputation gate failure).

        Returns the new ``budget_remaining``.
        """
        key = f"{_SESSION_PREFIX}{session_id}"
        new_remaining = await self._redis.hincrby(key, "budget_remaining", amount)
        logger.debug(
            "session_budget_refunded",
            session_id=session_id,
            refund=amount,
            new_remaining=new_remaining,
        )
        return int(new_remaining)

    async def close(self, session_id: str) -> SessionData | None:
        """Close a session and remove it from Redis.

        Returns the final state snapshot before deletion, or ``None`` if the
        session was already gone.
        """
        session = await self.get(session_id)
        if session is None:
            return None

        key = f"{_SESSION_PREFIX}{session_id}"
        await self._redis.delete(key)

        logger.info(
            "session_closed",
            session_id=session_id,
            wallet_address=session.wallet_address,
            budget_remaining=session.budget_remaining,
        )
        return session


# ---------------------------------------------------------------------------
# Session exceptions
# ---------------------------------------------------------------------------


class SessionError(Exception):
    """Base class for session errors."""

    def __init__(self, session_id: str, message: str) -> None:
        self.session_id = session_id
        super().__init__(message)


class SessionNotFound(SessionError):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id, f"Session not found: {session_id}")


class SessionExpired(SessionError):
    def __init__(self, session_id: str) -> None:
        super().__init__(session_id, f"Session expired: {session_id}")


class InsufficientBudget(SessionError):
    def __init__(self, session_id: str, cost: int) -> None:
        self.cost = cost
        super().__init__(session_id, f"Insufficient budget for cost {cost}: {session_id}")
