"""Tests for execution_state_from_status and decoder protocol."""

import pytest

from tripwire.types.models import (
    ExecutionState,
    TrustSource,
    execution_state_from_status,
)
from tripwire.ingestion.decoders.protocol import Decoder, DecodedEvent
from tripwire.ingestion.decoders import ERC3009Decoder, AbiGenericDecoder


# ── Part A: execution_state_from_status ────────────────────


class TestExecutionStateFromStatus:
    def test_pre_confirmed(self):
        state, safe, source = execution_state_from_status("pre_confirmed")
        assert state == ExecutionState.PROVISIONAL
        assert safe is False
        assert source == TrustSource.FACILITATOR

    def test_pending(self):
        state, safe, source = execution_state_from_status("pending")
        assert state == ExecutionState.CONFIRMED
        assert safe is False
        assert source == TrustSource.ONCHAIN

    def test_confirmed(self):
        state, safe, source = execution_state_from_status("confirmed")
        assert state == ExecutionState.CONFIRMED
        assert safe is False
        assert source == TrustSource.ONCHAIN

    def test_finalized(self):
        state, safe, source = execution_state_from_status("finalized")
        assert state == ExecutionState.FINALIZED
        assert safe is True
        assert source == TrustSource.ONCHAIN

    def test_reorged(self):
        state, safe, source = execution_state_from_status("reorged")
        assert state == ExecutionState.REORGED
        assert safe is False
        assert source == TrustSource.ONCHAIN

    def test_unknown_falls_back(self):
        state, safe, source = execution_state_from_status("some_unknown_status")
        assert state == ExecutionState.CONFIRMED
        assert safe is False
        assert source == TrustSource.ONCHAIN


# ── Part C1: Decoder protocol compliance ───────────────────


class TestDecoderProtocol:
    def test_erc3009_satisfies_protocol(self):
        assert isinstance(ERC3009Decoder(), Decoder)

    def test_abi_generic_satisfies_protocol(self):
        decoder = AbiGenericDecoder(abi_fragment=[{"type": "event", "name": "Test", "inputs": []}])
        assert isinstance(decoder, Decoder)

    def test_erc3009_decoder_name(self):
        assert ERC3009Decoder().name == "erc3009"

    def test_abi_generic_decoder_name(self):
        decoder = AbiGenericDecoder(abi_fragment=[])
        assert decoder.name == "abi_generic"


class TestERC3009Decoder:
    """Test ERC3009Decoder.decode returns a proper DecodedEvent."""

    USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
    AUTHORIZER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    NONCE_BYTES32 = "0x" + "ab" * 32
    TX_HASH = "0x" + "ff" * 32
    BLOCK_HASH = "0x" + "ee" * 32

    RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"

    def _make_raw_log(self):
        from tripwire.ingestion.decoder import AUTHORIZATION_USED_TOPIC
        return {
            "address": self.USDC_BASE,
            "topics": f"{AUTHORIZATION_USED_TOPIC},0x000000000000000000000000{self.AUTHORIZER[2:]},{self.NONCE_BYTES32}",
            "data": "0x",
            "transaction_hash": self.TX_HASH,
            "block_number": 100,
            "block_hash": self.BLOCK_HASH,
            "log_index": 1,
            "chain_id": 8453,
            "transfer": {
                "from_address": self.AUTHORIZER,
                "to_address": self.RECIPIENT,
                "value": "5000000",
            },
        }

    def test_decode_returns_decoded_event(self):
        decoder = ERC3009Decoder()
        raw = self._make_raw_log()
        result = decoder.decode(raw)
        assert isinstance(result, DecodedEvent)
        assert result.decoder_name == "erc3009"
        assert result.typed_model is not None
        assert result.tx_hash == self.TX_HASH
        assert result.block_number == 100

    def test_can_decode_auth_used(self):
        decoder = ERC3009Decoder()
        assert decoder.can_decode(self._make_raw_log()) is True

    def test_cannot_decode_random_topic(self):
        decoder = ERC3009Decoder()
        raw = {"topics": ["0x" + "00" * 32]}
        assert decoder.can_decode(raw) is False


class TestAbiGenericDecoder:
    """Test AbiGenericDecoder.decode returns a proper DecodedEvent."""

    def _make_abi_and_log(self):
        abi = [{
            "type": "event",
            "name": "Transfer",
            "inputs": [
                {"name": "from", "type": "address", "indexed": True},
                {"name": "to", "type": "address", "indexed": True},
                {"name": "value", "type": "uint256", "indexed": False},
            ],
        }]
        from eth_abi import encode as abi_encode
        raw_log = {
            "topics": [
                "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef",
                "0x0000000000000000000000001234567890abcdef1234567890abcdef12345678",
                "0x000000000000000000000000abcdefabcdefabcdefabcdefabcdefabcdefabcd",
            ],
            "data": "0x" + abi_encode(["uint256"], [1000000]).hex(),
            "transaction_hash": "0x" + "ff" * 32,
            "block_number": 200,
            "block_hash": "0x" + "ee" * 32,
            "log_index": 5,
            "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
            "chain_id": 8453,
        }
        return abi, raw_log

    def test_decode_returns_decoded_event(self):
        abi, raw_log = self._make_abi_and_log()
        decoder = AbiGenericDecoder(abi_fragment=abi)
        result = decoder.decode(raw_log)
        assert isinstance(result, DecodedEvent)
        assert result.decoder_name == "abi_generic"
        assert result.tx_hash == "0x" + "ff" * 32
        assert result.block_number == 200
        assert "from" in result.fields
        assert "to" in result.fields
        assert result.fields["value"] == 1000000

    def test_identity_address_extracted(self):
        abi, raw_log = self._make_abi_and_log()
        decoder = AbiGenericDecoder(abi_fragment=abi)
        result = decoder.decode(raw_log)
        # Should find the first address field
        assert result.identity_address is not None
        assert result.identity_address.startswith("0x")
