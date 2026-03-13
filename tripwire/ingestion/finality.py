"""Block finality tracking via raw JSON-RPC calls (Goldsky Edge)."""

import httpx
import structlog

from tripwire.config.settings import settings
from tripwire.types.models import (
    FINALITY_DEPTHS,
    ChainId,
    ERC3009Transfer,
    FinalityStatus,
)

logger = structlog.get_logger(__name__)

# ── Shared httpx client (created lazily, no singleton gymnastics) ──
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


def get_rpc_url(chain_id: ChainId) -> str:
    """Return the configured Goldsky Edge RPC URL for a chain."""
    urls = {
        ChainId.ETHEREUM: settings.ethereum_rpc_url,
        ChainId.BASE: settings.base_rpc_url,
        ChainId.ARBITRUM: settings.arbitrum_rpc_url,
    }
    url = urls.get(chain_id, "")
    if not url:
        raise ValueError(f"No RPC URL configured for chain {chain_id}")
    return url


async def get_block_number(
    chain_id: ChainId,
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


async def check_finality(
    transfer: ERC3009Transfer,
    current_block: int | None = None,
    client: httpx.AsyncClient | None = None,
) -> FinalityStatus:
    """Check whether a transfer has reached finality.

    If current_block is not provided, it will be fetched via RPC.
    """
    if current_block is None:
        current_block = await get_block_number(transfer.chain_id, client=client)

    required = FINALITY_DEPTHS[transfer.chain_id]
    confirmations = max(0, current_block - transfer.block_number)
    is_finalized = confirmations >= required

    status = FinalityStatus(
        tx_hash=transfer.tx_hash,
        chain_id=transfer.chain_id,
        block_number=transfer.block_number,
        confirmations=confirmations,
        required_confirmations=required,
        is_finalized=is_finalized,
        finalized_at=current_block if is_finalized else None,
    )

    logger.debug(
        "finality_check",
        tx_hash=transfer.tx_hash,
        confirmations=confirmations,
        required=required,
        finalized=is_finalized,
    )
    return status
