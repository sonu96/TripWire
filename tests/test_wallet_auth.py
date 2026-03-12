"""Tests for SIWE wallet-based authentication.

Covers valid signatures, invalid/tampered signatures, expired timestamps,
partial headers, and case-insensitive address matching.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import httpx
import pytest
from eth_account import Account
from eth_account.messages import encode_defunct
from fastapi import Depends, FastAPI

from tripwire.api.auth import WalletAuthContext, require_wallet_auth
from tripwire.api.middleware import RequestLoggingMiddleware

from tests._wallet_helpers import (
    TEST_PRIVATE_KEY,
    OTHER_PRIVATE_KEY,
    MockRedis,
    make_auth_headers,
    _build_siwe_message,
)
from tripwire.config.settings import settings


# ── Minimal app for auth testing ─────────────────────────────


def _auth_test_app() -> FastAPI:
    """Build a tiny FastAPI app with a single authenticated endpoint."""
    app = FastAPI()
    app.add_middleware(RequestLoggingMiddleware)

    @app.get("/protected")
    async def protected(wallet: WalletAuthContext = Depends(require_wallet_auth)):
        return {"wallet_address": wallet.wallet_address}

    @app.post("/protected")
    async def protected_post(wallet: WalletAuthContext = Depends(require_wallet_auth)):
        return {"wallet_address": wallet.wallet_address}

    return app


# ── Helpers ──────────────────────────────────────────────────

_PRIMARY = Account.from_key(TEST_PRIVATE_KEY)
_OTHER = Account.from_key(OTHER_PRIVATE_KEY)


def _make_headers_and_seed(mock_redis, account, *, method="GET", path="/protected", body=b"", **kwargs):
    """Build auth headers and seed the nonce in mock Redis."""
    headers = make_auth_headers(account, method=method, path=path, body=body, **kwargs)
    mock_redis.seed_nonce(headers["X-TripWire-Nonce"])
    return headers


# ── TestValidSignatures ──────────────────────────────────────


class TestValidSignatures:
    """A correctly signed request should be accepted."""

    @pytest.mark.asyncio
    async def test_valid_signature_returns_200(self):
        app = _auth_test_app()
        mock_redis = MockRedis()
        headers = _make_headers_and_seed(mock_redis, _PRIMARY, method="GET", path="/protected")

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_valid_signature_returns_correct_address(self):
        app = _auth_test_app()
        mock_redis = MockRedis()
        headers = _make_headers_and_seed(mock_redis, _PRIMARY, method="GET", path="/protected")

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        body = resp.json()
        assert body["wallet_address"].lower() == _PRIMARY.address.lower()


# ── TestInvalidSignatures ────────────────────────────────────


class TestInvalidSignatures:
    """Signatures that are wrong, tampered, or mismatched must be rejected."""

    @pytest.mark.asyncio
    async def test_wrong_key_rejected(self):
        """Sign with OTHER key but claim PRIMARY address."""
        app = _auth_test_app()
        mock_redis = MockRedis()
        # Build headers for OTHER, then swap the address to PRIMARY
        headers = _make_headers_and_seed(mock_redis, _OTHER, method="GET", path="/protected")
        headers["X-TripWire-Address"] = _PRIMARY.address

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 401
        # Could be "does not match" or "Invalid signature" depending on recovery
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_tampered_signature_rejected(self):
        """Flip a byte in a valid signature."""
        app = _auth_test_app()
        mock_redis = MockRedis()
        headers = _make_headers_and_seed(mock_redis, _PRIMARY, method="GET", path="/protected")

        sig = headers["X-TripWire-Signature"]
        if sig.startswith("0x"):
            sig_bytes = bytes.fromhex(sig[2:])
        else:
            sig_bytes = bytes.fromhex(sig)
        tampered = bytes([sig_bytes[0] ^ 0xFF]) + sig_bytes[1:]
        headers["X-TripWire-Signature"] = "0x" + tampered.hex()

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_address_mismatch_rejected(self):
        """Valid signature from OTHER but claimed address is PRIMARY."""
        app = _auth_test_app()
        mock_redis = MockRedis()
        # Sign correctly for OTHER
        headers = _make_headers_and_seed(mock_redis, _OTHER, method="GET", path="/protected")
        # Swap in PRIMARY's address
        headers["X-TripWire-Address"] = _PRIMARY.address

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 401


# ── TestExpiredTimestamps ────────────────────────────────────


class TestExpiredTimestamps:
    """Expired or not-yet-valid timestamps must be rejected."""

    @pytest.mark.asyncio
    async def test_expired_signature_rejected(self):
        """Expiration time in the past."""
        app = _auth_test_app()
        mock_redis = MockRedis()
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        headers = _make_headers_and_seed(
            mock_redis, _PRIMARY, method="GET", path="/protected",
            expiration_time=past,
        )

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 401
        assert "expired" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_very_old_expiration_rejected(self):
        """Expiration time far in the past."""
        app = _auth_test_app()
        mock_redis = MockRedis()
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        headers = _make_headers_and_seed(
            mock_redis, _PRIMARY, method="GET", path="/protected",
            expiration_time=old,
        )

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 401


# ── TestPartialHeaders ───────────────────────────────────────


class TestPartialHeaders:
    """Missing any subset of the five required headers must yield 401."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "missing_header",
        [
            "X-TripWire-Address",
            "X-TripWire-Signature",
            "X-TripWire-Nonce",
            "X-TripWire-Issued-At",
            "X-TripWire-Expiration",
        ],
    )
    async def test_missing_single_header(self, missing_header):
        app = _auth_test_app()
        mock_redis = MockRedis()
        headers = _make_headers_and_seed(mock_redis, _PRIMARY, method="GET", path="/protected")
        del headers[missing_header]

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_no_headers_returns_401(self):
        """With APP_ENV=testing (not development), missing all headers is 401."""
        app = _auth_test_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/protected")

        assert resp.status_code == 401


# ── TestCaseInsensitive ──────────────────────────────────────


class TestCaseInsensitive:
    """Both checksum and lowercase addresses should be accepted."""

    @pytest.mark.asyncio
    async def test_checksum_address_accepted(self):
        app = _auth_test_app()
        mock_redis = MockRedis()
        headers = _make_headers_and_seed(mock_redis, _PRIMARY, method="GET", path="/protected")
        # Verify the address is in checksum format
        assert headers["X-TripWire-Address"] == _PRIMARY.address
        assert any(c.isupper() for c in headers["X-TripWire-Address"][2:])

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_lowercase_address_accepted(self):
        """Lowercase address in the header should still verify.

        We must sign the SIWE message with the lowercase address in the
        message text, since auth.py reconstructs the message using the
        header value directly.
        """
        app = _auth_test_app()
        mock_redis = MockRedis()

        # Build headers with lowercase address by constructing manually
        import secrets as _secrets
        nonce = _secrets.token_urlsafe(32)
        mock_redis.seed_nonce(nonce)

        now = datetime.now(timezone.utc)
        issued_at = now.isoformat()
        expiration_time = (now + timedelta(minutes=5)).isoformat()
        address_lower = _PRIMARY.address.lower()
        body_hash = hashlib.sha256(b"").hexdigest()
        statement = f"GET /protected {body_hash}"

        message_text = _build_siwe_message(
            domain=settings.siwe_domain,
            address=address_lower,
            statement=statement,
            nonce=nonce,
            issued_at=issued_at,
            expiration_time=expiration_time,
        )
        signable = encode_defunct(text=message_text)
        signed = _PRIMARY.sign_message(signable)
        sig_hex = signed.signature.hex() if isinstance(signed.signature, bytes) else str(signed.signature)

        headers = {
            "X-TripWire-Address": address_lower,
            "X-TripWire-Signature": sig_hex,
            "X-TripWire-Nonce": nonce,
            "X-TripWire-Issued-At": issued_at,
            "X-TripWire-Expiration": expiration_time,
        }

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 200
        body = resp.json()
        assert body["wallet_address"].lower() == address_lower


# ── TestNonceReplay ──────────────────────────────────────────


class TestNonceReplay:
    """Nonces must be consumed after first use; replays are rejected."""

    @pytest.mark.asyncio
    async def test_nonce_replay_rejected(self):
        """Using the same nonce twice should fail on the second attempt."""
        app = _auth_test_app()
        mock_redis = MockRedis()
        headers = _make_headers_and_seed(mock_redis, _PRIMARY, method="GET", path="/protected")

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # First request succeeds
                resp1 = await client.get("/protected", headers=headers)
                assert resp1.status_code == 200

                # Second request with same nonce is rejected
                resp2 = await client.get("/protected", headers=headers)
                assert resp2.status_code == 401
                assert "nonce" in resp2.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_unknown_nonce_rejected(self):
        """A nonce not seeded in Redis should be rejected."""
        app = _auth_test_app()
        mock_redis = MockRedis()
        # Build headers but do NOT seed the nonce
        headers = make_auth_headers(_PRIMARY, method="GET", path="/protected")

        with patch("tripwire.api.auth._get_redis", return_value=mock_redis):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.get("/protected", headers=headers)

        assert resp.status_code == 401
        assert "nonce" in resp.json()["detail"].lower()
