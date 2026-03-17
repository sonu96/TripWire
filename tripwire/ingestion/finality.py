"""Block finality tracking via raw JSON-RPC calls (Goldsky Edge)."""

import httpx
import structlog

from tripwire.rpc import eth_block_number, get_rpc_url  # noqa: F401 — re-export for callers
from tripwire.types.models import (
    FINALITY_DEPTHS,
    ChainId,
    ERC3009Transfer,
    FinalityStatus,
)

logger = structlog.get_logger(__name__)


async def get_block_number(
    chain_id: ChainId,
    client: httpx.AsyncClient | None = None,
) -> int:
    """Fetch the latest block number from the chain via JSON-RPC."""
    return await eth_block_number(chain_id, client=client)


async def get_block_hash(
    chain_id: ChainId | int,
    block_number: int,
    client: httpx.AsyncClient | None = None,
) -> str | None:
    """Fetch the canonical block hash for a given block number via JSON-RPC.

    Returns the block hash as a hex string, or None on failure.
    Used by the finality poller for reorg detection.
    """
    from tripwire.rpc import get_rpc_url, _get_http_client

    chain_id_int = chain_id.value if hasattr(chain_id, "value") else chain_id
    rpc_url = get_rpc_url(chain_id_int)
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "eth_getBlockByNumber",
        "params": [hex(block_number), False],
    }
    http = client or _get_http_client()
    try:
        resp = await http.post(rpc_url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        result = data.get("result")
        if result and isinstance(result, dict):
            return result.get("hash")
        return None
    except Exception:
        logger.warning(
            "get_block_hash_failed",
            chain_id=chain_id_int,
            block_number=block_number,
        )
        return None


async def check_finality(
    transfer: ERC3009Transfer,
    current_block: int | None = None,
    client: httpx.AsyncClient | None = None,
    required_depth: int | None = None,
) -> FinalityStatus:
    """Check whether a transfer has reached finality.

    If *current_block* is not provided, it will be fetched via RPC.
    If *required_depth* is provided it overrides the chain-default
    depth from ``FINALITY_DEPTHS``.
    """
    if current_block is None:
        current_block = await get_block_number(transfer.chain_id, client=client)

    required = required_depth if required_depth is not None else FINALITY_DEPTHS[transfer.chain_id]
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
