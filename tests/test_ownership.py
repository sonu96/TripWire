"""Tests for endpoint ownership enforcement.

Verifies that only the wallet that created an endpoint can read, update, or
delete it, and that listing only returns endpoints belonging to the caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI, Depends

from tripwire.api import get_supabase
from tripwire.api.auth import WalletAuthContext, require_wallet_auth
from tripwire.api.middleware import RequestLoggingMiddleware
from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.types.models import EndpointPolicies

from tests._wallet_helpers import TEST_PRIVATE_KEY, OTHER_PRIVATE_KEY

# ── Deterministic wallets ────────────────────────────────────

_WALLET_A = Account.from_key(TEST_PRIVATE_KEY)
_WALLET_B = Account.from_key(OTHER_PRIVATE_KEY)

NOW_ISO = datetime.now(timezone.utc).isoformat()

RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"


# ── Mock Supabase ────────────────────────────────────────────
# A lightweight mock that supports filtering by column values so
# ownership checks and list-filtering work correctly.


class _MockResult:
    def __init__(self, data: list):
        self.data = data


class _MockQuery:
    def __init__(self, rows: list[dict]):
        self._rows = list(rows)
        self._filters: list[tuple[str, object]] = []
        self._pending_insert: dict | None = None
        self._pending_update: dict | None = None

    def select(self, *a, **kw) -> "_MockQuery":
        return self

    def eq(self, col: str, val) -> "_MockQuery":
        self._filters.append((col, val))
        return self

    def insert(self, row: dict) -> "_MockQuery":
        self._pending_insert = row
        return self

    def update(self, values: dict) -> "_MockQuery":
        self._pending_update = values
        return self

    def order(self, *a, **kw) -> "_MockQuery":
        return self

    def limit(self, *a, **kw) -> "_MockQuery":
        return self

    def lt(self, *a, **kw) -> "_MockQuery":
        return self

    def execute(self) -> _MockResult:
        if self._pending_insert is not None:
            return _MockResult([self._pending_insert])
        if self._pending_update is not None:
            # Return the first matching row merged with updates
            matched = self._apply_filters()
            if matched:
                merged = {**matched[0], **self._pending_update}
                return _MockResult([merged])
            return _MockResult([])
        return _MockResult(self._apply_filters())

    def _apply_filters(self) -> list[dict]:
        result = self._rows
        for col, val in self._filters:
            result = [r for r in result if r.get(col) == val]
        return result


class _OwnershipMockSupabase:
    """Mock Supabase that serves pre-loaded endpoint rows with filtering."""

    def __init__(self, rows: list[dict]):
        self._rows = rows

    def table(self, name: str) -> _MockQuery:
        if name == "endpoints":
            return _MockQuery(self._rows)
        return _MockQuery([])


# ── App factory ──────────────────────────────────────────────


def _ownership_app(
    wallet_address: str,
    endpoint_rows: list[dict],
) -> FastAPI:
    """Build a test app with auth overridden to return a specific wallet address."""
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(endpoints_router, prefix="/api/v1")

    # Override auth dependency to return the specified wallet
    async def _override_auth():
        return WalletAuthContext(wallet_address=wallet_address)

    app.dependency_overrides[require_wallet_auth] = _override_auth

    # Override supabase dependency
    mock_sb = _OwnershipMockSupabase(endpoint_rows)

    def _override_supabase():
        return mock_sb

    app.dependency_overrides[get_supabase] = _override_supabase

    # Webhook provider (needed for POST /endpoints with mode=execute)
    app.state.webhook_provider = AsyncMock()
    app.state.webhook_provider.create_app = AsyncMock(return_value="app_test")
    app.state.webhook_provider.create_endpoint = AsyncMock(return_value="ep_test")
    app.state.supabase = mock_sb

    return app


def _endpoint_row(owner: str, endpoint_id: str = "ep_test123456789abcd") -> dict:
    return {
        "id": endpoint_id,
        "url": "https://myapp.example.com/webhook",
        "mode": "execute",
        "chains": [8453],
        "recipient": RECIPIENT,
        "owner_address": owner,
        "policies": EndpointPolicies().model_dump(),
        "active": True,
        "created_at": NOW_ISO,
        "updated_at": NOW_ISO,
    }


# ── Tests ────────────────────────────────────────────────────


class TestOwnership:
    """Endpoint ownership tests using dependency_overrides for auth."""

    @pytest.mark.asyncio
    async def test_owner_can_get_own_endpoint(self):
        """Wallet A creates an endpoint, wallet A can GET it."""
        row = _endpoint_row(owner=_WALLET_A.address)
        app = _ownership_app(wallet_address=_WALLET_A.address, endpoint_rows=[row])
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/endpoints/{row['id']}")

        assert resp.status_code == 200
        assert resp.json()["id"] == row["id"]

    @pytest.mark.asyncio
    async def test_non_owner_cannot_get_endpoint(self):
        """Wallet A owns the endpoint; wallet B gets 403."""
        row = _endpoint_row(owner=_WALLET_A.address)
        app = _ownership_app(wallet_address=_WALLET_B.address, endpoint_rows=[row])
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get(f"/api/v1/endpoints/{row['id']}")

        assert resp.status_code == 403
        assert "Not authorized" in resp.json()["detail"]

    @pytest.mark.asyncio
    async def test_non_owner_cannot_update_endpoint(self):
        """Wallet B tries to PATCH wallet A's endpoint -> 403."""
        row = _endpoint_row(owner=_WALLET_A.address)
        app = _ownership_app(wallet_address=_WALLET_B.address, endpoint_rows=[row])
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.patch(
                f"/api/v1/endpoints/{row['id']}",
                json={"active": False},
            )

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_non_owner_cannot_delete_endpoint(self):
        """Wallet B tries to DELETE wallet A's endpoint -> 403."""
        row = _endpoint_row(owner=_WALLET_A.address)
        app = _ownership_app(wallet_address=_WALLET_B.address, endpoint_rows=[row])
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete(f"/api/v1/endpoints/{row['id']}")

        assert resp.status_code == 403

    @pytest.mark.asyncio
    async def test_list_only_shows_owned_endpoints(self):
        """Wallet A only sees endpoints they own, not wallet B's."""
        rows = [
            _endpoint_row(owner=_WALLET_A.address, endpoint_id="ep_owned_by_A_111"),
            _endpoint_row(owner=_WALLET_A.address, endpoint_id="ep_owned_by_A_222"),
            _endpoint_row(owner=_WALLET_B.address, endpoint_id="ep_owned_by_B_333"),
        ]
        app = _ownership_app(wallet_address=_WALLET_A.address, endpoint_rows=rows)
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/v1/endpoints")

        assert resp.status_code == 200
        body = resp.json()
        # The mock filters by owner_address AND active=True via .eq() chains
        assert body["count"] == 2
        returned_ids = {ep["id"] for ep in body["data"]}
        assert "ep_owned_by_A_111" in returned_ids
        assert "ep_owned_by_A_222" in returned_ids
        assert "ep_owned_by_B_333" not in returned_ids
