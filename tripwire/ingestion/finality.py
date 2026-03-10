"""Block finality tracking via raw JSON-RPC calls."""

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

_RPC_URLS: dict[ChainId, str] = {
    ChainId.ETHEREUM: settings.ethereum_rpc_url,
    ChainId.BASE: settings.base_rpc_url,
    ChainId.ARBITRUM: settings.arbitrum_rpc_url,
}


async def get_block_number(chain_id: ChainId) -> int:
    """Fetch the latest block number from the chain via JSON-RPC."""
    rpc_url = _RPC_URLS[chain_id]
    payload = {
        "jsonrpc": "2.0",
        "method": "eth_blockNumber",
        "params": [],
        "id": 1,
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(rpc_url, json=payload, timeout=10.0)
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
) -> FinalityStatus:
    """Check whether a transfer has reached finality.

    If current_block is not provided, it will be fetched via RPC.
    """
    if current_block is None:
        current_block = await get_block_number(transfer.chain_id)

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
