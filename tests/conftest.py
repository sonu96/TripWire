"""Shared fixtures for TripWire test suite."""

import os

# Set APP_ENV to testing BEFORE any settings import so dev bypass is never active
os.environ["APP_ENV"] = "testing"
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_ANON_KEY", "test-anon-key")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-service-role-key")
os.environ.setdefault("CONVOY_API_KEY", "test-convoy-key")
os.environ.setdefault("CONVOY_URL", "http://localhost:5005")

from datetime import datetime, timezone

import pytest
from eth_account import Account

from tripwire.types.models import (
    AgentIdentity,
    ChainId,
    Endpoint,
    EndpointMode,
    EndpointPolicies,
    ERC3009Transfer,
)

from tests._wallet_helpers import (
    TEST_PRIVATE_KEY,
    OTHER_PRIVATE_KEY,
    make_auth_headers,
    MockRedis,
)

# ── Constants ─────────────────────────────────────────────────

USDC_BASE = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
SENDER = "0x1234567890abcdef1234567890abcdef12345678"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
AUTHORIZER = "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
NONCE_HEX = "0x" + "ab" * 32
TX_HASH = "0x" + "ff" * 32
BLOCK_HASH = "0x" + "ee" * 32


# ── Wallet fixtures ──────────────────────────────────────────


@pytest.fixture
def test_wallet():
    """Primary test wallet derived from Hardhat account #0."""
    acct = Account.from_key(TEST_PRIVATE_KEY)
    return {"account": acct, "address": acct.address}


@pytest.fixture
def other_wallet():
    """Secondary test wallet derived from Hardhat account #1."""
    acct = Account.from_key(OTHER_PRIVATE_KEY)
    return {"account": acct, "address": acct.address}


@pytest.fixture
def mock_redis():
    """In-memory Redis mock for nonce management."""
    return MockRedis()


@pytest.fixture
def auth_headers(test_wallet):
    """Auth headers for the primary test wallet, targeting GET /."""
    return make_auth_headers(test_wallet["account"], method="GET", path="/")


# ── Sample model fixtures ────────────────────────────────────


@pytest.fixture
def sample_transfer() -> ERC3009Transfer:
    return ERC3009Transfer(
        chain_id=ChainId.BASE,
        tx_hash=TX_HASH,
        block_number=100,
        block_hash=BLOCK_HASH,
        log_index=3,
        from_address=SENDER,
        to_address=RECIPIENT,
        value="5000000",
        authorizer=AUTHORIZER,
        valid_after=0,
        valid_before=2**32 - 1,
        nonce=NONCE_HEX,
        token=USDC_BASE.lower(),
        timestamp=1700000000,
    )


@pytest.fixture
def sample_endpoint() -> Endpoint:
    now = datetime.now(timezone.utc)
    return Endpoint(
        id="ep_abc123def456ghi78",
        url="https://myapp.example.com/webhook",
        mode=EndpointMode.EXECUTE,
        chains=[8453],
        recipient=RECIPIENT,
        owner_address="0x0000000000000000000000000000000000000000",
        policies=EndpointPolicies(),
        active=True,
        created_at=now,
        updated_at=now,
    )


@pytest.fixture
def sample_identity() -> AgentIdentity:
    return AgentIdentity(
        address=AUTHORIZER,
        agent_class="trading-bot",
        deployer="0xdeaDDeADDEaDdeaDdEAddEADDEAdDeadDEADDEaD",
        capabilities=["swap", "limit-order", "portfolio-rebalance"],
        reputation_score=85.0,
        registered_at=1738108800,
        metadata={"agent_id": 1, "agent_uri": "https://example.com/agents/trading-bot"},
    )


@pytest.fixture
def sample_raw_log() -> dict:
    return {
        "transaction_hash": TX_HASH,
        "block_number": 100,
        "block_hash": BLOCK_HASH,
        "log_index": 3,
        "block_timestamp": 1700000000,
        "address": USDC_BASE.lower(),
        "chain_id": 8453,
        "decoded": {
            "authorizer": AUTHORIZER,
            "nonce": NONCE_HEX,
        },
    }


@pytest.fixture
def sample_raw_log_enriched() -> dict:
    """Raw log with joined Transfer data (as produced by the updated pipeline)."""
    return {
        "transaction_hash": TX_HASH,
        "block_number": 100,
        "block_hash": BLOCK_HASH,
        "log_index": 3,
        "block_timestamp": 1700000000,
        "address": USDC_BASE.lower(),
        "chain_id": 8453,
        "decoded": {
            "authorizer": AUTHORIZER,
            "nonce": NONCE_HEX,
        },
        "transfer": {
            "from_address": SENDER,
            "to_address": RECIPIENT,
            "value": 5_000_000,
        },
    }
