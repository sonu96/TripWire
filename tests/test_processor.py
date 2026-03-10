"""Tests for tripwire/ingestion/processor.py."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tripwire.ingestion.processor import EventProcessor
from tripwire.types.models import (
    AgentIdentity,
    ChainId,
    Endpoint,
    EndpointMode,
    EndpointPolicies,
)

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
SENDER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
TX_HASH = "0x" + "ff" * 32
BLOCK_HASH = "0x" + "ee" * 32
NONCE_HEX = "0x" + "ab" * 32
NOW = datetime.now(timezone.utc)


def _raw_log() -> dict:
    return {
        "transaction_hash": TX_HASH,
        "block_number": 100,
        "block_hash": BLOCK_HASH,
        "log_index": 3,
        "block_timestamp": 1700000000,
        "address": USDC_BASE,
        "chain_id": 8453,
        "decoded": {
            "authorizer": SENDER,
            "nonce": NONCE_HEX,
        },
        "transfer": {
            "from_address": SENDER,
            "to_address": RECIPIENT,
            "value": 5_000_000,
        },
    }


def _endpoint_row() -> dict:
    return {
        "id": "ep_test123",
        "url": "https://myapp.example.com/webhook",
        "mode": "execute",
        "chains": [8453],
        "recipient": SENDER.lower(),
        "policies": EndpointPolicies().model_dump(),
        "active": True,
        "created_at": NOW.isoformat(),
        "updated_at": NOW.isoformat(),
    }


def _make_processor(
    endpoint_rows: list | None = None,
    nonce_is_new: bool = True,
) -> EventProcessor:
    # Build Endpoint objects from raw rows so list_by_recipient returns them
    endpoints = [Endpoint(**row) for row in (endpoint_rows or [])]

    endpoint_repo = MagicMock()
    endpoint_repo.list_by_recipient = MagicMock(return_value=endpoints)

    event_repo = MagicMock()
    event_repo.insert = MagicMock(return_value={})

    delivery_repo = MagicMock()
    delivery_repo.create = MagicMock(return_value={})

    resolver = AsyncMock()
    resolver.resolve = AsyncMock(return_value=None)

    nonce_repo = MagicMock()
    nonce_repo.record_nonce = MagicMock(return_value=nonce_is_new)

    realtime_notifier = AsyncMock()
    realtime_notifier.notify_batch = AsyncMock(return_value=[])

    webhook_provider = AsyncMock()
    webhook_provider.send = AsyncMock(return_value="msg_001")

    return EventProcessor(
        endpoint_repo=endpoint_repo,
        event_repo=event_repo,
        nonce_repo=nonce_repo,
        delivery_repo=delivery_repo,
        identity_resolver=resolver,
        realtime_notifier=realtime_notifier,
        webhook_provider=webhook_provider,
    )


@pytest.mark.asyncio
async def test_process_event_success():
    ep_row = _endpoint_row()
    processor = _make_processor(endpoint_rows=[ep_row])
    ep = Endpoint(**ep_row)

    with patch("tripwire.ingestion.processor.check_finality", new_callable=AsyncMock) as mock_fin, \
         patch("tripwire.ingestion.processor.dispatch_event", new_callable=AsyncMock) as mock_disp, \
         patch("tripwire.ingestion.processor.match_endpoints", return_value=[ep]):

        mock_fin.return_value = MagicMock(
            is_finalized=True,
            confirmations=3,
            required_confirmations=3,
        )
        mock_disp.return_value = ["msg_001"]

        result = await processor.process_event(_raw_log())

    assert result["status"] == "processed"
    assert result["tx_hash"] == TX_HASH
    assert result["endpoints_matched"] == 1
    assert result["webhooks_sent"] == 1


@pytest.mark.asyncio
async def test_process_event_duplicate():
    processor = _make_processor(nonce_is_new=False)
    result = await processor.process_event(_raw_log())
    assert result["status"] == "duplicate"
    assert result["tx_hash"] == TX_HASH


@pytest.mark.asyncio
async def test_process_event_decode_failure():
    processor = _make_processor()
    bad_log: dict[str, Any] = {"garbage": True}
    result = await processor.process_event(bad_log)
    assert result["status"] == "error"
    assert result["reason"] == "decode_failed"


@pytest.mark.asyncio
async def test_process_batch():
    ep_row = _endpoint_row()
    processor = _make_processor(endpoint_rows=[ep_row])
    ep = Endpoint(**ep_row)

    with patch("tripwire.ingestion.processor.check_finality", new_callable=AsyncMock) as mock_fin, \
         patch("tripwire.ingestion.processor.dispatch_event", new_callable=AsyncMock) as mock_disp, \
         patch("tripwire.ingestion.processor.match_endpoints", return_value=[ep]):

        mock_fin.return_value = MagicMock(
            is_finalized=True,
            confirmations=3,
            required_confirmations=3,
        )
        mock_disp.return_value = ["msg_001"]

        results = await processor.process_batch([_raw_log(), _raw_log()])

    assert len(results) == 2
    assert all(r["tx_hash"] == TX_HASH for r in results)
