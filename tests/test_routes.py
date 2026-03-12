"""Tests for API routes (endpoints, ingest)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tripwire.api.auth import WalletAuthContext
from tripwire.api.middleware import RequestLoggingMiddleware
from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.api.routes.ingest import router as ingest_router
from tripwire.types.models import EndpointMode, EndpointPolicies

NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat()

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
OWNER_ADDRESS = "0x1111111111111111111111111111111111111111"
TX_HASH = "0x" + "ff" * 32
NONCE_HEX = "0x" + "ab" * 32


class _MockResult:
    def __init__(self, data: list):
        self.data = data


class _MockQuery:
    def __init__(self, data: list):
        self._data = data

    def select(self, *a, **kw) -> "_MockQuery":
        return self

    def eq(self, *a, **kw) -> "_MockQuery":
        return self

    def insert(self, row: dict) -> "_MockQuery":
        self._data = [row]
        return self

    def update(self, *a, **kw) -> "_MockQuery":
        return self

    def order(self, *a, **kw) -> "_MockQuery":
        return self

    def limit(self, *a, **kw) -> "_MockQuery":
        return self

    def lt(self, *a, **kw) -> "_MockQuery":
        return self

    def execute(self) -> _MockResult:
        return _MockResult(self._data)


class MockSupabase:
    def __init__(self, data: list | None = None):
        self._data = data or []

    def table(self, name: str) -> _MockQuery:
        return _MockQuery(self._data)


def _endpoint_row() -> dict:
    return {
        "id": "ep_test123456789abcd",
        "url": "https://myapp.example.com/webhook",
        "mode": "execute",
        "chains": [8453],
        "recipient": RECIPIENT,
        "owner_address": OWNER_ADDRESS,
        "policies": EndpointPolicies().model_dump(),
        "active": True,
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
    }


def _create_test_app(supabase_data: list | None = None) -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(endpoints_router, prefix="/api/v1")
    app.include_router(ingest_router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        from tripwire import __version__
        return {"status": "ok", "service": "tripwire", "version": __version__}

    app.state.supabase = MockSupabase(data=supabase_data)
    app.state.webhook_provider = AsyncMock()
    app.state.webhook_provider.create_app = AsyncMock(return_value="app_test")
    app.state.webhook_provider.create_endpoint = AsyncMock(return_value="ep_test")
    return app


@pytest.mark.asyncio
async def test_health():
    app = _create_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "version" in body


@pytest.mark.asyncio
async def test_register_endpoint():
    """Registration uses wallet auth; in dev mode the dev bypass provides a default owner_address."""
    app = _create_test_app()
    transport = httpx.ASGITransport(app=app)

    payload = {
        "url": "https://myapp.example.com/webhook",
        "mode": "execute",
        "chains": [8453],
        "recipient": RECIPIENT,
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # No wallet auth headers — dev-mode bypass assigns a zero address as owner
        resp = await client.post("/api/v1/endpoints", json=payload)

    assert resp.status_code == 201
    body = resp.json()
    assert body["url"] == payload["url"]
    assert body["mode"] == "execute"
    assert body["recipient"] == RECIPIENT
    assert body["active"] is True
    assert "owner_address" in body


@pytest.mark.asyncio
async def test_list_endpoints():
    app = _create_test_app(supabase_data=[_endpoint_row()])
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/endpoints")

    assert resp.status_code == 200
    body = resp.json()
    assert "data" in body
    assert body["count"] == 1
    assert body["data"][0]["id"] == "ep_test123456789abcd"


@pytest.mark.asyncio
async def test_ingest_single_event():
    app = _create_test_app()

    mock_processor = AsyncMock()
    mock_processor.process_event = AsyncMock(return_value={
        "status": "processed",
        "tx_hash": TX_HASH,
        "event_id": "evt_123",
    })
    app.state.processor = mock_processor

    transport = httpx.ASGITransport(app=app)
    raw_log = {
        "transaction_hash": TX_HASH,
        "block_number": 100,
        "block_hash": "0x" + "ee" * 32,
        "log_index": 3,
        "block_timestamp": 1700000000,
        "address": USDC_BASE,
        "chain_id": 8453,
        "decoded": {
            "authorizer": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "nonce": NONCE_HEX,
        },
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/ingest/event", json=raw_log)

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "processed"
    assert body["tx_hash"] == TX_HASH
    mock_processor.process_event.assert_awaited_once()
