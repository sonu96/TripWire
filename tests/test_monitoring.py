"""Tests for monitoring endpoints: /health/detailed, /ready, /api/v1/stats."""

from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tripwire import __version__
from tripwire.api.middleware import RequestLoggingMiddleware
from tripwire.api.routes.stats import router as stats_router


class _MockResult:
    def __init__(self, data: list, count: int | None = None):
        self.data = data
        self.count = count


class _MockQuery:
    def __init__(self, data: list, count: int | None = None):
        self._data = data
        self._count = count

    def select(self, *a, **kw) -> "_MockQuery":
        return self

    def eq(self, *a, **kw) -> "_MockQuery":
        return self

    def gte(self, *a, **kw) -> "_MockQuery":
        return self

    def order(self, *a, **kw) -> "_MockQuery":
        return self

    def limit(self, *a, **kw) -> "_MockQuery":
        return self

    def execute(self) -> _MockResult:
        return _MockResult(self._data, self._count)


class MockSupabase:
    def __init__(self, data: list | None = None, count: int | None = None):
        self._data = data or []
        self._count = count

    def table(self, name: str) -> _MockQuery:
        return _MockQuery(self._data, self._count)


def _create_monitoring_app(
    supabase=None,
    webhook_provider=None,
    identity_resolver=None,
    ready: bool = True,
) -> FastAPI:
    """Create a minimal FastAPI app with monitoring endpoints."""
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)
    app.include_router(stats_router, prefix="/api/v1")

    app.state.supabase = supabase or MockSupabase()
    app.state.webhook_provider = webhook_provider or AsyncMock()
    app.state.identity_resolver = identity_resolver or MagicMock()
    app.state.ready = ready
    app.state.started_at = time.time()

    @app.get("/health")
    async def health():
        return {"status": "ok", "service": "tripwire", "version": __version__}

    @app.get("/health/detailed")
    async def health_detailed(request: Request):
        """Deep health check."""
        components: dict[str, dict] = {}
        try:
            sb = request.app.state.supabase
            sb.table("events").select("id").limit(1).execute()
            components["supabase"] = {"status": "healthy"}
        except Exception as exc:
            components["supabase"] = {"status": "unhealthy", "error": str(exc)}

        try:
            wp = request.app.state.webhook_provider
            if hasattr(wp, "_api_key"):
                components["webhook_provider"] = {"status": "healthy", "type": "convoy"}
            else:
                components["webhook_provider"] = {"status": "healthy", "type": "log_only"}
        except Exception as exc:
            components["webhook_provider"] = {"status": "unhealthy", "error": str(exc)}

        try:
            resolver = request.app.state.identity_resolver
            resolver_type = type(resolver).__name__
            components["identity_resolver"] = {"status": "healthy", "type": resolver_type}
        except Exception as exc:
            components["identity_resolver"] = {"status": "unhealthy", "error": str(exc)}

        statuses = [c["status"] for c in components.values()]
        if all(s == "healthy" for s in statuses):
            overall = "healthy"
        elif any(s == "unhealthy" for s in statuses):
            overall = "unhealthy"
        else:
            overall = "degraded"

        status_code = 200 if overall == "healthy" else 503
        uptime = time.time() - getattr(request.app.state, "started_at", time.time())
        return JSONResponse(
            status_code=status_code,
            content={
                "status": overall,
                "version": __version__,
                "uptime_seconds": round(uptime, 1),
                "components": components,
            },
        )

    @app.get("/ready")
    async def readiness(request: Request):
        """Readiness probe."""
        if getattr(request.app.state, "ready", False):
            return {"ready": True}
        return JSONResponse(status_code=503, content={"ready": False})

    return app


# ── /health/detailed tests ───────────────────────────────────


@pytest.mark.asyncio
async def test_health_detailed_all_healthy():
    app = _create_monitoring_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health/detailed")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "healthy"
    assert "uptime_seconds" in body
    assert body["components"]["supabase"]["status"] == "healthy"
    assert body["components"]["webhook_provider"]["status"] == "healthy"
    assert body["components"]["identity_resolver"]["status"] == "healthy"


@pytest.mark.asyncio
async def test_health_detailed_supabase_unhealthy():
    """When Supabase query raises, the component should be unhealthy."""

    class BrokenSupabase:
        def table(self, name: str):
            raise ConnectionError("Supabase unreachable")

    app = _create_monitoring_app(supabase=BrokenSupabase())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health/detailed")

    assert resp.status_code == 503
    body = resp.json()
    assert body["status"] == "unhealthy"
    assert body["components"]["supabase"]["status"] == "unhealthy"
    assert "Supabase unreachable" in body["components"]["supabase"]["error"]


# ── /ready tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_ready_after_startup():
    app = _create_monitoring_app(ready=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")

    assert resp.status_code == 200
    assert resp.json()["ready"] is True


@pytest.mark.asyncio
async def test_not_ready_before_startup():
    app = _create_monitoring_app(ready=False)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/ready")

    assert resp.status_code == 503
    assert resp.json()["ready"] is False


# ── /api/v1/stats tests ─────────────────────────────────────


@pytest.mark.asyncio
async def test_stats_returns_counts():
    """Stats endpoint queries DB and returns aggregated counts."""
    now_iso = datetime.now(timezone.utc).isoformat()

    class StatsSupabase:
        """Mock that returns different data per table."""

        def table(self, name: str):
            if name == "events":
                return _StatsEventsQuery(now_iso)
            if name == "endpoints":
                return _StatsEndpointsQuery()
            return _MockQuery([])

    app = _create_monitoring_app(supabase=StatsSupabase())
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert "total_events" in body
    assert "events_last_hour" in body
    assert "active_endpoints" in body
    assert "last_event_at" in body
    assert body["last_event_at"] == now_iso


class _StatsEventsQuery:
    """Simulates event queries for the stats endpoint."""

    def __init__(self, now_iso: str):
        self._now_iso = now_iso
        self._is_count = False
        self._is_recent = False
        self._is_latest = False

    def select(self, *a, **kw):
        if kw.get("count") == "exact":
            self._is_count = True
        return self

    def gte(self, *a, **kw):
        self._is_recent = True
        return self

    def order(self, *a, **kw):
        self._is_latest = True
        return self

    def limit(self, *a, **kw):
        return self

    def eq(self, *a, **kw):
        return self

    def execute(self):
        if self._is_latest:
            return _MockResult([{"created_at": self._now_iso}])
        if self._is_recent:
            return _MockResult([{"id": "1"}, {"id": "2"}], count=2)
        # Total count
        return _MockResult(
            [{"id": "1"}, {"id": "2"}, {"id": "3"}],
            count=3,
        )


class _StatsEndpointsQuery:
    """Simulates endpoint queries for the stats endpoint."""

    def select(self, *a, **kw):
        return self

    def eq(self, *a, **kw):
        return self

    def execute(self):
        return _MockResult([{"id": "ep1"}, {"id": "ep2"}], count=2)


@pytest.mark.asyncio
async def test_stats_empty_database():
    """Stats endpoint handles empty DB gracefully."""
    app = _create_monitoring_app(supabase=MockSupabase(data=[], count=0))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/stats")

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_events"] == 0
    assert body["last_event_at"] is None
