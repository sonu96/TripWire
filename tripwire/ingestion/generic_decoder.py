"""Generic ABI-driven EVM event decoder for dynamic triggers.

Decodes ANY EVM event given its ABI fragment, raw topics, and data.
Uses eth-abi for decoding. Returns a flat dict of field_name -> value
plus _-prefixed metadata fields.
"""

from __future__ import annotations

from typing import Any

import structlog
from eth_abi import decode

from tripwire.ingestion.decoder import _parse_topics, _to_int

logger = structlog.get_logger(__name__)


def decode_event_with_abi(
    raw_log: dict[str, Any],
    abi_fragment: list[dict[str, Any]],
) -> dict[str, Any]:
    """Decode a raw log using the provided ABI event fragment.

    Args:
        raw_log: Raw Goldsky log with topics, data, address, etc.
        abi_fragment: ABI array; uses the first item with type=="event".

    Returns:
        Dict with decoded fields by name + _-prefixed metadata.
    """
    event_abi = None
    for entry in abi_fragment:
        if entry.get("type") == "event":
            event_abi = entry
            break
    if event_abi is None:
        raise ValueError("No event ABI entry found in abi_fragment")

    topics = _parse_topics(raw_log.get("topics", []))
    raw_data = raw_log.get("data", "0x")
    data_bytes = bytes.fromhex(raw_data.removeprefix("0x")) if raw_data and raw_data != "0x" else b""

    inputs = event_abi.get("inputs", [])
    indexed_inputs = [inp for inp in inputs if inp.get("indexed", False)]
    non_indexed_inputs = [inp for inp in inputs if not inp.get("indexed", False)]

    decoded: dict[str, Any] = {}

    # Decode indexed parameters from topics[1:]
    for i, inp in enumerate(indexed_inputs):
        topic_idx = i + 1
        if topic_idx < len(topics):
            topic_hex = topics[topic_idx]
            typ = inp["type"]
            if typ == "address":
                decoded[inp["name"]] = f"0x{topic_hex.removeprefix('0x')[-40:]}"
            elif typ in ("bytes32", "bytes"):
                decoded[inp["name"]] = topic_hex
            elif typ.startswith("uint") or typ.startswith("int"):
                decoded[inp["name"]] = int(topic_hex, 16)
            elif typ == "bool":
                decoded[inp["name"]] = int(topic_hex, 16) != 0
            else:
                decoded[inp["name"]] = topic_hex

    # Decode non-indexed parameters from data
    if non_indexed_inputs and data_bytes:
        types = [inp["type"] for inp in non_indexed_inputs]
        try:
            values = decode(types, data_bytes)
            for inp, val in zip(non_indexed_inputs, values):
                if isinstance(val, bytes):
                    decoded[inp["name"]] = f"0x{val.hex()}"
                else:
                    decoded[inp["name"]] = val
        except Exception:
            logger.exception(
                "generic_decode_data_failed",
                tx_hash=raw_log.get("transaction_hash", ""),
                types=types,
            )

    # Attach metadata
    decoded["_tx_hash"] = raw_log.get("transaction_hash", "")
    decoded["_block_number"] = _to_int(raw_log.get("block_number", 0))
    decoded["_block_hash"] = raw_log.get("block_hash", "")
    decoded["_log_index"] = _to_int(raw_log.get("log_index", 0))
    decoded["_address"] = raw_log.get("address", "")
    decoded["_chain_id"] = raw_log.get("chain_id")

    return decoded
