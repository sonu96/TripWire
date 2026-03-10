"""Tests for tripwire/ingestion/finality.py."""

import pytest

from tripwire.ingestion.finality import check_finality
from tripwire.types.models import ChainId, ERC3009Transfer

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
USDC_ETH = "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48"
USDC_ARB = "0xaf88d065e77c8cc2239327c5edb3a432268e5831"
TX_HASH = "0x" + "ff" * 32
BLOCK_HASH = "0x" + "ee" * 32
NONCE_HEX = "0x" + "ab" * 32
SENDER = "0x1234567890abcdef1234567890abcdef12345678"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


def _transfer(chain_id: ChainId, block_number: int = 100) -> ERC3009Transfer:
    token_map = {
        ChainId.BASE: USDC_BASE,
        ChainId.ETHEREUM: USDC_ETH,
        ChainId.ARBITRUM: USDC_ARB,
    }
    return ERC3009Transfer(
        chain_id=chain_id,
        tx_hash=TX_HASH,
        block_number=block_number,
        block_hash=BLOCK_HASH,
        log_index=0,
        from_address=SENDER,
        to_address=RECIPIENT,
        value="5000000",
        authorizer=SENDER,
        valid_after=0,
        valid_before=0,
        nonce=NONCE_HEX,
        token=token_map[chain_id],
        timestamp=1700000000,
    )


@pytest.mark.asyncio
async def test_finalized_on_base():
    transfer = _transfer(ChainId.BASE, block_number=100)
    status = await check_finality(transfer, current_block=103)

    assert status.is_finalized is True
    assert status.confirmations == 3
    assert status.required_confirmations == 3
    assert status.finalized_at == 103


@pytest.mark.asyncio
async def test_not_finalized_on_base():
    transfer = _transfer(ChainId.BASE, block_number=100)
    status = await check_finality(transfer, current_block=101)

    assert status.is_finalized is False
    assert status.confirmations == 1
    assert status.required_confirmations == 3
    assert status.finalized_at is None


@pytest.mark.asyncio
async def test_finalized_on_ethereum():
    transfer = _transfer(ChainId.ETHEREUM, block_number=100)
    status = await check_finality(transfer, current_block=112)

    assert status.is_finalized is True
    assert status.confirmations == 12
    assert status.required_confirmations == 12
    assert status.finalized_at == 112


@pytest.mark.asyncio
async def test_finalized_on_arbitrum():
    transfer = _transfer(ChainId.ARBITRUM, block_number=100)
    status = await check_finality(transfer, current_block=101)

    assert status.is_finalized is True
    assert status.confirmations == 1
    assert status.required_confirmations == 1
    assert status.finalized_at == 101


@pytest.mark.asyncio
async def test_zero_confirmations():
    transfer = _transfer(ChainId.BASE, block_number=100)
    status = await check_finality(transfer, current_block=100)

    assert status.is_finalized is False
    assert status.confirmations == 0
    assert status.finalized_at is None
