"""Tests for tripwire/api/middleware.py."""

import pytest
import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tripwire.api.middleware import RequestLoggingMiddleware


def _create_test_app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/echo")
    async def echo():
        return {"msg": "hello"}

    return app


@pytest.mark.asyncio
async def test_request_id_generated():
    app = _create_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/echo")

    assert resp.status_code == 200
    rid = resp.headers.get("x-request-id")
    assert rid is not None
    assert len(rid) > 0


@pytest.mark.asyncio
async def test_request_id_preserved():
    app = _create_test_app()
    transport = httpx.ASGITransport(app=app)
    custom_id = "my-trace-id-12345"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/echo", headers={"X-Request-ID": custom_id})

    assert resp.status_code == 200
    assert resp.headers.get("x-request-id") == custom_id


@pytest.mark.asyncio
async def test_health_endpoint_logged():
    app = _create_test_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "x-request-id" in resp.headers
