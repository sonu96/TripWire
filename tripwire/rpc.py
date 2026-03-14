"""Shared JSON-RPC client for Goldsky Edge.

Consolidates all eth_call / eth_blockNumber logic into one place.
Used by finality, identity resolver, and reputation service.
"""

from __future__ import annotations

from typing import Any

import httpx
import structlog

from tripwire.config.settings import settings

logger = structlog.get_logger(__name__)

# ── Lazy singleton HTTP client ─────────────────────────────────
_http_client: httpx.AsyncClient | None = None


def _get_http_client() -> httpx.AsyncClient:
    """Return a module-level async HTTP client, creating it on first use."""
    global _http_client
    if _http_client is None:
        headers: dict[str, str] = {}
        edge_key = settings.goldsky_edge_api_key.get_secret_value()
        if edge_key:
            headers["Authorization"] = f"Bearer {edge_key}"
        _http_client = httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers=headers,
        )
    return _http_client


# ── RPC URL resolution ──────────────────────────────────────────

_RPC_URLS: dict[int, str] = {}


def _build_rpc_urls() -> dict[int, str]:
    """Build chain_id → RPC URL map from settings (lazy, cached)."""
    global _RPC_URLS
    if not _RPC_URLS:
        _RPC_URLS = {
            1: settings.ethereum_rpc_url,
            8453: settings.base_rpc_url,
            42161: settings.arbitrum_rpc_url,
        }
    return _RPC_URLS


def get_rpc_url(chain_id: int) -> str:
    """Return the configured Goldsky Edge RPC URL for a chain."""
    urls = _build_rpc_urls()
    url = urls.get(chain_id, "")
    if not url:
        raise ValueError(f"No RPC URL configured for chain {chain_id}")
    return url


# ── JSON-RPC primitives ────────────────────────────────────────


async def eth_call(
    chain_id: int,
    to: str,
    data: str,
    *,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Send an eth_call and return the hex result, or None on failure."""
    rpc_url = get_rpc_url(chain_id)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_call",
        "params": [{"to": to, "data": data}, "latest"],
    }
    http = client or _get_http_client()
    try:
        resp = await http.post(rpc_url, json=payload)
        resp.raise_for_status()
        body = resp.json()
        result = body.get("result")
        if not result or result == "0x" or len(result) < 66:
            return None
        return result
    except Exception:
        logger.warning("eth_call_failed", to=to, chain_id=chain_id)
        return None


async def eth_block_number(
    chain_id: int,
    *,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Fetch the latest block number from the chain via JSON-RPC."""
    rpc_url = get_rpc_url(chain_id)
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_blockNumber",
        "params": [],
        "id": 1,
    }
    http = client or _get_http_client()
    resp = await http.post(rpc_url, json=payload)
    resp.raise_for_status()

    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"RPC error on chain {chain_id}: {data['error']}")

    block_hex = data["result"]
    block_num = int(block_hex, 16)
    logger.debug("rpc_block_number", chain_id=chain_id, block=block_num)
    return block_num


async def close_rpc_client() -> None:
    """Close the shared HTTP client on shutdown."""
    global _http_client
    if _http_client is not None:
        await _http_client.aclose()
        _http_client = None
