"""Integration tests for the full TripWire pipeline.

Tests the complete flow via HTTP: register endpoint -> ingest event -> verify
state changes. Uses a stateful MockSupabase that tracks data across tables
so the processor, repositories, and routes all work end-to-end.
"""

from __future__ import annotations

import copy
import os
import time
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from eth_account import Account
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware

from tripwire.api.auth import WalletAuthContext, require_wallet_auth
from tripwire.api.middleware import RequestLoggingMiddleware
from tripwire.api.ratelimit import limiter, rate_limit_exceeded_handler
from tripwire.api.routes.endpoints import router as endpoints_router
from tripwire.api.routes.ingest import router as ingest_router, _verify_goldsky_request
from tripwire.api.routes.subscriptions import router as subscriptions_router
from tripwire.db.repositories.endpoints import EndpointRepository
from tripwire.db.repositories.events import EventRepository
from tripwire.db.repositories.nonces import NonceRepository
from tripwire.db.repositories.webhooks import WebhookDeliveryRepository
from tripwire.identity.resolver import MockResolver
from tripwire.ingestion.processor import EventProcessor
from tripwire.notify.realtime import RealtimeNotifier
from tripwire.webhook.provider import LogOnlyProvider

from tests._wallet_helpers import TEST_PRIVATE_KEY, make_auth_headers, MockRedis

# ── Constants ────────────────────────────────────────────────

USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
SENDER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_TEST_WALLET = Account.from_key(TEST_PRIVATE_KEY)
TEST_OWNER_ADDRESS = _TEST_WALLET.address
TX_HASH = "0x" + "ff" * 32
BLOCK_HASH = "0x" + "ee" * 32
NONCE_HEX = "0x" + "ab" * 32


# ── Stateful MockSupabase ────────────────────────────────────
# Tracks actual inserts/selects/upserts across tables so the full
# pipeline flows through repositories without hitting a real DB.


class _MockResult:
    """Mimics the postgrest result object."""

    def __init__(self, data: list[dict]):
        self.data = data


class _MockQuery:
    """Chainable query builder that operates on the in-memory table store."""

    def __init__(self, store: dict[str, list[dict]], table_name: str):
        self._store = store
        self._table = table_name
        self._filters: list[tuple[str, str, Any]] = []
        self._pending_insert: list[dict] | None = None
        self._pending_update: dict | None = None
        self._pending_upsert: dict | None = None
        self._upsert_conflict: str | None = None
        self._upsert_ignore_dups: bool = False
        self._selected_cols: str = "*"
        self._order_col: str | None = None
        self._order_desc: bool = False
        self._limit_val: int | None = None

    # ── Chainable methods ─────────────────────────────────────

    def select(self, cols: str = "*", **kw) -> "_MockQuery":
        self._selected_cols = cols
        return self

    def eq(self, col: str, val: Any) -> "_MockQuery":
        self._filters.append(("eq", col, val))
        return self

    def lt(self, col: str, val: Any) -> "_MockQuery":
        self._filters.append(("lt", col, val))
        return self

    def order(self, col: str, desc: bool = False, **kw) -> "_MockQuery":
        self._order_col = col
        self._order_desc = desc
        return self

    def limit(self, n: int) -> "_MockQuery":
        self._limit_val = n
        return self

    def insert(self, rows: dict | list[dict]) -> "_MockQuery":
        if isinstance(rows, dict):
            self._pending_insert = [rows]
        else:
            self._pending_insert = rows
        return self

    def update(self, values: dict) -> "_MockQuery":
        self._pending_update = values
        return self

    def upsert(
        self, row: dict, on_conflict: str = "", ignore_duplicates: bool = False
    ) -> "_MockQuery":
        self._pending_upsert = row
        self._upsert_conflict = on_conflict
        self._upsert_ignore_dups = ignore_duplicates
        return self

    # ── Execute ───────────────────────────────────────────────

    def execute(self) -> _MockResult:
        table = self._store.setdefault(self._table, [])

        # INSERT
        if self._pending_insert is not None:
            for row in self._pending_insert:
                table.append(copy.deepcopy(row))
            return _MockResult(copy.deepcopy(self._pending_insert))

        # UPSERT (used by NonceRepository)
        if self._pending_upsert is not None:
            row = self._pending_upsert
            conflict_cols = [c.strip() for c in (self._upsert_conflict or "").split(",") if c.strip()]
            # Check for existing row matching all conflict columns
            existing = None
            for existing_row in table:
                if all(existing_row.get(c) == row.get(c) for c in conflict_cols):
                    existing = existing_row
                    break

            if existing is not None:
                if self._upsert_ignore_dups:
                    # Duplicate found, ignore_duplicates=True → return empty data
                    return _MockResult([])
                # Update existing row
                existing.update(row)
                return _MockResult([copy.deepcopy(existing)])
            else:
                # New row
                table.append(copy.deepcopy(row))
                return _MockResult([copy.deepcopy(row)])

        # UPDATE with filters
        if self._pending_update is not None:
            updated = []
            for existing_row in table:
                if self._matches(existing_row):
                    existing_row.update(self._pending_update)
                    updated.append(copy.deepcopy(existing_row))
            return _MockResult(updated)

        # SELECT with filters
        results = [copy.deepcopy(r) for r in table if self._matches(r)]

        # Ordering
        if self._order_col:
            results.sort(
                key=lambda r: r.get(self._order_col, ""),
                reverse=self._order_desc,
            )

        # Limit
        if self._limit_val is not None:
            results = results[: self._limit_val]

        return _MockResult(results)

    # ── Filter matching ───────────────────────────────────────

    def _matches(self, row: dict) -> bool:
        for op, col, val in self._filters:
            row_val = row.get(col)
            if op == "eq" and row_val != val:
                return False
            if op == "lt" and not (row_val is not None and row_val < val):
                return False
        return True


class StatefulMockSupabase:
    """In-memory Supabase mock that tracks state across tables."""

    def __init__(self) -> None:
        self._store: dict[str, list[dict]] = {}

    def table(self, name: str) -> _MockQuery:
        return _MockQuery(self._store, name)

    def get_table_data(self, name: str) -> list[dict]:
        """Helper for test assertions: return all rows in a table."""
        return list(self._store.get(name, []))


# ── App Factory ──────────────────────────────────────────────


def _create_integration_app() -> tuple[FastAPI, StatefulMockSupabase]:
    """Build a FastAPI app wired with the real processor + stateful mock DB."""
    app = FastAPI()

    # Rate limiter
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, rate_limit_exceeded_handler)

    # Middleware
    app.add_middleware(RequestLoggingMiddleware)
    app.add_middleware(SlowAPIMiddleware)

    # Routes
    app.include_router(endpoints_router, prefix="/api/v1")
    app.include_router(ingest_router, prefix="/api/v1")
    app.include_router(subscriptions_router, prefix="/api/v1")

    @app.get("/health")
    async def health():
        from tripwire import __version__
        return {"status": "ok", "service": "tripwire", "version": __version__}

    # Stateful Supabase mock
    mock_sb = StatefulMockSupabase()
    app.state.supabase = mock_sb

    # LogOnlyProvider for webhook delivery
    webhook_provider = LogOnlyProvider()
    app.state.webhook_provider = webhook_provider

    # Identity resolver (mock)
    resolver = MockResolver()
    app.state.identity_resolver = resolver

    # Repositories wired to the same stateful mock
    endpoint_repo = EndpointRepository(mock_sb)
    event_repo = EventRepository(mock_sb)
    nonce_repo = NonceRepository(mock_sb)
    delivery_repo = WebhookDeliveryRepository(mock_sb)

    # Realtime notifier
    realtime_notifier = RealtimeNotifier(mock_sb)

    # The real processor — end-to-end pipeline
    processor = EventProcessor(
        endpoint_repo=endpoint_repo,
        event_repo=event_repo,
        nonce_repo=nonce_repo,
        delivery_repo=delivery_repo,
        identity_resolver=resolver,
        realtime_notifier=realtime_notifier,
        webhook_provider=webhook_provider,
    )
    app.state.processor = processor

    # Override wallet auth so tests don't need to sign every request
    _test_wallet = Account.from_key(TEST_PRIVATE_KEY)

    async def _override_auth():
        return WalletAuthContext(wallet_address=_test_wallet.address)

    app.dependency_overrides[require_wallet_auth] = _override_auth

    # Override Goldsky auth so ingest routes work in testing env
    async def _override_goldsky():
        return None

    app.dependency_overrides[_verify_goldsky_request] = _override_goldsky

    # Mark ready
    app.state.ready = True
    app.state.started_at = time.time()

    return app, mock_sb


def _raw_log(
    *,
    recipient: str = RECIPIENT,
    sender: str = SENDER,
    nonce: str = NONCE_HEX,
    value: int = 5_000_000,
    chain_id: int = 8453,
) -> dict:
    """Build a raw Goldsky-decoded log for ingestion."""
    return {
        "transaction_hash": TX_HASH,
        "block_number": 100,
        "block_hash": BLOCK_HASH,
        "log_index": 3,
        "block_timestamp": 1700000000,
        "address": USDC_BASE,
        "chain_id": chain_id,
        "topics": ["0x98de503528ee59b575ef0c0a2576a82497bfc029a5685b209e9ec333479b10a5"],
        "decoded": {
            "authorizer": sender,
            "nonce": nonce,
        },
        "transfer": {
            "from_address": sender,
            "to_address": recipient,
            "value": value,
        },
    }


# ── Tests ────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_endpoint_cache():
    """Clear the module-level endpoint cache between tests to prevent pollution."""
    from tripwire.ingestion.processor import _endpoint_cache
    _endpoint_cache.clear()
    yield
    _endpoint_cache.clear()


@pytest.mark.asyncio
async def test_register_then_ingest_event():
    """Scenario 1: Register endpoint -> ingest event -> verify event has correct endpoint_id."""
    app, mock_sb = _create_integration_app()
    transport = httpx.ASGITransport(app=app)

    with patch("tripwire.ingestion.processor.check_finality", new_callable=AsyncMock) as mock_fin:
        mock_fin.return_value = MagicMock(
            is_finalized=True, confirmations=3, required_confirmations=3
        )

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Step 1: Register an endpoint
            reg_resp = await client.post("/api/v1/endpoints", json={
                "url": "https://myapp.example.com/webhook",
                "mode": "execute",
                "chains": [8453],
                "recipient": RECIPIENT,
                "owner_address": TEST_OWNER_ADDRESS,
            })
            assert reg_resp.status_code == 201
            endpoint = reg_resp.json()
            endpoint_id = endpoint["id"]
            assert endpoint["active"] is True
            assert endpoint["recipient"] == RECIPIENT

            # Step 2: Ingest an event targeting that recipient
            ingest_resp = await client.post(
                "/api/v1/ingest/event",
                json=_raw_log(recipient=RECIPIENT),
            )
            assert ingest_resp.status_code == 200
            result = ingest_resp.json()
            assert result["status"] == "processed"
            assert result["tx_hash"] == TX_HASH

    # Step 3: Verify the event was stored with the correct endpoint_id
    events = mock_sb.get_table_data("events")
    assert len(events) >= 1
    event = events[0]
    assert event["endpoint_id"] == endpoint_id
    assert event["tx_hash"] == TX_HASH


@pytest.mark.asyncio
async def test_duplicate_nonce_dedup():
    """Scenario 2: Register endpoint -> ingest same nonce twice -> second is duplicate."""
    app, mock_sb = _create_integration_app()
    transport = httpx.ASGITransport(app=app)

    with patch("tripwire.ingestion.processor.check_finality", new_callable=AsyncMock) as mock_fin:
        mock_fin.return_value = MagicMock(
            is_finalized=True, confirmations=3, required_confirmations=3
        )

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Register endpoint
            await client.post("/api/v1/endpoints", json={
                "url": "https://myapp.example.com/webhook",
                "mode": "execute",
                "chains": [8453],
                "recipient": RECIPIENT,
                "owner_address": TEST_OWNER_ADDRESS,
            })

            raw = _raw_log(recipient=RECIPIENT, nonce=NONCE_HEX)

            # First ingest — should be processed
            resp1 = await client.post("/api/v1/ingest/event", json=raw)
            assert resp1.status_code == 200
            assert resp1.json()["status"] == "processed"

            # Second ingest with same nonce — should be duplicate
            resp2 = await client.post("/api/v1/ingest/event", json=raw)
            assert resp2.status_code == 200
            assert resp2.json()["status"] == "duplicate"

    # Verify only one nonce row was stored
    nonces = mock_sb.get_table_data("nonces")
    assert len(nonces) == 1


@pytest.mark.asyncio
async def test_wallet_auth_roundtrip():
    """Scenario 3: Register endpoint with real wallet auth, then list using signed headers."""
    import json as _json

    app, mock_sb = _create_integration_app()

    # Remove the default auth override so we exercise real wallet auth
    app.dependency_overrides.pop(require_wallet_auth, None)

    _test_acct = Account.from_key(TEST_PRIVATE_KEY)
    mock_redis = MockRedis()
    transport = httpx.ASGITransport(app=app)

    with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Step 1: Register endpoint with real wallet auth headers
            reg_body = _json.dumps({
                "url": "https://myapp.example.com/webhook",
                "mode": "execute",
                "chains": [8453],
                "recipient": RECIPIENT,
                "owner_address": TEST_OWNER_ADDRESS,
            }, separators=(",", ":")).encode()
            reg_headers = make_auth_headers(
                _test_acct, method="POST", path="/api/v1/endpoints", body=reg_body,
            )
            mock_redis.seed_nonce(reg_headers["X-TripWire-Nonce"])
            reg_headers["Content-Type"] = "application/json"

            reg_resp = await client.post(
                "/api/v1/endpoints",
                content=reg_body,
                headers=reg_headers,
            )
            assert reg_resp.status_code == 201
            body = reg_resp.json()
            assert body["owner_address"].lower() == _test_acct.address.lower()

            # Step 2: List endpoints with real wallet auth headers
            list_headers = make_auth_headers(
                _test_acct, method="GET", path="/api/v1/endpoints",
            )
            mock_redis.seed_nonce(list_headers["X-TripWire-Nonce"])

            list_resp = await client.get(
                "/api/v1/endpoints",
                headers=list_headers,
            )
            assert list_resp.status_code == 200
            data = list_resp.json()
            assert data["count"] >= 1
            ids = [ep["id"] for ep in data["data"]]
            assert body["id"] in ids


@pytest.mark.asyncio
async def test_policy_rejection_min_amount():
    """Scenario 4: Register endpoint with min_amount policy -> ingest below-threshold event."""
    app, mock_sb = _create_integration_app()
    transport = httpx.ASGITransport(app=app)

    with patch("tripwire.ingestion.processor.check_finality", new_callable=AsyncMock) as mock_fin:
        mock_fin.return_value = MagicMock(
            is_finalized=True, confirmations=3, required_confirmations=3
        )

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Register endpoint with min_amount = 10 USDC (10_000_000 in 6-decimal units)
            reg_resp = await client.post("/api/v1/endpoints", json={
                "url": "https://myapp.example.com/webhook",
                "mode": "execute",
                "chains": [8453],
                "recipient": RECIPIENT,
                "owner_address": TEST_OWNER_ADDRESS,
                "policies": {
                    "min_amount": "10000000",
                },
            })
            assert reg_resp.status_code == 201

            # Ingest event with only 5 USDC (5_000_000) — below the threshold
            ingest_resp = await client.post(
                "/api/v1/ingest/event",
                json=_raw_log(recipient=RECIPIENT, value=5_000_000),
            )
            assert ingest_resp.status_code == 200
            result = ingest_resp.json()

            # The event is "processed" (endpoint matched, but policy rejected)
            # The IngestSingleResponse only exposes status/tx_hash/event_id,
            # so we verify via the DB state instead.
            assert result["status"] == "processed"
            assert result["tx_hash"] == TX_HASH

    # Verify the event was recorded but no webhook deliveries were created
    # (policy rejected the below-threshold amount)
    events = mock_sb.get_table_data("events")
    assert len(events) >= 1
    deliveries = mock_sb.get_table_data("webhook_deliveries")
    assert len(deliveries) == 0


@pytest.mark.asyncio
async def test_notify_mode_with_subscription():
    """Scenario 5: Register notify endpoint + subscription -> ingest matching event."""
    app, mock_sb = _create_integration_app()
    transport = httpx.ASGITransport(app=app)

    with patch("tripwire.ingestion.processor.check_finality", new_callable=AsyncMock) as mock_fin:
        mock_fin.return_value = MagicMock(
            is_finalized=True, confirmations=3, required_confirmations=3
        )

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Step 1: Register a notify-mode endpoint
            reg_resp = await client.post("/api/v1/endpoints", json={
                "url": "https://myapp.example.com/realtime",
                "mode": "notify",
                "chains": [8453],
                "recipient": RECIPIENT,
                "owner_address": TEST_OWNER_ADDRESS,
            })
            assert reg_resp.status_code == 201
            endpoint = reg_resp.json()
            endpoint_id = endpoint["id"]

            # Step 2: Create a subscription for this endpoint
            sub_resp = await client.post(
                f"/api/v1/endpoints/{endpoint_id}/subscriptions",
                json={
                    "filters": {
                        "chains": [8453],
                        "recipients": [RECIPIENT],
                    },
                },
            )
            assert sub_resp.status_code == 201
            sub = sub_resp.json()
            assert sub["endpoint_id"] == endpoint_id
            assert sub["active"] is True

            # Step 3: Ingest an event matching the endpoint
            ingest_resp = await client.post(
                "/api/v1/ingest/event",
                json=_raw_log(recipient=RECIPIENT),
            )
            assert ingest_resp.status_code == 200
            result = ingest_resp.json()
            assert result["status"] == "processed"
            assert result["tx_hash"] == TX_HASH

    # Verify no webhook deliveries (notify mode doesn't use Convoy)
    deliveries = mock_sb.get_table_data("webhook_deliveries")
    assert len(deliveries) == 0

    # Verify realtime_events table got a row (notify-mode delivery)
    rt_events = mock_sb.get_table_data("realtime_events")
    assert len(rt_events) == 1
    assert rt_events[0]["endpoint_id"] == endpoint_id
    assert rt_events[0]["type"] == "payment.confirmed"
