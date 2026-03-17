"""Decoder protocol and unified DecodedEvent envelope."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


@dataclass
class DecodedEvent:
    """Unified envelope for a decoded onchain event.

    Both the ERC-3009 and generic ABI decoders produce this.
    """

    tx_hash: str
    block_number: int
    block_hash: str
    log_index: int
    chain_id: int | None
    contract_address: str
    topic0: str
    fields: dict[str, Any]
    raw_log: dict[str, Any]
    decoder_name: str
    typed_model: Any | None = None
    identity_address: str | None = None
    dedup_key: str | None = None
    # C3: Payment metadata extracted by decoders that handle payment events
    payment_amount: str | None = None  # Smallest unit (e.g. USDC 6 decimals)
    payment_token: str | None = None  # Token contract address
    payment_from: str | None = None
    payment_to: str | None = None


@runtime_checkable
class Decoder(Protocol):
    """Protocol that all event decoders must satisfy."""

    @property
    def name(self) -> str: ...

    def can_decode(self, raw_log: dict[str, Any]) -> bool: ...

    def decode(self, raw_log: dict[str, Any]) -> DecodedEvent: ...
