"""ERC-3009 event decoder.

ERC-3009's transferWithAuthorization is a *function* call, not an event.
The actual events emitted on-chain are:

  1. Transfer(address indexed from, address indexed to, uint256 value)
  2. AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce)

Both events appear in the same transaction when transferWithAuthorization
is called. This module can decode both event types and combines them into
a single ERC3009Transfer model when given a full transaction's logs.
"""

from typing import Any

import structlog
from eth_abi import decode

from tripwire.types.models import USDC_CONTRACTS, ChainId, ERC3009Transfer

logger = structlog.get_logger(__name__)

# keccak256("AuthorizationUsed(address,bytes32)")
AUTHORIZATION_USED_TOPIC = (
    "0x98de503528ee59b575ef0c0a2576a82497bfc029"
    "a5685b209e9ec333479b10a5"
)

# keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = (
    "0xddf252ad1be2c89b69c2b068fc378daa952ba7f1"
    "63c4a11628f55a4df523b3ef"
)

# Reversed lookup: contract address (lowercased) -> chain_id
_CONTRACT_TO_CHAIN: dict[str, ChainId] = {
    addr.lower(): chain_id for chain_id, addr in USDC_CONTRACTS.items()
}


def decode_authorization_used(raw_log: dict[str, Any]) -> dict[str, str]:
    """Decode an AuthorizationUsed log.

    AuthorizationUsed(address indexed authorizer, bytes32 indexed nonce)
    - topic[1]: authorizer (address, zero-padded to 32 bytes)
    - topic[2]: nonce (bytes32)
    - data: empty
    """
    topics = raw_log["topics"]
    authorizer = _address_from_topic(topics[1])
    nonce = topics[2]  # already a bytes32 hex string
    return {"authorizer": authorizer, "nonce": nonce}


def decode_transfer_log(raw_log: dict[str, Any]) -> dict[str, Any]:
    """Decode an ERC-20 Transfer log.

    Transfer(address indexed from, address indexed to, uint256 value)
    - topic[1]: from address
    - topic[2]: to address
    - data: value (uint256)
    """
    topics = raw_log["topics"]
    from_address = _address_from_topic(topics[1])
    to_address = _address_from_topic(topics[2])

    data_bytes = bytes.fromhex(raw_log["data"].removeprefix("0x"))
    (value,) = decode(["uint256"], data_bytes)

    return {"from_address": from_address, "to_address": to_address, "value": value}


def decode_erc3009_from_logs(
    logs: list[dict[str, Any]],
    chain_id: ChainId | None = None,
) -> ERC3009Transfer:
    """Decode an ERC-3009 transfer from a transaction's logs.

    Expects logs from a single transaction that contain both a Transfer and
    an AuthorizationUsed event from the same USDC contract. This confirms
    the transfer was an ERC-3009 transferWithAuthorization call.

    Args:
        logs: All logs from a single transaction (or a filtered subset
              containing at least the Transfer + AuthorizationUsed pair).
        chain_id: If known; otherwise derived from the contract address.
    """
    transfer_log = None
    auth_log = None

    for log in logs:
        topics = log.get("topics", [])
        if not topics:
            continue
        topic0 = topics[0].lower()
        address = log.get("address", "").lower()

        # Only consider logs from known USDC contracts
        if address not in _CONTRACT_TO_CHAIN:
            continue

        if topic0 == TRANSFER_TOPIC.lower():
            transfer_log = log
        elif topic0 == AUTHORIZATION_USED_TOPIC.lower():
            auth_log = log

    if transfer_log is None:
        raise ValueError("No Transfer event found in transaction logs")
    if auth_log is None:
        raise ValueError("No AuthorizationUsed event found in transaction logs")

    contract = transfer_log["address"].lower()
    resolved_chain_id = chain_id or _resolve_chain_id(transfer_log, contract)
    _validate_contract(contract, resolved_chain_id)

    transfer_data = decode_transfer_log(transfer_log)
    auth_data = decode_authorization_used(auth_log)

    block_number = _to_int(transfer_log["blockNumber"])
    log_index = _to_int(transfer_log["logIndex"])
    timestamp = _to_int(transfer_log.get("timestamp", 0))

    result = ERC3009Transfer(
        chain_id=resolved_chain_id,
        tx_hash=transfer_log["transactionHash"],
        block_number=block_number,
        block_hash=transfer_log["blockHash"],
        log_index=log_index,
        from_address=transfer_data["from_address"],
        to_address=transfer_data["to_address"],
        value=str(transfer_data["value"]),
        authorizer=auth_data["authorizer"],
        valid_after=0,  # not available from events, set by caller if needed
        valid_before=0,  # not available from events, set by caller if needed
        nonce=auth_data["nonce"],
        token=contract,
        timestamp=timestamp,
    )

    logger.debug(
        "decoded_erc3009",
        tx_hash=result.tx_hash,
        from_addr=result.from_address,
        to_addr=result.to_address,
        value=result.value,
        authorizer=auth_data["authorizer"],
    )
    return result


def decode_transfer_event(raw_log: dict[str, Any]) -> ERC3009Transfer:
    """Decode a Goldsky-decoded row into an ERC3009Transfer.

    This handles rows produced by the Goldsky pipeline SQL transform, which
    JOINs Transfer and AuthorizationUsed events from the same transaction.
    The row includes:
      - decoded: dict with authorizer, nonce (from AuthorizationUsed)
      - transfer: dict with from_address, to_address, value (from Transfer)

    If the ``transfer`` key is missing (legacy rows or AuthorizationUsed-only
    pipelines), falls back to empty to_address/value with a warning.

    For raw undecoded logs, use decode_erc3009_from_logs() instead.

    Expected raw_log keys (from Goldsky decoded output):
        - transaction_hash: tx hash
        - block_number: int
        - block_hash: block hash
        - log_index: int
        - block_timestamp: int
        - decoded: dict with authorizer, nonce
        - transfer: dict with from_address, to_address, value (optional)
        - address: USDC contract (if present)
        - chain_id: chain id (int)
    """
    # Normalize Goldsky field names: _gs_log_decode produces "from"/"to" keys
    # but downstream code expects "from_address"/"to_address"
    if "from" in raw_log and "from_address" not in raw_log:
        raw_log["from_address"] = raw_log["from"]
    if "to" in raw_log and "to_address" not in raw_log:
        raw_log["to_address"] = raw_log["to"]

    decoded = raw_log.get("decoded", {})
    transfer = raw_log.get("transfer", {})
    contract = raw_log.get("address", "").lower()
    chain_id = _resolve_chain_id(raw_log, contract)

    if contract:
        _validate_contract(contract, chain_id)

    block_number = _to_int(raw_log.get("block_number", 0))
    log_index = _to_int(raw_log.get("log_index", 0))
    timestamp = _to_int(raw_log.get("block_timestamp", 0))

    authorizer = decoded.get("authorizer", "")
    nonce = decoded.get("nonce", "0x" + "00" * 32)

    # Extract Transfer fields from the joined row when available
    from_address = transfer.get("from_address", "") or authorizer
    to_address = transfer.get("to_address", "")
    value = str(transfer.get("value", "0"))

    if not to_address:
        logger.warning(
            "transfer_missing_to_address",
            tx_hash=raw_log.get("transaction_hash", ""),
            msg="No Transfer data in row; endpoint matching will fail",
        )

    return ERC3009Transfer(
        chain_id=chain_id,
        tx_hash=raw_log.get("transaction_hash", ""),
        block_number=block_number,
        block_hash=raw_log.get("block_hash", ""),
        log_index=log_index,
        from_address=from_address,
        to_address=to_address,
        value=value,
        authorizer=authorizer,
        valid_after=0,
        valid_before=0,
        nonce=nonce,
        token=contract or USDC_CONTRACTS.get(chain_id, ""),
        timestamp=timestamp,
    )


# ── Internal helpers ──────────────────────────────────────────


def _resolve_chain_id(raw_log: dict[str, Any], contract: str) -> ChainId:
    """Resolve chain_id from the raw log or by contract address."""
    if "chain_id" in raw_log:
        return ChainId(raw_log["chain_id"])
    if contract in _CONTRACT_TO_CHAIN:
        return _CONTRACT_TO_CHAIN[contract]
    raise ValueError(f"Cannot determine chain_id for contract {contract}")


def _validate_contract(contract: str, chain_id: ChainId) -> None:
    """Verify the contract address matches the expected USDC contract for the chain."""
    expected = USDC_CONTRACTS[chain_id].lower()
    if contract != expected:
        raise ValueError(
            f"Contract {contract} does not match expected USDC "
            f"contract {expected} for chain {chain_id}"
        )


def _address_from_topic(topic: str) -> str:
    """Extract a checksumless address from a 32-byte hex topic."""
    raw = topic.removeprefix("0x")
    return f"0x{raw[-40:]}"


def _to_int(val: int | str) -> int:
    """Convert a value that may be hex-encoded to an int."""
    if isinstance(val, int):
        return val
    if isinstance(val, str) and val.startswith("0x"):
        return int(val, 16)
    return int(val)
