"""Tests for tripwire/api/policies/engine.py."""

from tripwire.api.policies.engine import evaluate_policy
from tripwire.types.models import (
    AgentIdentity,
    ChainId,
    EndpointPolicies,
    TransferData,
)

SENDER = "0x1234567890abcdef1234567890abcdef12345678"
RECIPIENT = "0xabcdefabcdefabcdefabcdefabcdefabcdefabcd"
TX_HASH = "0x" + "ff" * 32
NONCE_HEX = "0x" + "ab" * 32
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"


def _transfer(amount: str = "5000000", sender: str = SENDER) -> TransferData:
    return TransferData(
        chain_id=ChainId.BASE,
        tx_hash=TX_HASH,
        block_number=100,
        from_address=sender,
        to_address=RECIPIENT,
        amount=amount,
        nonce=NONCE_HEX,
        token=USDC_BASE,
    )


def _identity(
    agent_class: str = "trading-bot",
    reputation: float = 85.0,
    address: str = SENDER,
) -> AgentIdentity:
    return AgentIdentity(
        address=address,
        agent_class=agent_class,
        deployer="0xdeaDDeADDEaDdeaDdEAddEADDEAdDeadDEADDEaD",
        capabilities=["swap"],
        reputation_score=reputation,
        registered_at=1738108800,
    )


def test_allow_no_policies():
    policies = EndpointPolicies()
    allowed, reason = evaluate_policy(_transfer(), None, policies)
    assert allowed is True
    assert reason is None


def test_reject_below_min_amount():
    policies = EndpointPolicies(min_amount="10000000")
    allowed, reason = evaluate_policy(_transfer(amount="5000000"), None, policies)
    assert allowed is False
    assert "below minimum" in reason


def test_reject_above_max_amount():
    policies = EndpointPolicies(max_amount="1000000")
    allowed, reason = evaluate_policy(_transfer(amount="5000000"), None, policies)
    assert allowed is False
    assert "above maximum" in reason


def test_reject_blocked_sender():
    policies = EndpointPolicies(blocked_senders=[SENDER])
    allowed, reason = evaluate_policy(_transfer(), None, policies)
    assert allowed is False
    assert "blocked" in reason


def test_reject_sender_not_in_allowlist():
    other_sender = "0x9999999999999999999999999999999999999999"
    policies = EndpointPolicies(allowed_senders=[other_sender])
    allowed, reason = evaluate_policy(_transfer(), None, policies)
    assert allowed is False
    assert "not in allowlist" in reason


def test_allow_sender_in_allowlist():
    policies = EndpointPolicies(allowed_senders=[SENDER])
    allowed, reason = evaluate_policy(_transfer(), None, policies)
    assert allowed is True
    assert reason is None


def test_reject_wrong_agent_class():
    policies = EndpointPolicies(required_agent_class="data-oracle")
    identity = _identity(agent_class="trading-bot")
    allowed, reason = evaluate_policy(_transfer(), identity, policies)
    assert allowed is False
    assert "does not match" in reason


def test_reject_no_identity_when_class_required():
    policies = EndpointPolicies(required_agent_class="trading-bot")
    allowed, reason = evaluate_policy(_transfer(), None, policies)
    assert allowed is False
    assert "identity required" in reason


def test_reject_low_reputation():
    policies = EndpointPolicies(min_reputation_score=90.0)
    identity = _identity(reputation=85.0)
    allowed, reason = evaluate_policy(_transfer(), identity, policies)
    assert allowed is False
    assert "below" in reason


def test_reject_no_identity_when_reputation_required():
    policies = EndpointPolicies(min_reputation_score=50.0)
    allowed, reason = evaluate_policy(_transfer(), None, policies)
    assert allowed is False
    assert "identity required" in reason


def test_allow_all_checks_pass():
    policies = EndpointPolicies(
        min_amount="1000000",
        max_amount="100000000",
        allowed_senders=[SENDER],
        blocked_senders=["0x0000000000000000000000000000000000000bad"],
        required_agent_class="trading-bot",
        min_reputation_score=80.0,
    )
    identity = _identity(agent_class="trading-bot", reputation=85.0, address=SENDER)
    allowed, reason = evaluate_policy(_transfer(amount="5000000"), identity, policies)
    assert allowed is True
    assert reason is None
