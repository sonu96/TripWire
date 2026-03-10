"""Tests for tripwire/ingestion/decoder.py."""

import pytest
from eth_abi import encode

from tripwire.ingestion.decoder import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    decode_authorization_used,
    decode_erc3009_from_logs,
    decode_transfer_event,
    decode_transfer_log,
)
from tripwire.types.models import ChainId

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SENDER = "0x1234567890abcdef1234567890abcdef12345678"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
AUTHORIZER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NONCE_BYTES32 = "0x" + "ab" * 32
TX_HASH = "0x" + "ff" * 32
BLOCK_HASH = "0x" + "ee" * 32


def _pad_address(addr: str) -> str:
    return "0x" + "0" * 24 + addr[2:]


def _encode_uint256(value: int) -> str:
    return "0x" + encode(["uint256"], [value]).hex()


def _make_transfer_log(
    from_addr: str = SENDER,
    to_addr: str = RECIPIENT,
    value: int = 5_000_000,
    contract: str = USDC_BASE,
) -> dict:
    return {
        "address": contract,
        "topics": [
            TRANSFER_TOPIC,
            _pad_address(from_addr),
            _pad_address(to_addr),
        ],
        "data": _encode_uint256(value),
        "transactionHash": TX_HASH,
        "blockNumber": 100,
        "blockHash": BLOCK_HASH,
        "logIndex": 3,
        "timestamp": 1700000000,
    }


def _make_auth_log(
    authorizer: str = AUTHORIZER,
    nonce: str = NONCE_BYTES32,
    contract: str = USDC_BASE,
) -> dict:
    return {
        "address": contract,
        "topics": [
            AUTHORIZATION_USED_TOPIC,
            _pad_address(authorizer),
            nonce,
        ],
        "data": "0x",
        "transactionHash": TX_HASH,
        "blockNumber": 100,
        "blockHash": BLOCK_HASH,
        "logIndex": 4,
        "timestamp": 1700000000,
    }


def test_decode_authorization_used():
    log = _make_auth_log()
    result = decode_authorization_used(log)

    assert result["authorizer"] == "0x" + AUTHORIZER[2:]
    assert result["nonce"] == NONCE_BYTES32


def test_decode_transfer_log():
    log = _make_transfer_log(value=10_000_000)
    result = decode_transfer_log(log)

    assert result["from_address"] == "0x" + SENDER[2:]
    assert result["to_address"] == "0x" + RECIPIENT[2:]
    assert result["value"] == 10_000_000


def test_decode_transfer_event(sample_raw_log):
    transfer = decode_transfer_event(sample_raw_log)

    assert transfer.chain_id == ChainId.BASE
    assert transfer.tx_hash == sample_raw_log["transaction_hash"]
    assert transfer.block_number == 100
    assert transfer.block_hash == BLOCK_HASH
    assert transfer.log_index == 3
    assert transfer.authorizer == AUTHORIZER
    assert transfer.nonce == NONCE_BYTES32
    assert transfer.token == USDC_BASE.lower()
    assert transfer.timestamp == 1700000000


def test_decode_erc3009_from_logs():
    transfer_log = _make_transfer_log()
    auth_log = _make_auth_log()
    logs = [transfer_log, auth_log]

    result = decode_erc3009_from_logs(logs, chain_id=ChainId.BASE)

    assert result.chain_id == ChainId.BASE
    assert result.tx_hash == TX_HASH
    assert result.block_number == 100
    assert result.from_address == "0x" + SENDER[2:]
    assert result.to_address == "0x" + RECIPIENT[2:]
    assert result.value == "5000000"
    assert result.authorizer == "0x" + AUTHORIZER[2:]
    assert result.nonce == NONCE_BYTES32
    assert result.token == USDC_BASE.lower()


def test_decode_erc3009_missing_transfer():
    auth_log = _make_auth_log()
    with pytest.raises(ValueError, match="No Transfer event found"):
        decode_erc3009_from_logs([auth_log], chain_id=ChainId.BASE)


def test_decode_erc3009_missing_auth():
    transfer_log = _make_transfer_log()
    with pytest.raises(ValueError, match="No AuthorizationUsed event found"):
        decode_erc3009_from_logs([transfer_log], chain_id=ChainId.BASE)
