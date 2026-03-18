"""Generic ABI decoder wrapper.

Wraps the existing ``decode_event_with_abi`` into the ``Decoder`` protocol.
"""

from __future__ import annotations

from typing import Any

from tripwire.ingestion.decoder import _parse_topics
from tripwire.ingestion.generic_decoder import decode_event_with_abi
from tripwire.ingestion.decoders.protocol import DecodedEvent


_AMOUNT_NAMES = {"value", "amount", "_value", "_amount"}
_FROM_NAMES = {"from", "sender", "_from"}
_TO_NAMES = {"to", "recipient", "_to"}


def _is_address(val: Any) -> bool:
    """Return True if *val* looks like a 0x-prefixed Ethereum address."""
    return isinstance(val, str) and len(val) == 42 and val.startswith("0x")


def _extract_payment_fields(
    decoded: dict[str, Any],
    raw_log: dict[str, Any],
) -> tuple[str | None, str | None, str | None, str | None]:
    """Best-effort extraction of payment metadata from decoded event fields.

    Returns (payment_amount, payment_token, payment_from, payment_to).
    Any field that cannot be matched remains None.
    """
    payment_amount: str | None = None
    payment_token: str | None = None
    payment_from: str | None = None
    payment_to: str | None = None

    for key, val in decoded.items():
        key_lower = key.lower()

        # Amount: look for value/amount fields with numeric-like content
        if payment_amount is None and key_lower in _AMOUNT_NAMES:
            payment_amount = str(val)

        # From address
        if payment_from is None and key_lower in _FROM_NAMES and _is_address(val):
            payment_from = val

        # To address
        if payment_to is None and key_lower in _TO_NAMES and _is_address(val):
            payment_to = val

    # Token address: use _address (contract that emitted the log) or contract_address
    contract_addr = decoded.get("_address") or raw_log.get("address")
    if contract_addr and _is_address(contract_addr):
        payment_token = contract_addr

    # Only return values if at least the amount was found (otherwise not a payment)
    if payment_amount is None:
        return None, None, None, None

    return payment_amount, payment_token, payment_from, payment_to


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

        # Best-effort payment field extraction from decoded fields
        payment_amount, payment_token, payment_from, payment_to = (
            _extract_payment_fields(decoded, raw_log)
        )

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
            payment_amount=payment_amount,
            payment_token=payment_token,
            payment_from=payment_from,
            payment_to=payment_to,
        )
