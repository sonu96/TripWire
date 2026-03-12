"""Async client for the TripWire API."""

from __future__ import annotations

from typing import Any

import httpx
from eth_account import Account

from tripwire_sdk.signer import make_auth_headers
from tripwire_sdk.types import (
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    Event,
    PaginatedResponse,
    Subscription,
    SubscriptionFilter,
)


class TripwireAPIError(Exception):
    """Raised when the TripWire API returns an error response."""

    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"TripWire API error {status_code}: {detail}")


class TripwireClient:
    """Async client for interacting with the TripWire REST API.

    Authenticates every request by signing it with the caller's Ethereum
    private key (EIP-191).  The server verifies the signature to identify
    the wallet address.

    Usage::

        async with TripwireClient(private_key="0x...") as client:
            print(client.wallet_address)
            ep = await client.register_endpoint(
                url="https://example.com/webhook",
                mode="execute",
                chains=[8453],
                recipient="0xAbC...",
            )
    """

    def __init__(
        self,
        private_key: str,
        base_url: str = "https://tripwire-production.up.railway.app",
    ) -> None:
        self._private_key = private_key
        self._base_url = base_url.rstrip("/")
        self._account = Account.from_key(private_key)
        self._address: str = self._account.address
        self._http: httpx.AsyncClient | None = None

    # ── Properties ─────────────────────────────────────────────

    @property
    def wallet_address(self) -> str:
        """The checksummed Ethereum address derived from the private key."""
        return self._address

    # ── Context manager ───────────────────────────────────────

    async def __aenter__(self) -> TripwireClient:
        self._http = httpx.AsyncClient(
            base_url=self._base_url,
            headers={"Content-Type": "application/json"},
            timeout=30.0,
        )
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    # ── Internal helpers ──────────────────────────────────────

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._base_url,
                headers={"Content-Type": "application/json"},
                timeout=30.0,
            )
        return self._http

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        auth_headers = make_auth_headers(self._private_key, self._address, path)
        resp = await self._client().request(
            method, path, json=json, params=params, headers=auth_headers
        )
        if resp.status_code >= 400:
            detail = resp.text
            try:
                body = resp.json()
                detail = body.get("detail", detail)
            except Exception:
                pass
            raise TripwireAPIError(resp.status_code, detail)
        if resp.status_code == 204:
            return None
        return resp.json()

    # ── Endpoints ─────────────────────────────────────────────

    async def register_endpoint(
        self,
        url: str,
        mode: str | EndpointMode,
        chains: list[int],
        recipient: str,
        policies: EndpointPolicies | dict | None = None,
    ) -> Endpoint:
        """Register a new webhook endpoint."""
        body: dict[str, Any] = {
            "url": url,
            "mode": mode if isinstance(mode, str) else mode.value,
            "chains": chains,
            "recipient": recipient,
        }
        if policies is not None:
            body["policies"] = (
                policies.model_dump() if isinstance(policies, EndpointPolicies) else policies
            )
        data = await self._request("POST", "/api/v1/endpoints", json=body)
        return Endpoint(**data)

    async def list_endpoints(self) -> list[Endpoint]:
        """List all active endpoints."""
        data = await self._request("GET", "/api/v1/endpoints")
        return [Endpoint(**ep) for ep in data["data"]]

    async def get_endpoint(self, endpoint_id: str) -> Endpoint:
        """Get endpoint details by ID."""
        data = await self._request("GET", f"/api/v1/endpoints/{endpoint_id}")
        return Endpoint(**data)

    async def update_endpoint(self, endpoint_id: str, **kwargs: Any) -> Endpoint:
        """Update an endpoint. Pass keyword arguments for fields to change."""
        if "mode" in kwargs and isinstance(kwargs["mode"], EndpointMode):
            kwargs["mode"] = kwargs["mode"].value
        if "policies" in kwargs and isinstance(kwargs["policies"], EndpointPolicies):
            kwargs["policies"] = kwargs["policies"].model_dump()
        data = await self._request("PATCH", f"/api/v1/endpoints/{endpoint_id}", json=kwargs)
        return Endpoint(**data)

    async def delete_endpoint(self, endpoint_id: str) -> None:
        """Deactivate (soft-delete) an endpoint."""
        await self._request("DELETE", f"/api/v1/endpoints/{endpoint_id}")

    # ── Subscriptions ─────────────────────────────────────────

    async def create_subscription(
        self,
        endpoint_id: str,
        filters: SubscriptionFilter | dict,
    ) -> Subscription:
        """Create a subscription for a notify-mode endpoint."""
        body = {
            "filters": (
                filters.model_dump() if isinstance(filters, SubscriptionFilter) else filters
            ),
        }
        data = await self._request(
            "POST", f"/api/v1/endpoints/{endpoint_id}/subscriptions", json=body
        )
        return Subscription(**data)

    async def list_subscriptions(self, endpoint_id: str) -> list[Subscription]:
        """List active subscriptions for an endpoint."""
        data = await self._request("GET", f"/api/v1/endpoints/{endpoint_id}/subscriptions")
        return [Subscription(**sub) for sub in data]

    async def delete_subscription(self, subscription_id: str) -> None:
        """Deactivate a subscription."""
        await self._request("DELETE", f"/api/v1/subscriptions/{subscription_id}")

    # ── Events ────────────────────────────────────────────────

    async def list_events(
        self,
        cursor: str | None = None,
        limit: int = 50,
        **filters: Any,
    ) -> PaginatedResponse:
        """List events with cursor pagination and optional filters."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        for key, val in filters.items():
            if val is not None:
                params[key] = val
        data = await self._request("GET", "/api/v1/events", params=params)
        return PaginatedResponse(**data)

    async def get_event(self, event_id: str) -> Event:
        """Get a single event by ID."""
        data = await self._request("GET", f"/api/v1/events/{event_id}")
        return Event(**data)

    async def ingest_event(self, event: dict[str, Any]) -> dict[str, Any]:
        """POST a single event to the ingestion endpoint (for testing)."""
        data = await self._request("POST", "/api/v1/ingest/event", json=event)
        return data

    async def get_endpoint_events(
        self,
        endpoint_id: str,
        cursor: str | None = None,
        limit: int = 50,
    ) -> PaginatedResponse:
        """List events for a specific endpoint."""
        params: dict[str, Any] = {"limit": limit}
        if cursor is not None:
            params["cursor"] = cursor
        data = await self._request(
            "GET", f"/api/v1/endpoints/{endpoint_id}/events", params=params
        )
        return PaginatedResponse(**data)
