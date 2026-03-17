"""Generic ABI decoder wrapper.

Wraps the existing ``decode_event_with_abi`` into the ``Decoder`` protocol.
"""

from __future__ import annotations

from typing import Any

from tripwire.ingestion.decoder import _parse_topics
from tripwire.ingestion.generic_decoder import decode_event_with_abi
from tripwire.ingestion.decoders.protocol import DecodedEvent


class AbiGenericDecoder:
    """Decoder for any EVM event given its ABI fragment."""

    def __init__(self, abi_fragment: list[dict[str, Any]]) -> None:
        self._abi = abi_fragment

    @property
    def name(self) -> str:
        return "abi_generic"

    def can_decode(self, raw_log: dict[str, Any]) -> bool:
        topics = _parse_topics(raw_log.get("topics", []))
        return len(topics) > 0

    def decode(self, raw_log: dict[str, Any]) -> DecodedEvent:
        decoded = decode_event_with_abi(raw_log, self._abi)
        topics = _parse_topics(raw_log.get("topics", []))
        topic0 = topics[0].lower() if topics else ""

        # Find first address-like field for identity resolution
        identity_address = None
        for key, val in decoded.items():
            if key.startswith("_"):
                continue
            if isinstance(val, str) and len(val) == 42 and val.startswith("0x"):
                identity_address = val
                break

        return DecodedEvent(
            tx_hash=decoded.get("_tx_hash", ""),
            block_number=decoded.get("_block_number", 0),
            block_hash=decoded.get("_block_hash", ""),
            log_index=decoded.get("_log_index", 0),
            chain_id=decoded.get("_chain_id"),
            contract_address=decoded.get("_address", ""),
            topic0=topic0,
            fields=decoded,
            raw_log=raw_log,
            decoder_name=self.name,
            identity_address=identity_address,
        )
