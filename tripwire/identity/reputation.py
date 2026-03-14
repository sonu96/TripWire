"""Reputation scoring for ERC-8004 agents.

Uses the ReputationRegistry contract (0x8004BAa17C55a88189AE136b182e5fdA19dE9b63)
deployed via CREATE2 at the same address on all supported chains.
"""

import time

from eth_abi import decode, encode

import structlog

from tripwire.config.settings import Settings
from tripwire.rpc import eth_call

logger = structlog.get_logger(__name__)

# getSummary(uint256 agentId, address[] clientAddresses) → aggregate feedback
_GET_SUMMARY_SELECTOR = "0x8ee8febc"
_CACHE_TTL = 300  # 5 minutes
_DEFAULT_SCORE = 0.0

class _ScoreCacheEntry:
    __slots__ = ("score", "expires_at")

    def __init__(self, score: float) -> None:
        self.score = score
        self.expires_at = time.monotonic() + _CACHE_TTL


class ReputationService:
    """Query and cache reputation scores from the ERC-8004 ReputationRegistry."""

    def __init__(self, settings: Settings) -> None:
        self._registry = settings.erc8004_reputation_registry
        self._cache: dict[str, _ScoreCacheEntry] = {}

    async def get_reputation_score(self, agent_id: int, chain_id: int) -> float:
        """Return reputation score (0-100) for an agent ID. Defaults to 0 if unavailable."""
        key = f"{chain_id}:{agent_id}"

        entry = self._cache.get(key)
        if entry is not None and entry.expires_at > time.monotonic():
            return entry.score

        score = await self._fetch_score(agent_id, chain_id)
        self._cache[key] = _ScoreCacheEntry(score)
        return score

    async def _fetch_score(self, agent_id: int, chain_id: int) -> float:
        # getSummary(uint256 agentId, address[] clientAddresses)
        # Pass empty client list for aggregate score
        data = _GET_SUMMARY_SELECTOR + encode(
            ["uint256", "address[]"], [agent_id, []]
        ).hex()

        try:
            result = await eth_call(chain_id, self._registry, data)
            if not result:
                return _DEFAULT_SCORE
            # First uint256 in response is the aggregate score in basis points (0-10000)
            decoded_vals = decode(["uint256"], bytes.fromhex(result[2:66]))
            raw_score = decoded_vals[0]
            return min(raw_score / 100.0, 100.0)
        except Exception:
            logger.warning(
                "reputation_fetch_failed", agent_id=agent_id, chain_id=chain_id
            )
            return _DEFAULT_SCORE


# ── Convenience function ─────────────────────────────────────────

_service: ReputationService | None = None


async def get_reputation_score(
    agent_id: int, chain_id: int, *, settings: Settings | None = None
) -> float:
    """Module-level convenience function for one-off lookups."""
    global _service
    if _service is None:
        if settings is None:
            from tripwire.config.settings import settings as default_settings

            settings = default_settings
        _service = ReputationService(settings)
    return await _service.get_reputation_score(agent_id, chain_id)
