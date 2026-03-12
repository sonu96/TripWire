"""Tests for API routes (endpoints, ingest, x402 middleware).

The test app factory does NOT enable x402 PaymentMiddlewareASGI because
TRIPWIRE_TREASURY_ADDRESS is empty in test env (see conftest.py).  This
lets endpoint-registration tests pass without a real facilitator.

A separate test class covers x402 middleware behaviour using explicit
middleware setup and a mock facilitator.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import httpx
from eth_account import Account
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tripwire.api import get_supabase
from tripwire.api.auth import WalletAuthContext, require_wallet_auth
from tripwire.api.middleware import RequestLoggingMiddleware
from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.api.routes.ingest import router as ingest_router, _verify_goldsky_request
from tripwire.types.models import EndpointMode, EndpointPolicies

from tests._wallet_helpers import TEST_PRIVATE_KEY

NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat()

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
_TEST_WALLET = Account.from_key(TEST_PRIVATE_KEY)
OWNER_ADDRESS = _TEST_WALLET.address
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

    mock_sb = MockSupabase(data=supabase_data)
    app.state.supabase = mock_sb
    app.state.webhook_provider = AsyncMock()
    app.state.webhook_provider.create_app = AsyncMock(return_value="app_test")
    app.state.webhook_provider.create_endpoint = AsyncMock(return_value="ep_test")

    # Override wallet auth to return a deterministic owner address
    async def _override_auth():
        return WalletAuthContext(wallet_address=OWNER_ADDRESS)

    app.dependency_overrides[require_wallet_auth] = _override_auth

    # Override Goldsky auth so ingest routes work in testing env
    async def _override_goldsky():
        return None

    app.dependency_overrides[_verify_goldsky_request] = _override_goldsky

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
    """Registration uses overridden wallet auth that provides a deterministic owner_address."""
    app = _create_test_app()
    transport = httpx.ASGITransport(app=app)

    payload = {
        "url": "https://myapp.example.com/webhook",
        "mode": "execute",
        "chains": [8453],
        "recipient": RECIPIENT,
        "owner_address": OWNER_ADDRESS,
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/endpoints", json=payload)

    assert resp.status_code == 201
    body = resp.json()
    assert body["url"] == payload["url"]
    assert body["mode"] == "execute"
    assert body["recipient"] == RECIPIENT
    assert body["active"] is True
    assert body["owner_address"] == OWNER_ADDRESS


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


# ── x402 Payment Middleware Tests ─────────────────────────────


x402 = pytest.importorskip("x402", reason="x402 package not installed")


@pytest.mark.asyncio
class TestX402PaymentMiddleware:
    """Tests for x402 payment gating on POST /api/v1/endpoints.

    These tests wire up PaymentMiddlewareASGI directly to verify that:
    - Requests without a payment header receive 402
    - The middleware passes through when treasury address is empty (disabled)

    Full facilitator round-trip tests require a running x402 facilitator
    and are marked as integration tests.
    """

    def _create_app_with_x402(self, *, treasury_address: str = "") -> FastAPI:
        """Build test app with x402 middleware explicitly configured."""
        from x402.http import HTTPFacilitatorClient, FacilitatorConfig, PaymentOption
        from x402.http.middleware.fastapi import PaymentMiddlewareASGI
        from x402.http.types import RouteConfig
        from x402.mechanisms.evm.exact import ExactEvmServerScheme
        from x402.server import x402ResourceServer

        app = FastAPI()
        app.add_middleware(RequestLoggingMiddleware)
        app.include_router(endpoints_router, prefix="/api/v1")

        mock_sb = MockSupabase(data=[])
        app.state.supabase = mock_sb
        app.state.webhook_provider = AsyncMock()
        app.state.webhook_provider.create_app = AsyncMock(return_value="app_test")
        app.state.webhook_provider.create_endpoint = AsyncMock(return_value="ep_test")

        # Override wallet auth
        async def _override_auth():
            return WalletAuthContext(wallet_address=OWNER_ADDRESS)

        app.dependency_overrides[require_wallet_auth] = _override_auth

        # Wire up x402 middleware if treasury address provided
        if treasury_address:
            x402_server = x402ResourceServer(
                HTTPFacilitatorClient(
                    FacilitatorConfig(url="https://x402.org/facilitator")
                )
            )
            x402_server.register("eip155:8453", ExactEvmServerScheme())

            x402_routes = {
                "POST /api/v1/endpoints": RouteConfig(
                    accepts=[
                        PaymentOption(
                            scheme="exact",
                            price="$1.00",
                            network="eip155:8453",
                            pay_to=treasury_address,
                        )
                    ]
                ),
            }
            app.add_middleware(
                PaymentMiddlewareASGI, routes=x402_routes, server=x402_server
            )

        return app

    async def test_post_endpoints_returns_402_without_payment(self):
        """POST /endpoints without X-PAYMENT header should return 402."""
        app = self._create_app_with_x402(
            treasury_address="0x1234567890abcdef1234567890abcdef12345678"
        )
        transport = httpx.ASGITransport(app=app)

        payload = {
            "url": "https://myapp.example.com/webhook",
            "mode": "execute",
            "chains": [8453],
            "recipient": RECIPIENT,
            "owner_address": OWNER_ADDRESS,
        }

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/endpoints", json=payload)

        assert resp.status_code == 402, (
            f"Expected 402 Payment Required, got {resp.status_code}"
        )

    async def test_get_endpoints_bypasses_x402(self):
        """GET /endpoints should NOT require payment even when x402 is active."""
        app = self._create_app_with_x402(
            treasury_address="0x1234567890abcdef1234567890abcdef12345678"
        )
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/endpoints")

        # Should succeed (200) — x402 only gates POST /endpoints
        assert resp.status_code == 200

    async def test_x402_disabled_when_treasury_empty(self):
        """When treasury address is empty, x402 middleware is not added and POST works."""
        app = self._create_app_with_x402(treasury_address="")
        transport = httpx.ASGITransport(app=app)

        payload = {
            "url": "https://myapp.example.com/webhook",
            "mode": "execute",
            "chains": [8453],
            "recipient": RECIPIENT,
            "owner_address": OWNER_ADDRESS,
        }

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/v1/endpoints", json=payload)

        # Without x402 middleware, registration should succeed
        assert resp.status_code == 201


@pytest.mark.asyncio
@pytest.mark.integration
async def test_x402_full_facilitator_roundtrip():
    """Full x402 facilitator round-trip: pay -> register endpoint.

    This test requires a running x402 facilitator and is skipped in CI.
    Run with: pytest -m integration
    """
    pytest.skip(
        "Requires a live x402 facilitator. "
        "Run with pytest -m integration against a local facilitator."
    )
