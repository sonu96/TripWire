"""Tests for API key rotation endpoint and dual-key auth."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import FastAPI
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from tripwire.api.auth import generate_api_key, hash_api_key
from tripwire.api.middleware import RequestLoggingMiddleware
from tripwire.api.ratelimit import limiter, rate_limit_exceeded_handler
from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.types.models import EndpointPolicies


@pytest.fixture(autouse=True)
def _disable_rate_limiter():
    """Disable slowapi rate limiter for key rotation tests.

    Avoids the header injection issue with Pydantic response models.
    """
    limiter.enabled = False
    yield
    limiter.enabled = True

RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
NOW = datetime.now(timezone.utc)
NOW_ISO = NOW.isoformat()


class _MockResult:
    def __init__(self, data: list):
        self.data = data


class _RotationMockQuery:
    """Mock that tracks filter state to simulate dual-key lookups."""

    def __init__(self, rows: list[dict]):
        self._rows = rows
        self._filters: dict[str, object] = {}
        self._selected_cols: str = "*"

    def select(self, cols: str, *a, **kw) -> "_RotationMockQuery":
        self._selected_cols = cols
        return self

    def eq(self, col: str, val: object) -> "_RotationMockQuery":
        self._filters[col] = val
        return self

    def insert(self, row: dict) -> "_RotationMockQuery":
        self._rows.append(row)
        return self

    def update(self, updates: dict) -> "_RotationMockQuery":
        # Apply updates to matching rows
        for row in self._rows:
            match = all(row.get(k) == v for k, v in self._filters.items())
            if match:
                row.update(updates)
        return self

    def execute(self) -> _MockResult:
        matched = []
        for row in self._rows:
            if all(row.get(k) == v for k, v in self._filters.items()):
                matched.append(dict(row))  # copy
        return _MockResult(matched)


class RotationMockSupabase:
    """Mock Supabase that supports rotation-aware queries."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def table(self, name: str) -> _RotationMockQuery:
        return _RotationMockQuery(self._rows)


def _make_endpoint_row(api_key_hash: str, **overrides) -> dict:
    row = {
        "id": "ep_test_rotation_001",
        "url": "https://myapp.example.com/webhook",
        "mode": "execute",
        "chains": [8453],
        "recipient": RECIPIENT,
        "policies": EndpointPolicies().model_dump(),
        "api_key_hash": api_key_hash,
        "old_api_key_hash": None,
        "key_rotated_at": None,
        "active": True,
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
    }
    row.update(overrides)
    return row


def _create_test_app(rows: list[dict]) -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(endpoints_router, prefix="/api/v1")
    app.state.supabase = RotationMockSupabase(rows)
    app.state.webhook_provider = AsyncMock()
    return app


# ── Rotation endpoint tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_rotate_key_returns_new_key():
    """POST /endpoints/{id}/rotate-key generates a new key and returns it."""
    original_key = generate_api_key()
    original_hash = hash_api_key(original_key)
    rows = [_make_endpoint_row(original_hash)]
    app = _create_test_app(rows)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/endpoints/ep_test_rotation_001/rotate-key")

    assert resp.status_code == 200
    body = resp.json()
    assert body["endpoint_id"] == "ep_test_rotation_001"
    assert body["api_key"].startswith("tw_")
    assert "rotated_at" in body

    # The returned key should be different from the original
    assert body["api_key"] != original_key


@pytest.mark.asyncio
async def test_rotate_key_404_for_missing_endpoint():
    """Rotating a non-existent endpoint returns 404."""
    app = _create_test_app(rows=[])
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/endpoints/ep_nonexistent/rotate-key")

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_rotate_key_moves_old_hash():
    """After rotation, the old hash is preserved in old_api_key_hash."""
    original_key = generate_api_key()
    original_hash = hash_api_key(original_key)
    rows = [_make_endpoint_row(original_hash)]
    app = _create_test_app(rows)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/endpoints/ep_test_rotation_001/rotate-key")

    assert resp.status_code == 200
    body = resp.json()
    new_hash = hash_api_key(body["api_key"])

    # Verify the row was updated: new hash is current, old hash is preserved
    row = rows[0]
    assert row["api_key_hash"] == new_hash
    assert row["old_api_key_hash"] == original_hash
    assert row["key_rotated_at"] is not None


# ── Dual-key auth tests ───────────────────────────────────────


@pytest.mark.asyncio
async def test_auth_accepts_new_key_after_rotation():
    """After rotation, the new key authenticates successfully."""
    original_key = generate_api_key()
    original_hash = hash_api_key(original_key)
    new_key = generate_api_key()
    new_hash = hash_api_key(new_key)

    rows = [_make_endpoint_row(
        new_hash,
        old_api_key_hash=original_hash,
        key_rotated_at=NOW_ISO,
    )]
    app = _create_test_app(rows)

    # Add a simple authenticated route for testing
    @app.get("/api/v1/endpoints")
    async def _list():
        return {"data": [], "count": 0}

    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/endpoints",
            headers={"Authorization": f"Bearer {new_key}"},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_accepts_old_key_within_grace_period():
    """During grace period, the old key still authenticates."""
    original_key = generate_api_key()
    original_hash = hash_api_key(original_key)
    new_key = generate_api_key()
    new_hash = hash_api_key(new_key)

    # Rotated 1 hour ago - within 24h grace period
    rotated_at = (NOW - timedelta(hours=1)).isoformat()

    rows = [_make_endpoint_row(
        new_hash,
        old_api_key_hash=original_hash,
        key_rotated_at=rotated_at,
    )]
    app = _create_test_app(rows)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/endpoints",
            headers={"Authorization": f"Bearer {original_key}"},
        )

    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_auth_rejects_old_key_after_grace_period():
    """After the grace period expires, the old key is rejected."""
    original_key = generate_api_key()
    original_hash = hash_api_key(original_key)
    new_key = generate_api_key()
    new_hash = hash_api_key(new_key)

    # Rotated 25 hours ago - outside 24h grace period
    rotated_at = (NOW - timedelta(hours=25)).isoformat()

    rows = [_make_endpoint_row(
        new_hash,
        old_api_key_hash=original_hash,
        key_rotated_at=rotated_at,
    )]
    app = _create_test_app(rows)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get(
            "/api/v1/endpoints",
            headers={"Authorization": f"Bearer {original_key}"},
        )

    assert resp.status_code == 401
