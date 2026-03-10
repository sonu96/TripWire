"""Policy evaluation engine for TripWire."""

import structlog

from tripwire.types.models import AgentIdentity, EndpointPolicies, TransferData

logger = structlog.get_logger(__name__)


def evaluate_policy(
    transfer: TransferData,
    identity: AgentIdentity | None,
    policies: EndpointPolicies,
) -> tuple[bool, str | None]:
    """Evaluate transfer against endpoint policies.

    Returns (allowed, reason). If allowed is False, reason explains why.
    """
    sender = transfer.from_address.lower()
    amount = int(transfer.amount)

    # Amount range checks
    if policies.min_amount is not None and amount < int(policies.min_amount):
        reason = f"Amount {amount} below minimum {policies.min_amount}"
        logger.info("policy_denied", check="min_amount", sender=sender, reason=reason)
        return False, reason

    if policies.max_amount is not None and amount > int(policies.max_amount):
        reason = f"Amount {amount} above maximum {policies.max_amount}"
        logger.info("policy_denied", check="max_amount", sender=sender, reason=reason)
        return False, reason

    # Sender blocklist
    if policies.blocked_senders:
        blocked = [s.lower() for s in policies.blocked_senders]
        if sender in blocked:
            reason = f"Sender {sender} is blocked"
            logger.info("policy_denied", check="blocked_sender", sender=sender)
            return False, reason

    # Sender allowlist (if set, only listed senders are allowed)
    if policies.allowed_senders:
        allowed = [s.lower() for s in policies.allowed_senders]
        if sender not in allowed:
            reason = f"Sender {sender} not in allowlist"
            logger.info("policy_denied", check="allowed_senders", sender=sender)
            return False, reason

    # Agent class check (requires identity)
    if policies.required_agent_class is not None:
        if identity is None:
            reason = "Agent identity required but not available"
            logger.info("policy_denied", check="agent_class", sender=sender, reason=reason)
            return False, reason
        if identity.agent_class != policies.required_agent_class:
            reason = (
                f"Agent class '{identity.agent_class}' does not match "
                f"required '{policies.required_agent_class}'"
            )
            logger.info("policy_denied", check="agent_class", sender=sender, reason=reason)
            return False, reason

    # Reputation score check (requires identity)
    if policies.min_reputation_score is not None:
        if identity is None:
            reason = "Agent identity required for reputation check but not available"
            logger.info("policy_denied", check="reputation", sender=sender, reason=reason)
            return False, reason
        if identity.reputation_score < policies.min_reputation_score:
            reason = (
                f"Reputation score {identity.reputation_score} below "
                f"minimum {policies.min_reputation_score}"
            )
            logger.info(
                "policy_denied",
                check="reputation",
                sender=sender,
                score=identity.reputation_score,
                required=policies.min_reputation_score,
            )
            return False, reason

    logger.debug("policy_allowed", sender=sender, amount=amount)
    return True, None
