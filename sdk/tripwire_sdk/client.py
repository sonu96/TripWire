"""Async client for the TripWire API."""

from __future__ import annotations

import hashlib
import json as json_mod
import logging
import warnings
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

logger = logging.getLogger(__name__)

from tripwire_sdk.errors import (
    SessionError,
    TripWireAuthError,
    TripWireError,
    TripWireNotFoundError,
    TripWireRateLimitError,
    TripWireServerError,
)
from tripwire_sdk.types import (
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    Event,
    PaginatedResponse,
    Session,
    Subscription,
    SubscriptionFilter,
)

# SIWE defaults
_SIWE_DOMAIN = "tripwire.dev"
_EXPIRATION_MINUTES = 5


class TripwireClient:
    """Async client for interacting with the TripWire REST API.

    Authenticates every request by signing a SIWE message with the caller's
    Ethereum private key (EIP-191).  A fresh nonce is fetched from the server
    before each request to prevent replay attacks.

    **Must** be used as an async context manager::

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
        enable_x402: bool = True,
    ) -> None:
        self._account: LocalAccount = Account.from_key(private_key)
        self._base_url = base_url.rstrip("/")
        self._address: str = self._account.address
        self._enable_x402 = enable_x402
        self._http: httpx.AsyncClient | None = None
        self._session_id: str | None = None

    # ── Properties ─────────────────────────────────────────────

    @property
    def wallet_address(self) -> str:
        """The checksummed Ethereum address derived from the private key."""
        return self._address

    def __repr__(self) -> str:
        return (
            f"TripwireClient(address={self._address!r}, "
            f"base_url={self._base_url!r})"
        )

    # ── Context manager ───────────────────────────────────────

    async def __aenter__(self) -> TripwireClient:
        client_kwargs: dict[str, Any] = {
            "base_url": self._base_url,
            "headers": {"Content-Type": "application/json"},
            "timeout": 30.0,
        }

        if self._enable_x402:
            try:
                from x402 import x402Client
                from x402.http.clients import x402HttpxClient
                from x402.mechanisms.evm import EthAccountSigner
                from x402.mechanisms.evm.exact.register import register_exact_evm_client

                x402_client = x402Client()
                register_exact_evm_client(x402_client, EthAccountSigner(self._account))
                self._http = x402HttpxClient(x402_client, **client_kwargs)
                logger.debug("x402 v2 payment handling enabled")
            except ImportError:
                try:
                    # Fallback: try v1-style import path
                    from x402.client import x402Client as x402ClientV1

                    self._http = x402ClientV1(
                        wallet=self._account,
                        **client_kwargs,
                    )
                    logger.debug("x402 v1 payment handling enabled (upgrade to v2 recommended)")
                except ImportError:
                    warnings.warn(
                        "x402 package is not installed — HTTP 402 Payment Required "
                        "responses will not be auto-handled. Install with: "
                        "pip install tripwire-sdk[x402]",
                        stacklevel=2,
                    )
                    self._http = httpx.AsyncClient(**client_kwargs)
        else:
            self._http = httpx.AsyncClient(**client_kwargs)

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
            raise RuntimeError(
                "TripwireClient must be used as an async context manager: "
                "async with TripwireClient(...) as client:"
            )
        return self._http

    @staticmethod
    def _raise_for_status(resp: httpx.Response) -> None:
        """Map HTTP error status codes to typed TripWire exceptions."""
        if resp.status_code < 400:
            return

        detail = resp.text
        try:
            body = resp.json()
            detail = body.get("detail", detail)
        except Exception:
            pass

        status = resp.status_code

        if status in (401, 403):
            raise TripWireAuthError(status, detail)
        if status == 404:
            raise TripWireNotFoundError(status, detail)
        if status == 429:
            retry_after_raw = resp.headers.get("Retry-After")
            retry_after: float | None = None
            if retry_after_raw is not None:
                try:
                    retry_after = float(retry_after_raw)
                except ValueError:
                    pass
            raise TripWireRateLimitError(status, detail, retry_after=retry_after)
        if 500 <= status < 600:
            raise TripWireServerError(status, detail)

        raise TripWireError(status, detail)

    def _make_auth_headers(
        self,
        path: str,
        *,
        nonce: str,
        method: str = "GET",
        body_bytes: bytes = b"",
        domain: str = _SIWE_DOMAIN,
    ) -> dict[str, str]:
        """Build and sign a SIWE message, returning authentication headers.

        Headers returned:
            X-TripWire-Address    -- checksummed wallet address
            X-TripWire-Signature  -- hex EIP-191 signature
            X-TripWire-Nonce      -- server-issued nonce
            X-TripWire-Issued-At  -- ISO-8601 timestamp
            X-TripWire-Expiration -- ISO-8601 expiration timestamp
        """
        now = datetime.now(timezone.utc)
        issued_at = now.isoformat()
        expiration_time = (now + timedelta(minutes=_EXPIRATION_MINUTES)).isoformat()

        body_hash = hashlib.sha256(body_bytes).hexdigest()
        statement = f"{method} {path} {body_hash}"

        message_text = (
            f"{domain} wants you to sign in with your Ethereum account:\n"
            f"{self._address}\n"
            f"\n"
            f"{statement}\n"
            f"\n"
            f"URI: https://{domain}\n"
            f"Version: 1\n"
            f"Chain ID: 8453\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}\n"
            f"Expiration Time: {expiration_time}"
        )

        signable = encode_defunct(text=message_text)
        signed = self._account.sign_message(signable)

        return {
            "X-TripWire-Address": self._address,
            "X-TripWire-Signature": signed.signature.hex(),
            "X-TripWire-Nonce": nonce,
            "X-TripWire-Issued-At": issued_at,
            "X-TripWire-Expiration": expiration_time,
        }

    async def get_nonce(self) -> str:
        """Fetch a fresh one-time nonce from the server for SIWE authentication."""
        try:
            resp = await self._client().get("/auth/nonce")
        except httpx.TimeoutException as exc:
            raise TripWireError(0, f"Request timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            raise TripWireError(0, f"Connection failed: {exc}") from exc

        self._raise_for_status(resp)
        return resp.json()["nonce"]

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
    ) -> Any:
        # Serialize body to bytes for signing (deterministic encoding)
        if json is not None:
            body_bytes = json_mod.dumps(json, separators=(",", ":"), sort_keys=True).encode()
        else:
            body_bytes = b""

        # Fetch a fresh nonce before each authenticated request
        nonce = await self.get_nonce()

        auth_headers = self._make_auth_headers(
            path,
            nonce=nonce,
            method=method.upper(),
            body_bytes=body_bytes,
        )

        # Attach session token when a Keeper session is active
        if self._session_id:
            auth_headers["X-TripWire-Session"] = self._session_id

        try:
            # Send pre-serialized body bytes so the wire payload matches
            # the exact bytes that were hashed for the SIWE signature.
            resp = await self._client().request(
                method,
                path,
                content=body_bytes if body_bytes else None,
                params=params,
                headers=auth_headers,
            )
        except httpx.TimeoutException as exc:
            raise TripWireError(0, f"Request timed out: {exc}") from exc
        except httpx.ConnectError as exc:
            raise TripWireError(0, f"Connection failed: {exc}") from exc

        self._raise_for_status(resp)

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

    # ── Sessions (Keeper) ────────────────────────────────────

    async def open_session(
        self,
        budget: int | None = None,
        ttl_seconds: int | None = None,
        chain_id: int = 8453,
    ) -> Session:
        """Open a pre-funded Keeper session.

        After opening, all subsequent API/MCP calls automatically use
        the session token instead of per-call x402 payments.
        """
        body: dict[str, Any] = {}
        if budget is not None:
            body["budget"] = budget
        if ttl_seconds is not None:
            body["ttl_seconds"] = ttl_seconds
        body["chain_id"] = chain_id

        data = await self._request("POST", "/api/v1/auth/session", json=body)
        session = Session(**data)
        self._session_id = session.session_id
        return session

    async def get_session(self, session_id: str | None = None) -> Session:
        """Get current session status."""
        sid = session_id or self._session_id
        if not sid:
            raise SessionError(0, "No active session")
        data = await self._request("GET", f"/api/v1/auth/session/{sid}")
        return Session(**data)

    async def close_session(self, session_id: str | None = None) -> Session:
        """Close session early. Returns final budget state."""
        sid = session_id or self._session_id
        if not sid:
            raise SessionError(0, "No active session")
        data = await self._request("DELETE", f"/api/v1/auth/session/{sid}")
        self._session_id = None
        return Session(**data)
