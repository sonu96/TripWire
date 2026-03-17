"""ERC-3009 decoder wrapper.

Wraps the existing ``decode_transfer_event`` into the ``Decoder`` protocol.
"""

from __future__ import annotations

from typing import Any

from tripwire.ingestion.decoder import (
    AUTHORIZATION_USED_TOPIC,
    TRANSFER_TOPIC,
    _parse_topics,
    decode_transfer_event,
)
from tripwire.ingestion.decoders.protocol import DecodedEvent


class ERC3009Decoder:
    """Decoder for ERC-3009 TransferWithAuthorization events."""

    @property
    def name(self) -> str:
        return "erc3009"

    def can_decode(self, raw_log: dict[str, Any]) -> bool:
        topics = _parse_topics(raw_log.get("topics", []))
        if not topics:
            return False
        t0 = topics[0].lower()
        return t0 in (
            AUTHORIZATION_USED_TOPIC.lower(),
            TRANSFER_TOPIC.lower(),
        )

    def decode(self, raw_log: dict[str, Any]) -> DecodedEvent:
        transfer = decode_transfer_event(raw_log)
        topics = _parse_topics(raw_log.get("topics", []))
        topic0 = topics[0].lower() if topics else ""

        return DecodedEvent(
            tx_hash=transfer.tx_hash,
            block_number=transfer.block_number,
            block_hash=transfer.block_hash,
            log_index=transfer.log_index,
            chain_id=transfer.chain_id.value if transfer.chain_id else None,
            contract_address=transfer.token,
            topic0=topic0,
            fields=transfer.model_dump(),
            raw_log=raw_log,
            decoder_name=self.name,
            typed_model=transfer,
            identity_address=transfer.authorizer or None,
            dedup_key=(
                f"{transfer.authorizer}:{transfer.nonce}"
                if transfer.authorizer
                else f"{transfer.tx_hash}:{transfer.log_index}"
            ),
            # C3: ERC-3009 is inherently a payment event
            payment_amount=transfer.value,
            payment_token=transfer.token,
            payment_from=transfer.from_address,
            payment_to=transfer.to_address,
        )
