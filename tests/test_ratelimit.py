"""Tests for API rate limiting."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
import httpx
from fastapi import FastAPI, Request
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from tripwire.api.ratelimit import limiter, rate_limit_exceeded_handler, INGEST_LIMIT, CRUD_LIMIT
from tripwire.api.routes.ingest import router as ingest_router
from tripwire.api.routes.endpoints import router as endpoints_router


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

    def execute(self) -> _MockResult:
        return _MockResult(self._data)


class MockSupabase:
    def __init__(self, data: list | None = None):
        self._data = data or []

    def table(self, name: str) -> _MockQuery:
        return _MockQuery(self._data)


def _create_rate_limit_app() -> FastAPI:
    """Create a minimal FastAPI app with rate limiting enabled."""
    app = FastAPI()

    # Wire up rate limiter
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)

    app.include_router(ingest_router, prefix="/api/v1")
    app.include_router(endpoints_router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    # Mock app state
    app.state.supabase = MockSupabase()
    app.state.webhook_provider = AsyncMock()
    app.state.processor = AsyncMock()
    app.state.processor.process_event = AsyncMock(return_value={
        "status": "processed",
        "tx_hash": "0x" + "ff" * 32,
        "event_id": "evt_123",
    })

    return app


@pytest.mark.asyncio
async def test_health_endpoint_not_rate_limited():
    """Health endpoint should not be rate limited."""
    app = _create_rate_limit_app()
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Make many requests — health should never be limited
        for _ in range(50):
            resp = await client.get("/health")
            assert resp.status_code == 200


@pytest.mark.asyncio
async def test_ingest_rate_limit_returns_429():
    """Ingest endpoint should return 429 after exceeding 100 requests/minute."""
    app = _create_rate_limit_app()
    # Reset limiter storage between tests
    limiter.reset()

    transport = httpx.ASGITransport(app=app)
    raw_log = {
        "transaction_hash": "0x" + "ff" * 32,
        "block_number": 100,
        "block_hash": "0x" + "ee" * 32,
        "log_index": 3,
        "block_timestamp": 1700000000,
        "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "chain_id": 8453,
        "decoded": {
            "authorizer": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "nonce": "0x" + "ab" * 32,
        },
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Send 101 requests — the 101st should be rate limited
        statuses = []
        for _ in range(101):
            resp = await client.post("/api/v1/ingest/event", json=raw_log)
            statuses.append(resp.status_code)

        # First 100 should succeed (200)
        assert statuses[:100] == [200] * 100
        # 101st should be rate limited
        assert statuses[100] == 429


@pytest.mark.asyncio
async def test_crud_rate_limit_returns_429():
    """CRUD endpoints should return 429 after exceeding 30 requests/minute."""
    app = _create_rate_limit_app()
    limiter.reset()

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        statuses = []
        for _ in range(31):
            resp = await client.get("/api/v1/endpoints")
            statuses.append(resp.status_code)

        # First 30 should succeed
        assert statuses[:30] == [200] * 30
        # 31st should be rate limited
        assert statuses[30] == 429


@pytest.mark.asyncio
async def test_429_response_has_retry_after_header():
    """Rate limited responses should include Retry-After header."""
    app = _create_rate_limit_app()
    limiter.reset()

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Exhaust the CRUD limit
        for _ in range(30):
            await client.get("/api/v1/endpoints")

        # Next request should be 429 with Retry-After
        resp = await client.get("/api/v1/endpoints")
        assert resp.status_code == 429
        assert "retry-after" in resp.headers
        body = resp.json()
        assert "Rate limit exceeded" in body["detail"]


@pytest.mark.asyncio
async def test_rate_limit_per_api_key():
    """Different API keys should have independent rate limits.

    Uses the ingest endpoint (Goldsky auth, skipped in dev mode) to test
    per-key rate limiting without needing a real database for API key lookup.
    """
    app = _create_rate_limit_app()
    limiter.reset()

    transport = httpx.ASGITransport(app=app)
    raw_log = {
        "transaction_hash": "0x" + "ff" * 32,
        "block_number": 100,
        "block_hash": "0x" + "ee" * 32,
        "log_index": 3,
        "block_timestamp": 1700000000,
        "address": "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913",
        "chain_id": 8453,
        "decoded": {
            "authorizer": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "nonce": "0x" + "ab" * 32,
        },
    }

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        # Exhaust limit for key A (100/minute for ingest)
        for _ in range(100):
            resp = await client.post(
                "/api/v1/ingest/event",
                json=raw_log,
                headers={"Authorization": "Bearer tw_key_a"},
            )
            assert resp.status_code == 200

        # Key A should now be limited
        resp = await client.post(
            "/api/v1/ingest/event",
            json=raw_log,
            headers={"Authorization": "Bearer tw_key_a"},
        )
        assert resp.status_code == 429

        # Key B should still work (separate rate limit bucket)
        resp = await client.post(
            "/api/v1/ingest/event",
            json=raw_log,
            headers={"Authorization": "Bearer tw_key_b"},
        )
        assert resp.status_code == 200
