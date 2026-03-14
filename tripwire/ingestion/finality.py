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
