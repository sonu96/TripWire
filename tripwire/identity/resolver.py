"""ERC-8004 identity resolution for onchain AI agents.

Resolution flow (ERC-721 based IdentityRegistry):
1. balanceOf(senderAddress) — check if sender has an ERC-8004 identity
2. tokenOfOwnerByIndex(senderAddress, 0) — get agentId
3. tokenURI(agentId) — get agent URI
4. getMetadata(agentId, "agentClass") — get agent class
5. getSummary(agentId, []) on ReputationRegistry — get reputation
"""

import time
from typing import Protocol, runtime_checkable

import httpx
from eth_abi import decode, encode

import structlog

from tripwire.config.settings import Settings
from tripwire.types.models import AgentIdentity

logger = structlog.get_logger()

# ── IdentityRegistry function selectors (ERC-721 based) ─────────
# balanceOf(address) → uint256
BALANCE_OF_SELECTOR = "0x70a08231"
# tokenOfOwnerByIndex(address, uint256) → uint256
TOKEN_OF_OWNER_SELECTOR = "0x2f745c59"
# tokenURI(uint256) → string
TOKEN_URI_SELECTOR = "0xc87b56dd"
# ownerOf(uint256) → address
OWNER_OF_SELECTOR = "0x6352211e"
# getMetadata(uint256, string) → bytes
GET_METADATA_SELECTOR = "0xcb4799f2"

# ── ReputationRegistry function selectors ────────────────────────
# getSummary(uint256, address[]) → aggregate feedback
GET_SUMMARY_SELECTOR = "0x8ee8febc"

# ── Cache ────────────────────────────────────────────────────────

_CACHE_TTL = 300  # 5 minutes


class _CacheEntry:
    __slots__ = ("value", "expires_at")

    def __init__(self, value: AgentIdentity | None, ttl: int = _CACHE_TTL) -> None:
        self.value = value
        self.expires_at = time.monotonic() + ttl


# ── Protocol ─────────────────────────────────────────────────────


@runtime_checkable
class IdentityResolver(Protocol):
    async def resolve(self, address: str, chain_id: int) -> AgentIdentity | None: ...


# ── Chain ID → RPC URL helper ────────────────────────────────────

_CHAIN_RPC_KEYS: dict[int, str] = {
    1: "ethereum_rpc_url",
    8453: "base_rpc_url",
    42161: "arbitrum_rpc_url",
}


def _rpc_url_for(settings: Settings, chain_id: int) -> str:
    key = _CHAIN_RPC_KEYS.get(chain_id)
    if key is None:
        raise ValueError(f"Unsupported chain_id: {chain_id}")
    return getattr(settings, key)


def _cache_key(address: str, chain_id: int) -> str:
    return f"{chain_id}:{address.lower()}"


# ── ERC8004Resolver ──────────────────────────────────────────────


class ERC8004Resolver:
    """Resolves agent identities from the ERC-8004 onchain registry.

    Uses the real deployed IdentityRegistry (ERC-721) and ReputationRegistry
    contracts. Same CREATE2 address on all supported chains.
    """

    def __init__(self, settings: Settings) -> None:
        self._identity_registry = settings.erc8004_identity_registry
        self._reputation_registry = settings.erc8004_reputation_registry
        self._settings = settings
        self._client = httpx.AsyncClient(timeout=10)
        self._cache: dict[str, _CacheEntry] = {}

    async def _eth_call(self, rpc_url: str, to: str, data: str) -> str | None:
        """Send an eth_call and return the hex result, or None on failure."""
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
        }
        try:
            resp = await self._client.post(rpc_url, json=payload)
            resp.raise_for_status()
            body = resp.json()
            result = body.get("result")
            if not result or result == "0x" or len(result) < 66:
                return None
            return result
        except Exception:
            logger.warning("eth_call_failed", to=to, rpc_url=rpc_url)
            return None

    async def resolve(self, address: str, chain_id: int) -> AgentIdentity | None:
        """Resolve an ERC-8004 agent identity, returning None if unregistered."""
        key = _cache_key(address, chain_id)

        entry = self._cache.get(key)
        if entry is not None and entry.expires_at > time.monotonic():
            return entry.value

        rpc_url = _rpc_url_for(self._settings, chain_id)
        identity = await self._resolve_onchain(address, rpc_url)
        self._cache[key] = _CacheEntry(identity)
        return identity

    async def _resolve_onchain(self, address: str, rpc_url: str) -> AgentIdentity | None:
        registry = self._identity_registry

        # 1) balanceOf(address) — check if sender owns an ERC-8004 NFT
        balance_data = BALANCE_OF_SELECTOR + encode(["address"], [address]).hex()
        balance_result = await self._eth_call(rpc_url, registry, balance_data)
        if balance_result is None:
            return None

        try:
            (balance,) = decode(["uint256"], bytes.fromhex(balance_result[2:]))
        except Exception:
            logger.warning("decode_balance_failed", address=address)
            return None

        if balance == 0:
            return None

        # 2) tokenOfOwnerByIndex(address, 0) — get first agentId
        token_data = TOKEN_OF_OWNER_SELECTOR + encode(
            ["address", "uint256"], [address, 0]
        ).hex()
        token_result = await self._eth_call(rpc_url, registry, token_data)
        if token_result is None:
            return None

        try:
            (agent_id,) = decode(["uint256"], bytes.fromhex(token_result[2:]))
        except Exception:
            logger.warning("decode_agent_id_failed", address=address)
            return None

        # 3) tokenURI(agentId) — get agent URI
        uri_data = TOKEN_URI_SELECTOR + encode(["uint256"], [agent_id]).hex()
        uri_result = await self._eth_call(rpc_url, registry, uri_data)
        agent_uri = ""
        if uri_result:
            try:
                (agent_uri,) = decode(["string"], bytes.fromhex(uri_result[2:]))
            except Exception:
                logger.warning("decode_token_uri_failed", agent_id=agent_id)

        # 4) getMetadata(agentId, "agentClass") — get agent class
        meta_data = GET_METADATA_SELECTOR + encode(
            ["uint256", "string"], [agent_id, "agentClass"]
        ).hex()
        meta_result = await self._eth_call(rpc_url, registry, meta_data)
        agent_class = "unknown"
        if meta_result:
            try:
                (raw_bytes,) = decode(["bytes"], bytes.fromhex(meta_result[2:]))
                agent_class = raw_bytes.decode("utf-8").strip("\x00")
            except Exception:
                logger.warning("decode_agent_class_failed", agent_id=agent_id)

        # 4b) getMetadata(agentId, "capabilities") — get capabilities
        cap_data = GET_METADATA_SELECTOR + encode(
            ["uint256", "string"], [agent_id, "capabilities"]
        ).hex()
        cap_result = await self._eth_call(rpc_url, registry, cap_data)
        capabilities: list[str] = []
        if cap_result:
            try:
                (raw_bytes,) = decode(["bytes"], bytes.fromhex(cap_result[2:]))
                cap_str = raw_bytes.decode("utf-8").strip("\x00")
                if cap_str:
                    capabilities = [c.strip() for c in cap_str.split(",") if c.strip()]
            except Exception:
                logger.warning("decode_capabilities_failed", agent_id=agent_id)

        # 5) ownerOf(agentId) — get deployer (token minter/owner)
        owner_data = OWNER_OF_SELECTOR + encode(["uint256"], [agent_id]).hex()
        owner_result = await self._eth_call(rpc_url, registry, owner_data)
        deployer = address
        if owner_result:
            try:
                (deployer,) = decode(["address"], bytes.fromhex(owner_result[2:]))
            except Exception:
                logger.warning("decode_owner_failed", agent_id=agent_id)

        # 6) getSummary(agentId, []) on ReputationRegistry — get reputation
        reputation_score = 50.0
        summary_data = GET_SUMMARY_SELECTOR + encode(
            ["uint256", "address[]"], [agent_id, []]
        ).hex()
        summary_result = await self._eth_call(
            rpc_url, self._reputation_registry, summary_data
        )
        if summary_result:
            try:
                # getSummary returns aggregate data; extract the first uint256 as raw score
                # Score is in basis points (0-10000) → convert to 0-100
                decoded = decode(["uint256"], bytes.fromhex(summary_result[2:66]))
                raw_score = decoded[0]
                reputation_score = min(raw_score / 100.0, 100.0)
            except Exception:
                logger.warning("decode_reputation_failed", agent_id=agent_id)

        # registered_at: use agentId as a proxy (sequential token ID ~ registration order)
        # In production, this would come from the Transfer event block timestamp
        registered_at = agent_id

        return AgentIdentity(
            address=address.lower(),
            agent_class=agent_class,
            deployer=deployer,
            capabilities=capabilities,
            reputation_score=reputation_score,
            registered_at=registered_at,
            metadata={"agent_id": agent_id, "agent_uri": agent_uri},
        )

    async def close(self) -> None:
        await self._client.aclose()


# ── MockResolver ─────────────────────────────────────────────────

_MOCK_AGENTS: list[dict] = [
    {
        "address": "0x0000000000000000000000000000000000000001",
        "agent_class": "trading-bot",
        "deployer": "0xdeaDDeADDEaDdeaDdEAddEADDEAdDeadDEADDEaD",
        "capabilities": ["swap", "limit-order", "portfolio-rebalance"],
        "reputation_score": 85.0,
        "registered_at": 1738108800,
        "metadata": {"agent_id": 1, "agent_uri": "https://example.com/agents/trading-bot"},
    },
    {
        "address": "0x0000000000000000000000000000000000000002",
        "agent_class": "data-oracle",
        "deployer": "0xcafecafecafecafecafecafecafecafecafecafe",
        "capabilities": ["price-feed", "data-aggregation"],
        "reputation_score": 92.0,
        "registered_at": 1738195200,
        "metadata": {"agent_id": 2, "agent_uri": "https://example.com/agents/data-oracle"},
    },
    {
        "address": "0x0000000000000000000000000000000000000003",
        "agent_class": "payment-agent",
        "deployer": "0xbeefbeefbeefbeefbeefbeefbeefbeefbeefbeef",
        "capabilities": ["transfer", "batch-pay", "recurring-pay"],
        "reputation_score": 78.0,
        "registered_at": 1738281600,
        "metadata": {"agent_id": 3, "agent_uri": "https://example.com/agents/payment-agent"},
    },
]


class MockResolver:
    """In-memory identity resolver for development and testing."""

    def __init__(self) -> None:
        self._identities: dict[str, AgentIdentity] = {}
        for agent in _MOCK_AGENTS:
            identity = AgentIdentity(**agent)
            self._identities[identity.address.lower()] = identity

    async def resolve(self, address: str, chain_id: int) -> AgentIdentity | None:
        return self._identities.get(address.lower())

    def add_identity(self, identity: AgentIdentity) -> None:
        self._identities[identity.address.lower()] = identity

    def remove_identity(self, address: str) -> bool:
        return self._identities.pop(address.lower(), None) is not None


# ── Factory ──────────────────────────────────────────────────────


def create_resolver(settings: Settings) -> IdentityResolver:
    """Create the appropriate resolver based on APP_ENV."""
    if settings.app_env == "development":
        logger.info("identity_resolver", mode="mock")
        return MockResolver()
    logger.info("identity_resolver", mode="erc8004")
    return ERC8004Resolver(settings)
